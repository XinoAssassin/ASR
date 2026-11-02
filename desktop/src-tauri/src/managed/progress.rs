//! Turns `uv` output into `ManagedProgress` (phase, best-effort overall percent, redacted line).
//!
//! uv prints plain lines when its output is not a terminal, e.g.
//!
//! ```text
//! Downloading cpython-3.12.12-windows-x86_64-none (download) (21.1MiB)
//! Installed Python 3.12.12 in 6.21s
//! Using CPython 3.12.12
//! Creating virtual environment at: C:\...\.venv
//! Downloading torch (198.9MiB)
//!  Downloaded torch
//! Prepared 109 packages in 1m 02s
//! Installed 109 packages in 21.3s
//!  + torch==2.14.1+cpu
//! ```
//!
//! Percent: each phase owns a slice of 0..100; inside `packages` the downloads announced so far
//! are weighted by their size. It never goes backwards.

use crate::ipc::{ManagedPhase, ManagedProgress};

/// Overall percent at the start / end of each phase.
pub fn phase_range(phase: ManagedPhase) -> (f64, f64) {
    match phase {
        ManagedPhase::Prepare => (0.0, 3.0),
        ManagedPhase::Python => (3.0, 12.0),
        ManagedPhase::Packages => (12.0, 90.0),
        ManagedPhase::Finalize => (90.0, 99.0),
        ManagedPhase::Done => (100.0, 100.0),
        ManagedPhase::Failed => (0.0, 0.0),
    }
}

/// Bilingual short status for a phase.
pub fn phase_message(phase: ManagedPhase) -> &'static str {
    match phase {
        ManagedPhase::Prepare => "正在准备 / Preparing",
        ManagedPhase::Python => "正在安装 Python 3.12 / Installing Python 3.12",
        ManagedPhase::Packages => "正在下载并安装依赖 / Downloading and installing packages",
        ManagedPhase::Finalize => "正在验证环境 / Verifying the environment",
        ManagedPhase::Done => "本地后端已就绪 / Local backend ready",
        ManagedPhase::Failed => "安装失败 / Setup failed",
    }
}

/// A size such as `198.9MiB` / `1.2GiB` / `850KiB` in bytes.
pub fn parse_size(s: &str) -> Option<f64> {
    let s = s.trim();
    let split = s.find(|c: char| c.is_ascii_alphabetic())?;
    let (num, unit) = s.split_at(split);
    let n: f64 = num.trim().parse().ok()?;
    let mult = match unit.trim() {
        "B" => 1.0,
        "KiB" | "kB" | "KB" => 1024.0,
        "MiB" | "MB" => 1024.0 * 1024.0,
        "GiB" | "GB" => 1024.0 * 1024.0 * 1024.0,
        _ => return None,
    };
    Some(n * mult)
}

/// Strip credentials from URLs (`https://user:pass@host` → `https://***@host`), query strings
/// and key-like values, then apply the uploader's redaction.
pub fn redact_line(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    for (i, word) in line.split(' ').enumerate() {
        if i > 0 {
            out.push(' ');
        }
        match word.find("://") {
            Some(p) => {
                let (scheme, rest) = word.split_at(p + 3);
                let host_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
                match rest[..host_end].rfind('@') {
                    Some(at) => {
                        out.push_str(scheme);
                        out.push_str("***");
                        out.push_str(&rest[at..]);
                    }
                    None => out.push_str(word),
                }
            }
            None => out.push_str(word),
        }
    }
    crate::uploader::client::redact(&out)
}

#[derive(Debug, Default)]
pub struct Tracker {
    percent: f64,
    announced: Vec<(String, f64)>,
    downloaded: f64,
    message: Option<String>,
}

