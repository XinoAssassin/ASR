//! The app's one proxy setting (`docs/desktop-ipc.md` "Proxy"): `system` (Windows Internet
//! settings, overridden by an `HTTP(S)_PROXY` variable of the app process), `none` or `manual`.
//! The effective proxy goes to `uv` (managed setup) and the sidecar as environment variables and
//! to the uploader's HTTP client; loopback and private LAN addresses bypass it by default.
//!
//! Credentials in a proxy URL are stored (`proxy.json` in the app data dir) but leave this module
//! only masked (`http://***@host:port`) and are never logged.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::ipc::{EffectiveProxy, ProxyMode, ProxySettings, ProxySource, ProxyState, ProxyTest};

pub const DEFAULT_NO_PROXY: &str =
    "localhost,127.0.0.1,::1,10.*,172.16.*-172.31.*,192.168.*,*.local";
const MASK: &str = "***";
/// Variables a child process gets (or loses, for no proxy).
const PROXY_VARS: [&str; 6] = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
];

/// Something that yields the current effective proxy (re-read at each use: the Windows
/// settings may change while the app runs).
pub type ProxyFn = Arc<dyn Fn() -> Resolved + Send + Sync>;

impl Default for ProxySettings {
    fn default() -> Self {
        Self {
            mode: ProxyMode::System,
            url: None,
            no_proxy: DEFAULT_NO_PROXY.into(),
        }
    }
}

/// Windows "Internet Settings" of the current user.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SystemProxy {
    pub enabled: bool,
    /// `ProxyServer`: `host:port` or `http=host:port;https=host:port;socks=host:port`.
    pub server: Option<String>,
    /// `ProxyOverride`: `;`-separated, `<local>` = names without a dot.
    pub overrides: Option<String>,
    /// `AutoConfigURL` (PAC script).
    pub pac: Option<String>,
}

/// The proxy actually used: the real URL (with credentials) for clients and child processes,
/// the bypass list, and the masked report for the SPA.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved {
    url: Option<String>,
    no_proxy: Vec<String>,
    pub effective: EffectiveProxy,
}

impl Resolved {
    /// A direct connection (no proxy at all).
    pub fn direct() -> Self {
        resolve(
            &ProxySettings {
                mode: ProxyMode::None,
                ..Default::default()
            },
            &|_| None,
            None,
        )
    }

    /// The proxy URL with credentials (for HTTP clients; never log it).
    pub fn url(&self) -> Option<&str> {
        self.url.as_deref()
    }

    /// Environment for a child process: Some(value) = set, None = remove (no proxy, so an
    /// inherited variable must not apply either). `NO_PROXY` in the conventional form
    /// (suffixes, CIDR) that uv, httpx and requests understand.
    pub fn child_env(&self) -> Vec<(String, Option<String>)> {
        let mut out: Vec<(String, Option<String>)> = PROXY_VARS
            .iter()
            .map(|k| (k.to_string(), self.url.clone()))
            .collect();
        let np = no_proxy_env(&self.no_proxy);
        out.push(("NO_PROXY".into(), Some(np.clone())));
        out.push(("no_proxy".into(), Some(np)));
        out
    }

    /// Apply [`Self::child_env`] to a command.
    pub fn apply(&self, cmd: &mut std::process::Command) {
        for (k, v) in self.child_env() {
            match v {
                Some(v) => cmd.env(k, v),
                None => cmd.env_remove(k),
            };
        }
    }

    /// Does `host` bypass the proxy? Loopback always does.
    pub fn bypass(&self, host: &str) -> bool {
        let h = host.trim_start_matches('[').trim_end_matches(']');
        h.eq_ignore_ascii_case("localhost")
            || h.parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback())
            || self.no_proxy.iter().any(|p| matches(p, h))
    }

    /// The proxy for an HTTP client: None = direct (also for SOCKS, which the app's own
    /// client cannot speak; uv and Python can).
    pub fn reqwest_proxy(&self) -> Option<reqwest::Proxy> {
        let url = self.url.clone().filter(|u| u.starts_with("http"))?;
        let me = self.clone();
        Some(reqwest::Proxy::custom(move |u: &reqwest::Url| {
            let host = u.host_str()?;
            (!me.bypass(host)).then(|| url.clone())
        }))
    }
}

