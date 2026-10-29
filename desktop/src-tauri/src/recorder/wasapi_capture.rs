//! WASAPI capture: shared-mode loopback of a render endpoint, or a capture endpoint, polled.
//! Packets carry the QPC timestamp WASAPI reports for their first frame.

use std::sync::OnceLock;

use wasapi::{
    AudioCaptureClient, AudioClient, DeviceEnumerator, Direction, Handle, SampleType, SessionState,
    StreamMode, WaveFormat,
};
use windows::Win32::System::Performance::{QueryPerformanceCounter, QueryPerformanceFrequency};

use super::capture::{CaptureSource, Packet, SourceKind};
use crate::ipc::{AudioProcess, Device, DeviceList};

/// QPC in seconds; the same clock (in 100 ns units) as WASAPI's `u64QPCPosition`.
pub fn qpc_now() -> f64 {
    static FREQ: OnceLock<i64> = OnceLock::new();
    let freq = *FREQ.get_or_init(|| {
        let mut f = 0i64;
        // SAFETY: plain out-parameter call.
        unsafe { QueryPerformanceFrequency(&mut f).ok() };
        f.max(1)
    });
    let mut c = 0i64;
    // SAFETY: plain out-parameter call.
    unsafe { QueryPerformanceCounter(&mut c).ok() };
    // Split to keep precision for large counter values.
    (c / freq) as f64 + (c % freq) as f64 / freq as f64
}

#[derive(Clone, Copy)]
enum Fmt {
    F32,
    I16,
    I24,
    I32,
}

pub struct WasapiSource {
    // Field order matters for drop: stop the stream before releasing the client.
    capture: AudioCaptureClient,
    client: AudioClient,
    rate: f64,
    channels: usize,
    fmt: Fmt,
    block: usize,
    raw: Vec<u8>,
    device_id: Option<String>,
    /// Event handle of event-mode clients (process loopback); kept alive, never waited on.
    _event: Option<Handle>,
}

// SAFETY: the COM objects are created on the recorder thread (MTA) and only used there.
unsafe impl Send for WasapiSource {}

fn e2s(e: impl std::fmt::Display) -> String {
    e.to_string()
}

impl WasapiSource {
    pub fn open(kind: SourceKind, device: Option<&str>) -> Result<Self, String> {
        let en = DeviceEnumerator::new().map_err(e2s)?;
        let dir = match kind {
            SourceKind::System => Direction::Render,
            SourceKind::Mic => Direction::Capture,
        };
        let dev = match device {
            Some(id) => en.get_device(id).map_err(e2s)?,
            None => en.get_default_device(&dir).map_err(e2s)?,
        };
        let mut client = dev.get_iaudioclient().map_err(e2s)?;
        let format = client.get_mixformat().map_err(e2s)?;
        let bits = format.get_bitspersample();
        let valid = format.get_validbitspersample();
        let fmt = match (format.get_subformat().map_err(e2s)?, bits) {
            (SampleType::Float, 32) => Fmt::F32,
            (SampleType::Int, 16) => Fmt::I16,
            (SampleType::Int, 24) => Fmt::I24,
            (SampleType::Int, 32) => Fmt::I32,
            (t, b) => return Err(format!("unsupported mix format {t:?} {b}/{valid} bit")),
        };
        let mode = StreamMode::PollingShared {
            autoconvert: false,
            buffer_duration_hns: 10_000_000, // 1 s of slack for a stalled recorder thread
        };
        // Loopback = capture direction on a render device.
        client
            .initialize_client(&format, &Direction::Capture, &mode)
            .map_err(e2s)?;
        let capture = client.get_audiocaptureclient().map_err(e2s)?;
        client.start_stream().map_err(e2s)?;
        let name = dev.get_friendlyname().unwrap_or_default();
        let device_id = dev.get_id().ok();
        log::info!(
            "opened {kind:?} '{name}': {} Hz, {} ch, {bits} bit",
            format.get_samplespersec(),
            format.get_nchannels()
        );
        Ok(Self {
            capture,
            client,
            rate: format.get_samplespersec() as f64,
            channels: format.get_nchannels() as usize,
            fmt,
            block: format.get_blockalign() as usize,
            raw: Vec::new(),
            device_id,
            _event: None,
        })
    }