impl Tracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn percent(&self) -> f64 {
        self.percent
    }

    fn bump(&mut self, p: f64) {
        if p > self.percent {
            self.percent = p.min(100.0);
        }
    }

    /// Enter a phase (percent jumps to its start).
    pub fn phase(&mut self, phase: ManagedPhase, message: Option<&str>) -> ManagedProgress {
        let (start, _) = phase_range(phase);
        self.bump(start);
        if phase == ManagedPhase::Packages {
            self.announced.clear();
            self.downloaded = 0.0;
        }
        self.message = None;
        ManagedProgress {
            phase,
            message: message.unwrap_or(phase_message(phase)).to_string(),
            percent: Some(self.percent),
            log_line: None,
        }
    }

    /// One output line of the tool running in `phase`.
    pub fn line(&mut self, phase: ManagedPhase, raw: &str) -> ManagedProgress {
        let line = raw.trim_end();
        let t = line.trim();
        let (start, end) = phase_range(phase);
        let at = |f: f64| start + (end - start) * f;
        match phase {
            ManagedPhase::Python => {
                if t.starts_with("Downloading ") {
                    self.bump(at(0.2));
                } else if t.starts_with("Installed Python") || t.contains("already installed") {
                    self.bump(end);
                }
            }
            ManagedPhase::Packages => {
                if t.starts_with("Using CPython") || t.starts_with("Using Python") {
                    self.bump(at(0.01));
                } else if t.starts_with("Creating virtual environment") {
                    self.bump(at(0.02));
                } else if let Some(rest) = t.strip_prefix("Downloading ") {
                    // "Downloading torch (198.9MiB)"
                    let (name, size) = match (rest.rfind(" ("), rest.ends_with(')')) {
                        (Some(i), true) => (&rest[..i], parse_size(&rest[i + 2..rest.len() - 1])),
                        _ => (rest, None),
                    };
                    let name = name.trim().to_string();
                    let size = size.unwrap_or(1024.0 * 1024.0);
                    self.message = Some(format!(
                        "正在下载 {name} / Downloading {name} ({:.0} MiB)",
                        size / 1024.0 / 1024.0
                    ));
                    if !self.announced.iter().any(|(n, _)| *n == name) {
                        self.announced.push((name, size));
                    }
                    self.bump(at(0.03));
                } else if let Some(name) = t.strip_prefix("Downloaded ") {
                    let name = name.trim();
                    if let Some((_, s)) = self.announced.iter().find(|(n, _)| n == name) {
                        self.downloaded += *s;
                    }
                    let total: f64 = self.announced.iter().map(|(_, s)| *s).sum();
                    if total > 0.0 {
                        self.bump(at(0.03 + 0.80 * (self.downloaded / total).min(1.0)));
                    }
                } else if t.starts_with("Prepared ") {
                    self.message = None;
                    self.bump(at(0.85));
                } else if t.starts_with("Uninstalled ") {
                    self.bump(at(0.87));
                } else if t.starts_with("Installed ") || t.starts_with("Audited ") {
                    self.message = None;
                    self.bump(end);
                }
            }
            _ => {}
        }
        ManagedProgress {
            phase,
            message: self
                .message
                .clone()
                .unwrap_or_else(|| phase_message(phase).to_string()),
            percent: Some(self.percent),
            log_line: Some(redact_line(line)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes() {
        assert_eq!(parse_size("1.0KiB"), Some(1024.0));
        assert_eq!(parse_size("2MiB"), Some(2.0 * 1024.0 * 1024.0));
        assert!(parse_size("1.5GiB").unwrap() > 1.6e9);
        assert_eq!(parse_size("lots"), None);
        assert_eq!(parse_size("3 parsecs"), None);
    }

    #[test]
    fn redaction_strips_credentials_and_queries() {
        let r = redact_line(
            "error: Failed to fetch: `https://user:s3cret@mirror.example/simple/torch/?sig=abc`",
        );
        assert!(
            !r.contains("s3cret") && !r.contains("user:") && !r.contains("abc"),
            "{r}"
        );
        assert!(r.contains("***@mirror.example"), "{r}");
        assert_eq!(redact_line(" + torch==2.14.1+cpu"), "+ torch==2.14.1+cpu");
        let r = redact_line("HF token=hf_abcdef");
        assert!(!r.contains("hf_abcdef"), "{r}");
    }

    #[test]
    fn a_real_uv_sync_log_moves_monotonically_to_the_end_of_packages() {
        let log = "\
Using CPython 3.12.12
Creating virtual environment at: C:\\Users\\x\\ASR 录音\\backend\\.venv
Downloading torch (198.9MiB)
Downloading transformers (11.2MiB)
Downloading numpy (12.0MiB)
 Downloaded numpy
 Downloaded transformers
   Building asr @ file:///C:/Users/x/backend/src
      Built asr @ file:///C:/Users/x/backend/src
 Downloaded torch
Prepared 109 packages in 1m 02s
Installed 109 packages in 21.30s
 + accelerate==1.15.0
 + torch==2.14.1+cpu";
        let mut t = Tracker::new();
        let first = t.phase(ManagedPhase::Packages, None);
        assert_eq!(first.percent, Some(12.0));
        let mut last = 12.0;
        let mut saw_torch_msg = false;
        let mut after_numpy = 0.0;
        for l in log.lines() {
            let p = t.line(ManagedPhase::Packages, l);
            let pct = p.percent.unwrap();
            assert!(pct >= last, "{l}: {pct} < {last}");
            last = pct;
            saw_torch_msg |= p.message.contains("Downloading torch (199 MiB)");
            if l.contains("Downloaded numpy") {
                after_numpy = pct;
            }
            assert!(p.log_line.is_some());
        }
        assert!(saw_torch_msg);
        // numpy is small compared with torch: little progress
        assert!(after_numpy < 20.0, "{after_numpy}");
        assert_eq!(last, 90.0);
        // entering a later phase never lowers the percent
        let f = t.phase(ManagedPhase::Finalize, None);
        assert_eq!(f.percent, Some(90.0));
        assert_eq!(f.message, phase_message(ManagedPhase::Finalize));
    }

    #[test]
    fn python_install_lines() {
        let mut t = Tracker::new();
        t.phase(ManagedPhase::Python, None);
        let p = t.line(
            ManagedPhase::Python,
            "Downloading cpython-3.12.12-windows-x86_64-none (download) (21.1MiB)",
        );
        assert!(p.percent.unwrap() > 3.0 && p.percent.unwrap() < 12.0);
        let p = t.line(ManagedPhase::Python, "Installed Python 3.12.12 in 6.21s");
        assert_eq!(p.percent, Some(12.0));
        let mut t = Tracker::new();
        t.phase(ManagedPhase::Python, None);
        let p = t.line(ManagedPhase::Python, "Python 3.12 is already installed");
        assert_eq!(p.percent, Some(12.0));
    }
}