/// `http://user:pass@host:port` → `http://user:***@host:port`.
pub fn mask(url: &str) -> String {
    match split_userinfo(url) {
        Some((scheme, info, rest)) => match info.split_once(':') {
            Some((user, _)) => format!("{scheme}{user}:{MASK}@{rest}"),
            // a lone userinfo may itself be a token
            None => format!("{scheme}{MASK}@{rest}"),
        },
        None => url.to_string(),
    }
}

/// (`scheme://`, userinfo, `host:port/...`) when the URL has credentials.
fn split_userinfo(url: &str) -> Option<(&str, &str, &str)> {
    let p = url.find("://")? + 3;
    let (scheme, rest) = url.split_at(p);
    let host_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let at = rest[..host_end].rfind('@')?;
    Some((scheme, &rest[..at], &rest[at + 1..]))
}

/// A manual proxy URL: http / https / socks5 / socks5h with a host.
pub fn validate_url(url: &str) -> Result<String, String> {
    let u = url.trim().trim_end_matches('/');
    let ok_scheme = ["http://", "https://", "socks5://", "socks5h://"]
        .iter()
        .any(|s| u.to_ascii_lowercase().starts_with(s));
    let host = u
        .split_once("://")
        .map(|(_, r)| r.rsplit('@').next().unwrap_or(""))
        .unwrap_or("");
    if !ok_scheme || host.is_empty() || host.starts_with(':') {
        return Err(
            "代理地址须为 http://主机:端口、https://… 或 socks5://… / The proxy must be http://host:port, https://… or socks5://host:port"
                .into(),
        );
    }
    Ok(u.to_string())
}

/// URL from a Windows `ProxyServer` value: the `https=` entry, else `http=`, else `socks=`,
/// else the single `host:port` for all protocols. A missing scheme means http.
pub fn parse_proxy_server(s: &str) -> Option<(String, Option<String>)> {
    let s = s.trim();
    if s.is_empty() {
        return None;
    }
    let with_scheme = |v: &str, default: &str| {
        let v = v.trim();
        if v.contains("://") {
            v.to_string()
        } else {
            format!("{default}://{v}")
        }
    };
    if !s.contains('=') {
        return Some((with_scheme(s, "http"), None));
    }
    let entries: Vec<(String, &str)> = s
        .split(';')
        .filter_map(|e| e.split_once('='))
        .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim()))
        .filter(|(_, v)| !v.is_empty())
        .collect();
    let get = |k: &str| entries.iter().find(|(n, _)| n == k).map(|(_, v)| *v);
    if let Some(v) = get("https").or_else(|| get("http")) {
        return Some((with_scheme(v, "http"), None));
    }
    get("socks").map(|v| {
        (
            with_scheme(v, "socks5"),
            Some(
                "Windows 设置的是 SOCKS 代理（按 SOCKS5 使用）/ The Windows proxy is SOCKS (used as SOCKS5)"
                    .to_string(),
            ),
        )
    })
}

/// No-proxy patterns from a comma / semicolon list (`ProxyOverride` uses `;`).
pub fn parse_list(s: &str) -> Vec<String> {
    s.split([',', ';'])
        .map(|p| p.trim().to_string())
        .filter(|p| !p.is_empty())
        .collect()
}

fn ipv4(s: &str) -> Option<[u8; 4]> {
    s.parse::<std::net::Ipv4Addr>().ok().map(|a| a.octets())
}

