//! Capture source abstraction. Packets are mono f32 at the device's native rate, stamped with
//! the monotonic clock of [`now`] (seconds).

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceKind {
    /// Loopback of a render (output) device: "what you hear".
    System,
    /// Capture (input) device.
    Mic,
}

pub struct Packet {
    /// Monotonic time of the first sample (seconds).
    pub t: f64,
    pub samples: Vec<f32>,
}

pub trait CaptureSource: Send {
    /// Nominal sample rate of the packets.
    fn rate(&self) -> f64;
    /// Drain every packet the device has ready. An error means the device is gone.
    fn poll(&mut self, out: &mut Vec<Packet>) -> Result<(), String>;
    /// Endpoint id of the opened device (None for process loopback / unknown).
    fn device_id(&self) -> Option<String> {
        None
    }
}

/// Device id meaning "the system default" (listed by backends without real ids, e.g. macOS).
pub const DEFAULT_DEVICE: &str = "default";

/// `device` as an explicit device id; None means the default device (also when the caller
/// passes the listed [`DEFAULT_DEVICE`] entry or an empty string).
pub fn explicit_device(device: Option<&str>) -> Option<&str> {
    device.filter(|d| !d.is_empty() && *d != DEFAULT_DEVICE)
}

/// Fallback packet timestamps for devices that sometimes report none (WASAPI
/// `timestamp == 0` / `timestamp_error`). Packets read back to back in one poll are stamped
/// consecutively, ending at the clock reading taken after the last one, so a backlog that
/// built up during a stall keeps its real spacing (stamping each `now - len` would pile it onto
/// one end time and the timeline would drop all but the last packet). Across polls the stamps
/// continue from the previous end and re-anchor to the clock only when they are off by more
/// than about two packet periods (a real gap, e.g. idle loopback).
#[derive(Debug, Default)]
pub struct BatchStamper {
    prev_end: Option<f64>,
}

impl BatchStamper {
    /// `packets[i]` = (frames, device timestamp if valid) in read order; `now` was read after
    /// the last packet. Returns the start time of each packet.
    pub fn stamp(&mut self, now: f64, rate: f64, packets: &[(usize, Option<f64>)]) -> Vec<f64> {
        let mut after: usize = packets.iter().map(|p| p.0).sum();
        let mut out = Vec::with_capacity(packets.len());
        for &(frames, device_t) in packets {
            let len_s = frames as f64 / rate;
            let est = now - after as f64 / rate;
            after -= frames;
            let t = match (device_t, self.prev_end) {
                (Some(t), _) => t,
                (None, Some(p)) if (est - p).abs() <= 2.0 * len_s.max(0.01) => p,
                (None, _) => est,
            };
            self.prev_end = Some(t + len_s);
            out.push(t);
        }
        out
    }
}

/// Monotonic clock shared with the capture timestamps (QPC on Windows), in seconds.
#[cfg(windows)]
pub fn now() -> f64 {
    super::wasapi_capture::qpc_now()
}

#[cfg(target_os = "macos")]
pub fn now() -> f64 {
    super::sck_capture::host_now()
}

