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
