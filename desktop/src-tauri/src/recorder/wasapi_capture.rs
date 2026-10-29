//! WASAPI capture: shared-mode loopback of a render endpoint, or a capture endpoint, polled.
//! Packets carry the QPC timestamp WASAPI reports for their first frame.

use std::sync::OnceLock;

use wasapi::{
    AudioCaptureClient, AudioClient, DeviceEnumerator, Direction, SampleType, StreamMode,
};
use windows::Win32::System::Performance::{QueryPerformanceCounter, QueryPerformanceFrequency};

use super::capture::{CaptureSource, Packet, SourceKind};
use crate::ipc::{Device, DeviceList};

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