    /// Process loopback (Windows 10 2004+): only the audio rendered by `pid` and its child
    /// processes, on whatever device they play. The engine converts it to 48 kHz float stereo.
    pub fn open_process(pid: u32) -> Result<Self, String> {
        let mut client = AudioClient::new_application_loopback_client(pid, true)
            .map_err(|e| format!("process loopback for pid {pid}: {e}"))?;
        let format = WaveFormat::new(32, 32, &SampleType::Float, 48000, 2, None);
        // The process-loopback virtual device only supports event-driven streams; the event
        // handle must exist before Start, but the recorder keeps polling.
        let mode = StreamMode::EventsShared {
            autoconvert: true,
            buffer_duration_hns: 10_000_000,
        };
        client
            .initialize_client(&format, &Direction::Capture, &mode)
            .map_err(e2s)?;
        let event = client.set_get_eventhandle().map_err(e2s)?;
        let capture = client.get_audiocaptureclient().map_err(e2s)?;
        client.start_stream().map_err(e2s)?;
        log::info!("opened process loopback for pid {pid} (tree): 48000 Hz, 2 ch, float");
        Ok(Self {
            capture,
            client,
            rate: 48000.0,
            channels: 2,
            fmt: Fmt::F32,
            block: format.get_blockalign() as usize,
            raw: Vec::new(),
            device_id: None,
            _event: Some(event),
        })
    }

    fn downmix(&self, frames: usize, silent: bool) -> Vec<f32> {
        if silent {
            return vec![0.0; frames];
        }
        let ch = self.channels;
        let scale = 1.0 / ch as f32;
        let mut out = Vec::with_capacity(frames);
        let bps = self.block / ch;
        for f in 0..frames {
            let base = f * self.block;
            let mut acc = 0.0f32;
            for c in 0..ch {
                let o = base + c * bps;
                let b = &self.raw[o..o + bps];
                acc += match self.fmt {
                    Fmt::F32 => f32::from_le_bytes([b[0], b[1], b[2], b[3]]),
                    Fmt::I16 => i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0,
                    Fmt::I24 => {
                        (i32::from_le_bytes([0, b[0], b[1], b[2]]) >> 8) as f32 / 8_388_608.0
                    }
                    Fmt::I32 => {
                        i32::from_le_bytes([b[0], b[1], b[2], b[3]]) as f32 / 2_147_483_648.0
                    }
                };
            }
            out.push(acc * scale);
        }
        out
    }
}

impl CaptureSource for WasapiSource {
    fn rate(&self) -> f64 {
        self.rate
    }

    fn device_id(&self) -> Option<String> {
        self.device_id.clone()
    }

    fn poll(&mut self, out: &mut Vec<Packet>) -> Result<(), String> {
        loop {
            let next = self
                .capture
                .get_next_packet_size()
                .map_err(e2s)?
                .unwrap_or(0) as usize;
            if next == 0 {
                return Ok(());
            }
            let need = next * self.block;
            if self.raw.len() < need {
                self.raw.resize(need, 0);
            }
            let (frames, info) = self.capture.read_from_device(&mut self.raw).map_err(e2s)?;
            let frames = frames as usize;
            if frames == 0 {
                return Ok(());
            }
            let t = if info.timestamp == 0 || info.flags.timestamp_error {
                qpc_now() - frames as f64 / self.rate
            } else {
                info.timestamp as f64 * 1e-7
            };
            let samples = self.downmix(frames, info.flags.silent);
            out.push(Packet { t, samples });
        }
    }
}

impl Drop for WasapiSource {
    fn drop(&mut self) {
        let _ = self.client.stop_stream();
    }
}

fn list(en: &DeviceEnumerator, dir: Direction) -> Result<Vec<Device>, String> {
    let default_id = en
        .get_default_device(&dir)
        .ok()
        .and_then(|d| d.get_id().ok());
    let coll = en.get_device_collection(&dir).map_err(e2s)?;
    let n = coll.get_nbr_devices().map_err(e2s)?;
    let mut out = Vec::new();
    for i in 0..n {
        let Ok(d) = coll.get_device_at_index(i) else {
            continue;
        };
        let Ok(id) = d.get_id() else { continue };
        let name = d.get_friendlyname().unwrap_or_else(|_| id.clone());
        out.push(Device {
            is_default: default_id.as_deref() == Some(id.as_str()),
            id,
            name,
        });
    }
    Ok(out)
}

pub fn devices() -> Result<DeviceList, String> {
    let en = DeviceEnumerator::new().map_err(e2s)?;
    Ok(DeviceList {
        outputs: list(&en, Direction::Render)?,
        inputs: list(&en, Direction::Capture)?,
    })
}

fn direction(kind: SourceKind) -> Direction {
    match kind {
        SourceKind::System => Direction::Render,
        SourceKind::Mic => Direction::Capture,
    }
}