/// `10.*` / `192.168.*` → (prefix octets, wildcard count).
fn ipv4_glob(p: &str) -> Option<Vec<u8>> {
    let parts: Vec<&str> = p.split('.').collect();
    if parts.len() > 4 || parts.last() != Some(&"*") {
        return None;
    }
    let fixed = &parts[..parts.len() - 1];
    if fixed.is_empty() {
        return None;
    }
    fixed.iter().map(|o| o.parse::<u8>().ok()).collect()
}

/// Does `host` match one no-proxy pattern: `*`, exact name / IP, `*.suffix` / `.suffix`,
/// IPv4 wildcards (`10.*`), ranges (`172.16.*-172.31.*`), CIDR (`10.0.0.0/8`), `<local>`
/// (a name without a dot).
pub fn matches(pattern: &str, host: &str) -> bool {
    let p = pattern.trim().to_ascii_lowercase();
    let h = host.trim().trim_end_matches('.').to_ascii_lowercase();
    if p.is_empty() || h.is_empty() {
        return false;
    }
    if p == "*" {
        return true;
    }
    if p == "<local>" {
        return !h.contains('.') && !h.contains(':');
    }
    if let Some((lo, hi)) = p.split_once('-') {
        if let (Some(lo), Some(hi), Some(ip)) = (ipv4_glob(lo), ipv4_glob(hi), ipv4(&h)) {
            if lo.len() == hi.len() {
                let n = lo.len();
                return ip[..n] >= lo[..] && ip[..n] <= hi[..];
            }
        }
        return false;
    }
    if let Some((net, bits)) = p.split_once('/') {
        if let (Some(net), Ok(bits), Some(ip)) = (ipv4(net), bits.parse::<u32>(), ipv4(&h)) {
            if bits <= 32 {
                let m = if bits == 0 {
                    0
                } else {
                    u32::MAX << (32 - bits)
                };
                return u32::from_be_bytes(net) & m == u32::from_be_bytes(ip) & m;
            }
        }
        return false;
    }
    if let Some(prefix) = ipv4_glob(&p) {
        return ipv4(&h).is_some_and(|ip| ip[..prefix.len()] == prefix[..]);
    }
    if let Some(suffix) = p.strip_prefix("*.").or_else(|| p.strip_prefix('.')) {
        return h == suffix || h.ends_with(&format!(".{suffix}"));
    }
    if p.contains('*') {
        // other globs (e.g. `*.corp*`): simple prefix / suffix wildcard
        let (a, b) = p.split_once('*').unwrap_or((&p, ""));
        return h.starts_with(a) && h.ends_with(b.trim_start_matches('*'));
    }
    h == p
}

/// `NO_PROXY` value in the form uv / httpx / requests understand: IPv4 wildcards and ranges
/// become CIDR blocks, `*.x` becomes `.x`, `<local>` is dropped (no equivalent).
pub fn no_proxy_env(patterns: &[String]) -> String {
    let mut out: Vec<String> = Vec::new();
    let mut push = |s: String| {
        if !out.contains(&s) {
            out.push(s);
        }
    };
    for p in patterns {
        let p = p.trim();
        if p == "<local>" {
            continue;
        }
        if let Some((lo, hi)) = p.split_once('-') {
            if let (Some(lo), Some(hi)) = (ipv4_glob(lo), ipv4_glob(hi)) {
                // 172.16.*-172.31.* → 172.16.0.0/12 when it is one aligned block, else /16s
                if lo.len() == 2 && hi.len() == 2 && lo[0] == hi[0] && lo[1] <= hi[1] {
                    let span = u32::from(hi[1] - lo[1]) + 1;
                    if span.is_power_of_two() && u32::from(lo[1]) % span == 0 {
                        push(format!(
                            "{}.{}.0.0/{}",
                            lo[0],
                            lo[1],
                            16 - span.trailing_zeros()
                        ));
                    } else {
                        for b in lo[1]..=hi[1] {
                            push(format!("{}.{b}.0.0/16", lo[0]));
                        }
                    }
                    continue;
                }
            }
            continue;
        }
        if let Some(prefix) = ipv4_glob(p) {
            let mut o = prefix.clone();
            o.resize(4, 0);
            push(format!(
                "{}.{}.{}.{}/{}",
                o[0],
                o[1],
                o[2],
                o[3],
                prefix.len() * 8
            ));
            continue;
        }
        if let Some(s) = p.strip_prefix("*.") {
            push(format!(".{s}"));
            continue;
        }
        push(p.to_string());
    }
    for l in ["localhost", "127.0.0.1", "::1"] {
        push(l.to_string());
    }
    out.join(",")
}