#[cfg(not(any(windows, target_os = "macos")))]
pub fn now() -> f64 {
    use std::sync::OnceLock;
    use std::time::Instant;
    static T0: OnceLock<Instant> = OnceLock::new();
    T0.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// Open a capture source; `device` is a `Device.id` or None for the default device.
/// `process` (system audio only) captures just that process tree.
#[cfg(windows)]
pub fn open(
    kind: SourceKind,
    device: Option<&str>,
    process: Option<u32>,
) -> Result<Box<dyn CaptureSource>, String> {
    match (kind, process) {
        (SourceKind::System, Some(pid)) => {
            super::wasapi_capture::WasapiSource::open_process(pid).map(|s| Box::new(s) as _)
        }
        _ => super::wasapi_capture::WasapiSource::open(kind, device).map(|s| Box::new(s) as _),
    }
}

#[cfg(target_os = "linux")]
pub fn open(
    kind: SourceKind,
    device: Option<&str>,
    process: Option<u32>,
) -> Result<Box<dyn CaptureSource>, String> {
    if process.is_some() {
        return Err("per-process capture is not supported on Linux".into());
    }
    super::pulse_capture::PulseSource::open(kind, device)
}

#[cfg(target_os = "macos")]
pub fn open(
    kind: SourceKind,
    device: Option<&str>,
    process: Option<u32>,
) -> Result<Box<dyn CaptureSource>, String> {
    super::sck_capture::open(kind, device, process)
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
pub fn open(
    _kind: SourceKind,
    _device: Option<&str>,
    _process: Option<u32>,
) -> Result<Box<dyn CaptureSource>, String> {
    Err("audio capture is not implemented on this platform".into())
}

/// Id of the current default device for `kind` (to follow default-device changes).
pub fn default_device_id(kind: SourceKind) -> Option<String> {
    #[cfg(windows)]
    {
        super::wasapi_capture::default_device_id(kind)
    }
    #[cfg(not(windows))]
    {
        let _ = kind;
        None
    }
}

/// Processes with an audio session (candidates for per-process capture).
pub fn processes() -> Result<Vec<crate::ipc::AudioProcess>, String> {
    #[cfg(windows)]
    {
        super::wasapi_capture::processes()
    }
    #[cfg(target_os = "macos")]
    {
        super::sck_capture::processes()
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        Ok(Vec::new())
    }
}

/// Per-thread platform audio initialisation (COM on Windows).
pub fn init_thread() {
    #[cfg(windows)]
    {
        let _ = wasapi::initialize_mta();
    }
}

/// Enumerate output (render) and input (capture) devices.
#[cfg(windows)]
pub fn devices() -> Result<crate::ipc::DeviceList, String> {
    super::wasapi_capture::devices()
}

#[cfg(target_os = "linux")]
pub fn devices() -> Result<crate::ipc::DeviceList, String> {
    super::pulse_capture::devices()
}

#[cfg(target_os = "macos")]
pub fn devices() -> Result<crate::ipc::DeviceList, String> {
    super::sck_capture::devices()
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
pub fn devices() -> Result<crate::ipc::DeviceList, String> {
    Ok(crate::ipc::DeviceList::default())
}

/// Whether a process with this pid is running.
pub fn process_exists(pid: u32) -> bool {
    #[cfg(windows)]
    {
        super::wasapi_capture::process_exists(pid)
    }
    #[cfg(unix)]
    {
        // SAFETY: kill(pid, 0) only checks for existence / permission.
        unsafe { libc::kill(pid as i32, 0) == 0 }
    }
    #[cfg(not(any(windows, unix)))]
    {
        let _ = pid;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const R: f64 = 48000.0;

    #[test]
    fn backlog_keeps_spacing_and_stamps_continue() {
        let mut st = BatchStamper::default();
        // steady state: one 10 ms packet per poll, read ~2 ms after it completed
        let mut now = 1.0;
        let t = st.stamp(now, R, &[(480, None)]);
        assert!((t[0] - 0.99).abs() < 1e-9);
        for _ in 0..5 {
            now += 0.01 + 0.002; // jitter: the next one is consecutive anyway
            let t = st.stamp(now, R, &[(480, None)]);
            assert!(t[0] < now - 0.01 + 1e-9);
        }
        let end = st.prev_end.unwrap();
        // 120 ms stall: 12 packets read in one go must stay 10 ms apart, continuing the stream
        now = end + 0.12 + 0.001;
        let pk = vec![(480usize, None); 12];
        let t = st.stamp(now, R, &pk);
        assert!((t[0] - end).abs() < 1e-9, "{} vs {end}", t[0]);
        for w in t.windows(2) {
            assert!((w[1] - w[0] - 0.01).abs() < 1e-9);
        }
        // idle loopback for 1 s: the next packet re-anchors to the clock (gap preserved)
        let end = st.prev_end.unwrap();
        now = end + 1.0;
        let t = st.stamp(now, R, &[(480, None)]);
        assert!((t[0] - (now - 0.01)).abs() < 1e-9);
    }

    #[test]
    fn device_timestamps_win_and_feed_continuity() {
        let mut st = BatchStamper::default();
        let t = st.stamp(5.0, R, &[(480, Some(4.0)), (480, None)]);
        assert_eq!(t[0], 4.0);
        // the fallback packet estimates 4.99, far from 4.01: re-anchored to the clock
        assert!((t[1] - 4.99).abs() < 1e-9);
        let t = st.stamp(5.012, R, &[(480, None)]);
        assert!((t[0] - 5.0).abs() < 1e-9, "{}", t[0]);
    }

    #[test]
    fn default_device_alias_maps_to_none() {
        assert_eq!(explicit_device(Some("default")), None);
        assert_eq!(explicit_device(Some("")), None);
        assert_eq!(explicit_device(None), None);
        assert_eq!(explicit_device(Some("mic-1")), Some("mic-1"));
    }
}