pub fn default_device_id(kind: SourceKind) -> Option<String> {
    let en = DeviceEnumerator::new().ok()?;
    en.get_default_device(&direction(kind)).ok()?.get_id().ok()
}

fn process_name(pid: u32) -> Option<String> {
    use windows::core::PWSTR;
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };
    // SAFETY: plain Win32 calls with a local buffer; the handle is closed below.
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let ok =
            QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut len);
        let _ = CloseHandle(h);
        ok.ok()?;
        let path = String::from_utf16_lossy(&buf[..len as usize]);
        Some(path.rsplit(['\\', '/']).next().unwrap_or(&path).to_string())
    }
}

/// Visible, unowned top-level window titles per pid.
fn window_titles() -> std::collections::HashMap<u32, String> {
    use windows::core::BOOL;
    use windows::Win32::Foundation::{HWND, LPARAM};
    use windows::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetWindow, GetWindowTextLengthW, GetWindowTextW, GetWindowThreadProcessId,
        IsWindowVisible, GW_OWNER,
    };
    unsafe extern "system" fn cb(hwnd: HWND, lp: LPARAM) -> BOOL {
        // SAFETY: lp is the &mut HashMap passed to EnumWindows below, alive for the call.
        let map = unsafe { &mut *(lp.0 as *mut std::collections::HashMap<u32, String>) };
        unsafe {
            if !IsWindowVisible(hwnd).as_bool()
                || GetWindow(hwnd, GW_OWNER).is_ok_and(|o| !o.is_invalid())
            {
                return true.into();
            }
            let n = GetWindowTextLengthW(hwnd);
            if n <= 0 {
                return true.into();
            }
            let mut buf = vec![0u16; n as usize + 1];
            let got = GetWindowTextW(hwnd, &mut buf);
            let mut pid = 0u32;
            GetWindowThreadProcessId(hwnd, Some(&mut pid));
            if got > 0 && pid != 0 {
                map.entry(pid)
                    .or_insert_with(|| String::from_utf16_lossy(&buf[..got as usize]));
            }
        }
        true.into()
    }
    let mut map = std::collections::HashMap::new();
    // SAFETY: the callback only touches `map` through the pointer while EnumWindows runs.
    unsafe {
        let _ = EnumWindows(Some(cb), LPARAM(&mut map as *mut _ as isize));
    }
    map
}

/// Processes with a non-expired audio session on any active render device, active ones first.
pub fn processes() -> Result<Vec<AudioProcess>, String> {
    let en = DeviceEnumerator::new().map_err(e2s)?;
    let coll = en.get_device_collection(&Direction::Render).map_err(e2s)?;
    let own = std::process::id();
    let mut found: Vec<(u32, bool)> = Vec::new();
    for i in 0..coll.get_nbr_devices().map_err(e2s)? {
        let Ok(dev) = coll.get_device_at_index(i) else {
            continue;
        };
        let Ok(mgr) = dev.get_iaudiosessionmanager() else {
            continue;
        };
        let Ok(sessions) = mgr.get_audiosessionenumerator() else {
            continue;
        };
        for j in 0..sessions.get_count().unwrap_or(0) {
            let Ok(s) = sessions.get_session(j) else {
                continue;
            };
            let Ok(pid) = s.get_process_id() else {
                continue;
            };
            let state = s.get_state().unwrap_or(SessionState::Expired);
            if pid == 0 || pid == own || state == SessionState::Expired {
                continue;
            }
            let active = state == SessionState::Active;
            match found.iter_mut().find(|(p, _)| *p == pid) {
                Some(f) => f.1 |= active,
                None => found.push((pid, active)),
            }
        }
    }
    let titles = window_titles();
    let mut out: Vec<(bool, AudioProcess)> = found
        .into_iter()
        .map(|(pid, active)| {
            (
                active,
                AudioProcess {
                    pid,
                    name: process_name(pid).unwrap_or_else(|| format!("pid {pid}")),
                    title: titles.get(&pid).cloned(),
                },
            )
        })
        .collect();
    out.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then_with(|| a.1.name.to_lowercase().cmp(&b.1.name.to_lowercase()))
    });
    Ok(out.into_iter().map(|(_, p)| p).collect())
}

pub fn process_exists(pid: u32) -> bool {
    use windows::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    // SAFETY: plain Win32 calls; the handle is closed below.
    unsafe {
        let Ok(h) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) else {
            return false;
        };
        let mut code = 0u32;
        let ok = GetExitCodeProcess(h, &mut code).is_ok();
        let _ = CloseHandle(h);
        ok && code == STILL_ACTIVE.0 as u32
    }
}