/// The effective proxy for `settings`, given the app process environment and the Windows
/// settings.
pub fn resolve(
    settings: &ProxySettings,
    env: &dyn Fn(&str) -> Option<String>,
    system: Option<SystemProxy>,
) -> Resolved {
    let mut no_proxy = parse_list(&settings.no_proxy);
    let mut warnings = Vec::new();
    let mut pac_url = None;
    let (source, url) = match settings.mode {
        ProxyMode::None => (ProxySource::None, None),
        ProxyMode::Manual => match settings.url.as_deref().map(validate_url) {
            Some(Ok(u)) => (ProxySource::Manual, Some(u)),
            Some(Err(e)) => {
                warnings.push(e);
                (ProxySource::None, None)
            }
            None => {
                warnings.push("未填写代理地址 / No proxy address set".into());
                (ProxySource::None, None)
            }
        },
        ProxyMode::System => {
            let var = |k: &str| {
                env(k)
                    .or_else(|| env(&k.to_ascii_lowercase()))
                    .filter(|v| !v.trim().is_empty())
            };
            if let Some(u) = var("HTTPS_PROXY")
                .or_else(|| var("HTTP_PROXY"))
                .or_else(|| var("ALL_PROXY"))
            {
                if let Some(np) = var("NO_PROXY") {
                    no_proxy.extend(parse_list(&np));
                }
                let u = if u.contains("://") {
                    u.trim().to_string()
                } else {
                    format!("http://{}", u.trim())
                };
                (ProxySource::Env, Some(u))
            } else {
                let sys = system.unwrap_or_default();
                pac_url = sys.pac.clone().filter(|p| !p.trim().is_empty());
                if pac_url.is_some() {
                    warnings.push(
                        "Windows 使用自动配置脚本（PAC），本应用不支持；请改用手动代理 / \
                         Windows uses an automatic configuration script (PAC), which is not supported: choose a manual proxy"
                            .into(),
                    );
                }
                match sys
                    .server
                    .as_deref()
                    .filter(|_| sys.enabled)
                    .and_then(parse_proxy_server)
                {
                    Some((u, w)) => {
                        warnings.extend(w);
                        if let Some(o) = sys.overrides.as_deref() {
                            no_proxy.extend(parse_list(o));
                        }
                        (ProxySource::System, Some(u))
                    }
                    None => (ProxySource::None, None),
                }
            }
        }
    };
    let mut seen = Vec::new();
    no_proxy.retain(|p| {
        let k = p.to_ascii_lowercase();
        let new = !seen.contains(&k);
        seen.push(k);
        new
    });
    Resolved {
        effective: EffectiveProxy {
            source,
            url: url.as_deref().map(mask),
            no_proxy: no_proxy.join(","),
            pac_url,
            warnings,
        },
        url,
        no_proxy,
    }
}

