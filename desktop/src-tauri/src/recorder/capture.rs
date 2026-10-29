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
}

/// Monotonic clock shared with the capture timestamps (QPC on Windows), in seconds.
#[cfg(windows)]
pub fn now() -> f64 {
    super::wasapi_capture::qpc_now()
}

#[cfg(not(windows))]
pub fn now() -> f64 {
    use std::sync::OnceLock;
    use std::time::Instant;
    static T0: OnceLock<Instant> = OnceLock::new();
    T0.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// Open a capture source; `device` is a `Device.id` or None for the default device.
#[cfg(windows)]
pub fn open(kind: SourceKind, device: Option<&str>) -> Result<Box<dyn CaptureSource>, String> {
    super::wasapi_capture::WasapiSource::open(kind, device).map(|s| Box::new(s) as _)
}

#[cfg(not(windows))]
pub fn open(_kind: SourceKind, _device: Option<&str>) -> Result<Box<dyn CaptureSource>, String> {
    Err("audio capture is only implemented on Windows".into())
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

#[cfg(not(windows))]
pub fn devices() -> Result<crate::ipc::DeviceList, String> {
    Ok(crate::ipc::DeviceList::default())
}