/// The current user's Windows proxy settings.
#[cfg(windows)]
pub fn read_system() -> Option<SystemProxy> {
    use windows::core::{HSTRING, PCWSTR};
    use windows::Win32::System::Registry::{
        RegGetValueW, HKEY_CURRENT_USER, RRF_RT_REG_DWORD, RRF_RT_REG_SZ,
    };
    let key = HSTRING::from(r"Software\Microsoft\Windows\CurrentVersion\Internet Settings");
    let sz = |name: &str| -> Option<String> {
        let mut buf = vec![0u16; 4096];
        let mut size = (buf.len() * 2) as u32;
        // SAFETY: buffer and size describe `buf`; RegGetValueW writes at most `size` bytes.
        let r = unsafe {
            RegGetValueW(
                HKEY_CURRENT_USER,
                PCWSTR(key.as_ptr()),
                &HSTRING::from(name),
                RRF_RT_REG_SZ,
                None,
                Some(buf.as_mut_ptr().cast()),
                Some(&mut size),
            )
        };
        if r.is_err() {
            return None;
        }
        let n = (size as usize / 2).min(buf.len());
        let s = String::from_utf16_lossy(&buf[..n]);
        Some(s.trim_end_matches('\0').to_string())
    };
    let mut enable: u32 = 0;
    let mut size = 4u32;
    // SAFETY: a DWORD value into a u32.
    let ok = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            PCWSTR(key.as_ptr()),
            &HSTRING::from("ProxyEnable"),
            RRF_RT_REG_DWORD,
            None,
            Some((&mut enable as *mut u32).cast()),
            Some(&mut size),
        )
    }
    .is_ok();
    Some(SystemProxy {
        enabled: ok && enable != 0,
        server: sz("ProxyServer"),
        overrides: sz("ProxyOverride"),
        pac: sz("AutoConfigURL"),
    })
}

#[cfg(not(windows))]
pub fn read_system() -> Option<SystemProxy> {
    None
}

/// `proxy.json` in the app data dir.
pub struct ProxyStore {
    path: PathBuf,
    settings: Mutex<ProxySettings>,
}

impl ProxyStore {
    pub fn open(dir: &Path) -> Arc<Self> {
        let path = dir.join("proxy.json");
        let settings = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        Arc::new(Self {
            path,
            settings: Mutex::new(settings),
        })
    }

    /// Stored settings with the real URL (internal).
    pub fn settings(&self) -> ProxySettings {
        crate::util::lock(&self.settings).clone()
    }

    pub fn resolve(&self) -> Resolved {
        resolve(&self.settings(), &|k| std::env::var(k).ok(), read_system())
    }

    pub fn resolve_with(&self, s: &ProxySettings) -> Resolved {
        resolve(s, &|k| std::env::var(k).ok(), read_system())
    }

    pub fn state(&self) -> ProxyState {
        let mut settings = self.settings();
        settings.url = settings.url.as_deref().map(mask);
        ProxyState {
            settings,
            effective: self.resolve().effective,
        }
    }

    /// A `url` equal to the masked form of the stored one (same scheme, user, host and port,
    /// password `***`) means "keep the stored password"; any other URL is taken as given.
    pub fn unmask(&self, mut s: ProxySettings) -> ProxySettings {
        let norm = |u: &str| u.trim().trim_end_matches('/').to_string();
        if let (Some(u), Some(old)) = (s.url.as_deref(), self.settings().url) {
            if split_userinfo(&old).is_some() && norm(u) == norm(&mask(&old)) {
                s.url = Some(old);
            }
        }
        s
    }

    pub fn set(&self, s: ProxySettings) -> Result<ProxyState, String> {
        let mut s = self.unmask(s);
        s.url = s
            .url
            .as_deref()
            .map(str::trim)
            .filter(|u| !u.is_empty())
            .map(validate_url)
            .transpose()?;
        if s.mode == ProxyMode::Manual && s.url.is_none() {
            return Err("手动代理需要地址 / A manual proxy needs an address".into());
        }
        if s.no_proxy.trim().is_empty() {
            s.no_proxy = DEFAULT_NO_PROXY.into();
        }
        let body = serde_json::to_vec_pretty(&s).map_err(|e| e.to_string())?;
        crate::util::write_atomic_private(&self.path, &body)
            .map_err(|e| format!("cannot store the proxy settings: {e}"))?;
        log::info!(
            "proxy settings: {:?} {}",
            s.mode,
            s.url.as_deref().map(mask).unwrap_or_default()
        );
        *crate::util::lock(&self.settings) = s;
        Ok(self.state())
    }
}

/// Hosts the managed setup and the backend need.
pub const TEST_TARGETS: [(&str, &str); 3] = [
    ("PyPI", "https://pypi.org/simple/pip/"),
    (
        "Python (GitHub)",
        "https://github.com/astral-sh/python-build-standalone/releases",
    ),
    ("Hugging Face", "https://huggingface.co/api/models?limit=1"),
];

/// Reach the package index, the Python download host and the model hub through `r`.
pub fn test(r: &Resolved) -> ProxyTest {
    test_targets(r, &TEST_TARGETS)
}

pub fn test_targets(r: &Resolved, targets: &[(&str, &str)]) -> ProxyTest {
    if r.url().is_some_and(|u| u.starts_with("socks")) {
        return ProxyTest {
            ok: true,
            message: "SOCKS 代理无法在此测试（uv 与后端会使用它）/ A SOCKS proxy cannot be tested here (uv and the backend will use it)".into(),
        };
    }
    let mut b = reqwest::blocking::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(10))
        .user_agent(concat!("asr-recorder/", env!("CARGO_PKG_VERSION")));
    if let Some(p) = r.reqwest_proxy() {
        b = b.proxy(p);
    }
    let client = match b.build() {
        Ok(c) => c,
        Err(e) => {
            return ProxyTest {
                ok: false,
                message: format!("{e}"),
            }
        }
    };
    let handles: Vec<_> = targets
        .iter()
        .map(|(name, url)| {
            let (c, name, url) = (client.clone(), name.to_string(), url.to_string());
            std::thread::spawn(move || {
                let t = Instant::now();
                let res = c.get(&url).send();
                let ms = t.elapsed().as_millis();
                match res {
                    Ok(resp) if resp.status().as_u16() < 500 => (true, format!("{name} ✓ {ms} ms")),
                    Ok(resp) => (false, format!("{name} ✗ HTTP {}", resp.status().as_u16())),
                    Err(e) => {
                        let why = if e.is_timeout() {
                            "timeout".to_string()
                        } else if e.is_connect() {
                            "connection failed".to_string()
                        } else {
                            crate::uploader::client::redact(&e.to_string())
                        };
                        (false, format!("{name} ✗ {why}"))
                    }
                }
            })
        })
        .collect();
    let results: Vec<(bool, String)> = handles
        .into_iter()
        .map(|h| h.join().unwrap_or((false, "panicked".into())))
        .collect();
    let via = match r.effective.url.as_deref() {
        Some(u) => format!("via {u}"),
        None => "direct".into(),
    };
    ProxyTest {
        ok: results.iter().all(|(ok, _)| *ok),
        message: format!(
            "{} ({via})",
            results
                .iter()
                .map(|(_, m)| m.as_str())
                .collect::<Vec<_>>()
                .join("; ")
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings(mode: ProxyMode, url: Option<&str>) -> ProxySettings {
        ProxySettings {
            mode,
            url: url.map(String::from),
            no_proxy: DEFAULT_NO_PROXY.into(),
        }
    }

    #[test]
    fn registry_strings() {
        assert_eq!(
            parse_proxy_server("127.0.0.1:1081"),
            Some(("http://127.0.0.1:1081".into(), None))
        );
        assert_eq!(
            parse_proxy_server("http=proxy:80;https=sproxy:443;ftp=f:21"),
            Some(("http://sproxy:443".into(), None))
        );
        assert_eq!(
            parse_proxy_server("http=proxy:80"),
            Some(("http://proxy:80".into(), None))
        );
        let (u, w) = parse_proxy_server("socks=127.0.0.1:1080").unwrap();
        assert_eq!(u, "socks5://127.0.0.1:1080");
        assert!(w.is_some());
        assert_eq!(parse_proxy_server("  "), None);
        assert_eq!(
            parse_list("localhost;127.*;<local>;*.corp"),
            vec!["localhost", "127.*", "<local>", "*.corp"]
        );
    }

    #[test]
    fn no_proxy_matching() {
        let r = resolve(
            &settings(ProxyMode::Manual, Some("http://p:3128")),
            &|_| None,
            None,
        );
        for h in [
            "localhost",
            "127.0.0.1",
            "[::1]",
            "10.1.2.3",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.10",
            "nas.local",
        ] {
            assert!(r.bypass(h), "{h} should bypass");
        }
        for h in ["172.32.0.1", "172.15.0.1", "8.8.8.8", "pypi.org", "nas"] {
            assert!(!r.bypass(h), "{h} should use the proxy");
        }
        assert!(matches("<local>", "nas"));
        assert!(!matches("<local>", "nas.lan"));
        assert!(matches("*.corp", "a.b.corp") && matches(".corp", "corp"));
        assert!(matches("10.0.0.0/8", "10.9.9.9") && !matches("10.0.0.0/8", "11.0.0.1"));
        assert!(matches("*", "anything"));
        assert!(matches("example.com", "EXAMPLE.com."));
    }

    #[test]
    fn no_proxy_env_is_conventional() {
        let env = no_proxy_env(&parse_list(DEFAULT_NO_PROXY));
        assert_eq!(
            env,
            "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.local"
        );
        assert_eq!(
            no_proxy_env(&parse_list("<local>,172.16.*-172.18.*")),
            "172.16.0.0/16,172.17.0.0/16,172.18.0.0/16,localhost,127.0.0.1,::1"
        );
    }

    #[test]
    fn env_wins_over_the_registry_in_system_mode() {
        let sys = SystemProxy {
            enabled: true,
            server: Some("regproxy:8080".into()),
            overrides: Some("*.intra;<local>".into()),
            pac: None,
        };
        let env = |k: &str| match k {
            "https_proxy" => Some("http://127.0.0.1:1081".to_string()),
            "NO_PROXY" => Some("internal.example".to_string()),
            _ => None,
        };
        let r = resolve(&ProxySettings::default(), &env, Some(sys.clone()));
        assert_eq!(r.effective.source, ProxySource::Env);
        assert_eq!(r.url(), Some("http://127.0.0.1:1081"));
        assert!(r.bypass("internal.example"));
        // without the variable the registry applies, with its overrides
        let r = resolve(&ProxySettings::default(), &|_| None, Some(sys.clone()));
        assert_eq!(r.effective.source, ProxySource::System);
        assert_eq!(r.url(), Some("http://regproxy:8080"));
        assert!(r.bypass("x.intra") && r.bypass("printer"));
        // disabled in Windows: direct; a PAC script is reported with a warning
        let r = resolve(
            &ProxySettings::default(),
            &|_| None,
            Some(SystemProxy {
                enabled: false,
                pac: Some("http://wpad/proxy.pac".into()),
                ..sys
            }),
        );
        assert_eq!(r.effective.source, ProxySource::None);
        assert_eq!(
            r.effective.pac_url.as_deref(),
            Some("http://wpad/proxy.pac")
        );
        assert_eq!(r.effective.warnings.len(), 1);
        // mode none ignores everything, and children lose inherited variables
        let r = resolve(&settings(ProxyMode::None, None), &env, None);
        assert_eq!((r.effective.source, r.url()), (ProxySource::None, None));
        let ce = r.child_env();
        assert!(ce
            .iter()
            .filter(|(k, _)| k.ends_with("_PROXY") && k != "NO_PROXY")
            .all(|(_, v)| v.is_none()));
    }

    #[test]
    fn child_env_carries_the_proxy() {
        let r = resolve(
            &settings(ProxyMode::Manual, Some("http://u:p@proxy:3128")),
            &|_| None,
            None,
        );
        let ce = r.child_env();
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "https_proxy"] {
            let v = ce.iter().find(|(n, _)| n == k).unwrap().1.clone();
            assert_eq!(v.as_deref(), Some("http://u:p@proxy:3128"), "{k}");
        }
        assert!(ce
            .iter()
            .any(|(k, v)| k == "no_proxy" && v.as_deref().unwrap().contains("192.168.0.0/16")));
        assert_eq!(r.effective.url.as_deref(), Some("http://u:***@proxy:3128"));
        assert!(r.reqwest_proxy().is_some());
        let s = resolve(
            &settings(ProxyMode::Manual, Some("socks5://h:1080")),
            &|_| None,
            None,
        );
        assert!(s.reqwest_proxy().is_none());
    }

    #[test]
    fn credentials_are_stored_but_masked() {
        let dir = tempfile::tempdir().unwrap();
        let st = ProxyStore::open(dir.path());
        assert_eq!(st.settings(), ProxySettings::default());
        assert!(st
            .set(settings(ProxyMode::Manual, Some("ftp://x")))
            .is_err());
        assert!(st.set(settings(ProxyMode::Manual, None)).is_err());
        let out = st
            .set(settings(
                ProxyMode::Manual,
                Some("http://alice:s3cret@proxy:3128"),
            ))
            .unwrap();
        assert_eq!(
            out.settings.url.as_deref(),
            Some("http://alice:***@proxy:3128")
        );
        assert_eq!(
            out.effective.url.as_deref(),
            Some("http://alice:***@proxy:3128")
        );
        assert!(!format!("{out:?}").contains("s3cret"));
        // the masked URL coming back keeps the stored credentials
        st.set(out.settings.clone()).unwrap();
        assert_eq!(
            ProxyStore::open(dir.path()).settings().url.as_deref(),
            Some("http://alice:s3cret@proxy:3128")
        );
        // a test draft in the masked form uses the stored password too
        let draft = st.unmask(settings(
            ProxyMode::Manual,
            Some("http://alice:***@proxy:3128/"),
        ));
        assert_eq!(draft.url.as_deref(), Some("http://alice:s3cret@proxy:3128"));
        // another user / host is taken as given
        st.set(settings(
            ProxyMode::Manual,
            Some("http://bob:***@proxy:3128"),
        ))
        .unwrap();
        assert_eq!(
            st.settings().url.as_deref(),
            Some("http://bob:***@proxy:3128")
        );
        st.set(settings(ProxyMode::Manual, Some("http://other:8080")))
            .unwrap();
        assert_eq!(st.settings().url.as_deref(), Some("http://other:8080"));
        assert_eq!(mask("http://tok@p:1"), "http://***@p:1");
        assert_eq!(mask("http://p:3128"), "http://p:3128");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let m = std::fs::metadata(dir.path().join("proxy.json")).unwrap();
            assert_eq!(m.permissions().mode() & 0o777, 0o600);
        }
    }

    #[cfg(windows)]
    #[test]
    fn reads_the_windows_settings() {
        let s = read_system().expect("Internet Settings key");
        eprintln!("windows proxy settings: {s:?}");
        let r = resolve(&ProxySettings::default(), &|_| None, Some(s));
        eprintln!("resolved without env: {:?}", r.effective);
    }

    #[test]
    fn proxy_test_reports_unreachable_hosts_quickly() {
        // a closed local port as the "proxy": every target fails fast
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let r = resolve(
            &settings(ProxyMode::Manual, Some(&format!("http://127.0.0.1:{port}"))),
            &|_| None,
            None,
        );
        let t = Instant::now();
        let out = test_targets(&r, &[("Example", "https://example.invalid/")]);
        assert!(!out.ok, "{out:?}");
        assert!(out.message.contains("Example ✗"), "{}", out.message);
        assert!(t.elapsed() < Duration::from_secs(15));
    }
}
