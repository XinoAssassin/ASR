//! macOS capture via ScreenCaptureKit (macOS 13+): system audio of the main display's session,
//! optionally only one application (`processId`), and the microphone through SCK's
//! `captureMicrophone` (macOS 15+). Needs the Screen Recording (and Microphone) permission.
//! Audio sample buffers carry host-time PTS, the same clock as [`now`].
//!
//! Status: compiles for `aarch64-apple-darwin`; not run on a Mac yet (see desktop/README.md).

use std::ptr::NonNull;
use std::sync::mpsc;
use std::time::Duration;

use block2::RcBlock;
use dispatch2::DispatchQueue;
use objc2::rc::Retained;
use objc2::runtime::ProtocolObject;
use objc2::{define_class, msg_send, AllocAnyThread, DefinedClass};
use objc2_core_audio_types::{AudioBuffer, AudioBufferList};
use objc2_core_media::{CMBlockBuffer, CMClock, CMSampleBuffer, CMTime, CMTimeFlags};
use objc2_foundation::{NSArray, NSError, NSObject, NSObjectProtocol, NSString};
use objc2_screen_capture_kit::{
    SCContentFilter, SCRunningApplication, SCShareableContent, SCStream, SCStreamConfiguration,
    SCStreamDelegate, SCStreamOutput, SCStreamOutputType,
};

use super::capture::{CaptureSource, Packet, SourceKind};
use crate::ipc::{AudioProcess, Device, DeviceList};

const RATE: f64 = 48000.0;
type Tx = mpsc::Sender<Result<Packet, String>>;

/// Host-time clock in seconds (the clock of SCK sample buffer timestamps).
pub fn host_now() -> f64 {
    // SAFETY: CoreMedia C calls without preconditions.
    unsafe { CMClock::host_time_clock().time().seconds() }
}

pub struct Ivars {
    tx: Tx,
    kind: SCStreamOutputType,
}

define_class!(
    #[unsafe(super(NSObject))]
    #[name = "ASRRecorderStreamOutput"]
    #[ivars = Ivars]
    struct Output;

    unsafe impl NSObjectProtocol for Output {}

    unsafe impl SCStreamOutput for Output {
        #[unsafe(method(stream:didOutputSampleBuffer:ofType:))]
        unsafe fn stream_did_output(
            &self,
            _stream: &SCStream,
            sample_buffer: &CMSampleBuffer,
            r#type: SCStreamOutputType,
        ) {
            if r#type != self.ivars().kind {
                return;
            }
            // SAFETY: a valid sample buffer handed to us by SCK for the duration of the call.
            if let Some(p) = unsafe { packet(sample_buffer) } {
                let _ = self.ivars().tx.send(Ok(p));
            }
        }
    }

    unsafe impl SCStreamDelegate for Output {
        #[unsafe(method(stream:didStopWithError:))]
        unsafe fn stream_did_stop(&self, _stream: &SCStream, error: &NSError) {
            let _ = self.ivars().tx.send(Err(format!(
                "capture stopped: {}",
                error.localizedDescription()
            )));
        }
    }
);

impl Output {
    fn new(tx: Tx, kind: SCStreamOutputType) -> Retained<Self> {
        let this = Self::alloc().set_ivars(Ivars { tx, kind });
        // SAFETY: NSObject's designated initialiser.
        unsafe { msg_send![super(this), init] }
    }
}

/// Mono f32 packet from an SCK audio sample buffer (float32, planar or interleaved).
unsafe fn packet(sb: &CMSampleBuffer) -> Option<Packet> {
    unsafe {
        let pts = sb.presentation_time_stamp();
        if !pts.flags.contains(CMTimeFlags::Valid) {
            return None;
        }
        let mut needed = 0usize;
        sb.audio_buffer_list_with_retained_block_buffer(
            &mut needed,
            std::ptr::null_mut(),
            0,
            None,
            None,
            0,
            std::ptr::null_mut(),
        );
        if needed == 0 {
            return None;
        }
        // u64 storage keeps the list suitably aligned.
        let mut storage = vec![0u64; needed.div_ceil(8)];
        let list = storage.as_mut_ptr() as *mut AudioBufferList;
        let mut block: *mut CMBlockBuffer = std::ptr::null_mut();
        let status = sb.audio_buffer_list_with_retained_block_buffer(
            std::ptr::null_mut(),
            list,
            needed,
            None,
            None,
            0,
            &mut block,
        );
        if status != 0 {
            return None;
        }
        let n = (*list).mNumberBuffers as usize;
        let first = std::ptr::addr_of!((*list).mBuffers) as *const AudioBuffer;
        let buffers: Vec<AudioBuffer> = (0..n).map(|i| *first.add(i)).collect();
        let mut out: Vec<f32> = Vec::new();
        let mut planes = 0usize;
        for b in &buffers {
            let ch = b.mNumberChannels.max(1) as usize;
            let samples = b.mDataByteSize as usize / 4;
            let data = std::slice::from_raw_parts(b.mData as *const f32, samples);
            let frames = samples / ch;
            if out.len() < frames {
                out.resize(frames, 0.0);
            }
            for f in 0..frames {
                let mut acc = 0.0;
                for c in 0..ch {
                    acc += data[f * ch + c];
                }
                out[f] += acc;
            }
            planes += ch;
        }
        if let Some(b) = NonNull::new(block) {
            // Balance the retain from ...WithRetainedBlockBuffer.
            drop(objc2_core_foundation::CFRetained::from_raw(b));
        }
        let scale = 1.0 / planes.max(1) as f32;
        out.iter_mut().for_each(|v| *v *= scale);
        Some(Packet {
            t: pts.seconds(),
            samples: out,
        })
    }
}

fn shareable_content() -> Result<Retained<SCShareableContent>, String> {
    let (tx, rx) = mpsc::channel();
    let block = RcBlock::new(
        move |content: *mut SCShareableContent, error: *mut NSError| {
            // SAFETY: SCK passes either a valid content object or an error.
            let res = unsafe {
                match (Retained::retain(content), error.as_ref()) {
                    (Some(c), _) => Ok(c),
                    (None, Some(e)) => Err(e.localizedDescription().to_string()),
                    (None, None) => Err("no shareable content".to_string()),
                }
            };
            let _ = tx.send(res);
        },
    );
    // SAFETY: the completion handler is a valid block that outlives the call (RcBlock).
    unsafe { SCShareableContent::getShareableContentWithCompletionHandler(&block) };
    rx.recv_timeout(Duration::from_secs(10))
        .map_err(|_| "ScreenCaptureKit did not answer (Screen Recording permission?)".to_string())?
        .map_err(|e| format!("ScreenCaptureKit: {e}"))
}

pub struct SckSource {
    stream: Retained<SCStream>,
    _output: Retained<Output>,
    rx: mpsc::Receiver<Result<Packet, String>>,
}

// SAFETY: the stream is only started/stopped from the recorder thread; SCK objects are
// thread-safe for these calls, packets cross threads through the channel.
unsafe impl Send for SckSource {}

pub fn open(
    kind: SourceKind,
    device: Option<&str>,
    process: Option<u32>,
) -> Result<Box<dyn CaptureSource>, String> {
    let content = shareable_content()?;
    // SAFETY: ScreenCaptureKit / Foundation calls on valid objects created here.
    unsafe {
        let displays = content.displays();
        let display = displays
            .firstObject()
            .ok_or("no display to attach the capture to")?;
        let no_windows = NSArray::new();
        let filter = match (kind, process) {
            (SourceKind::System, Some(pid)) => {
                let apps: Vec<Retained<SCRunningApplication>> = content
                    .applications()
                    .iter()
                    .filter(|a| a.processID() as u32 == pid)
                    .collect();
                if apps.is_empty() {
                    return Err(format!("process {pid} has no capturable application"));
                }
                let apps = NSArray::from_retained_slice(&apps);
                SCContentFilter::initWithDisplay_includingApplications_exceptingWindows(
                    SCContentFilter::alloc(),
                    &display,
                    &apps,
                    &no_windows,
                )
            }
            _ => SCContentFilter::initWithDisplay_excludingWindows(
                SCContentFilter::alloc(),
                &display,
                &no_windows,
            ),
        };
        let config = SCStreamConfiguration::new();
        // Video cannot be switched off; keep it tiny and slow.
        config.setWidth(2);
        config.setHeight(2);
        config.setMinimumFrameInterval(CMTime {
            value: 1,
            timescale: 1,
            flags: CMTimeFlags::Valid,
            epoch: 0,
        });
        config.setSampleRate(RATE as _);
        config.setChannelCount(2);
        config.setExcludesCurrentProcessAudio(true);
        let out_type = match kind {
            SourceKind::System => {
                config.setCapturesAudio(true);
                SCStreamOutputType::Audio
            }
            SourceKind::Mic => {
                // macOS 15+: older systems reject the selector -> the source is unavailable
                // and the recorder continues with system audio only.
                let sel = objc2::sel!(setCaptureMicrophone:);
                let responds: bool = msg_send![&*config, respondsToSelector: sel];
                if !responds {
                    return Err("microphone capture needs macOS 15 (ScreenCaptureKit)".into());
                }
                config.setCaptureMicrophone(true);
                // The listed "default" entry is not an SCK device id.
                if let Some(id) = super::capture::explicit_device(device) {
                    config.setMicrophoneCaptureDeviceID(Some(&NSString::from_str(id)));
                }
                SCStreamOutputType::Microphone
            }
        };
        let (tx, rx) = mpsc::channel();
        let output = Output::new(tx, out_type);
        let delegate = ProtocolObject::from_ref(&*output);
        let stream = SCStream::initWithFilter_configuration_delegate(
            SCStream::alloc(),
            &filter,
            &config,
            Some(delegate),
        );
        let queue = DispatchQueue::new("dev.asr.recorder.sck", None);
        stream
            .addStreamOutput_type_sampleHandlerQueue_error(
                ProtocolObject::from_ref(&*output),
                out_type,
                Some(&queue),
            )
            .map_err(|e| e.localizedDescription().to_string())?;
        let (stx, srx) = mpsc::channel();
        let done = RcBlock::new(move |error: *mut NSError| {
            let _ = stx.send(error.as_ref().map(|e| e.localizedDescription().to_string()));
        });
        stream.startCaptureWithCompletionHandler(Some(&done));
        match srx.recv_timeout(Duration::from_secs(10)) {
            Ok(None) => {}
            Ok(Some(e)) => return Err(format!("ScreenCaptureKit start: {e}")),
            Err(_) => return Err("ScreenCaptureKit start timed out".into()),
        }
        log::info!("opened {kind:?} via ScreenCaptureKit (process {process:?})");
        Ok(Box::new(SckSource {
            stream,
            _output: output,
            rx,
        }))
    }
}

impl CaptureSource for SckSource {
    fn rate(&self) -> f64 {
        RATE
    }

    fn poll(&mut self, out: &mut Vec<Packet>) -> Result<(), String> {
        loop {
            match self.rx.try_recv() {
                Ok(Ok(p)) => out.push(p),
                Ok(Err(e)) => return Err(e),
                Err(mpsc::TryRecvError::Empty) => return Ok(()),
                Err(mpsc::TryRecvError::Disconnected) => return Err("capture ended".into()),
            }
        }
    }
}

impl Drop for SckSource {
    fn drop(&mut self) {
        // SAFETY: stopping our own stream; no completion handler needed.
        unsafe { self.stream.stopCaptureWithCompletionHandler(None) };
    }
}

pub fn devices() -> Result<DeviceList, String> {
    Ok(DeviceList {
        outputs: vec![Device {
            id: "sck-system".into(),
            name: "System audio (ScreenCaptureKit)".into(),
            is_default: true,
        }],
        inputs: vec![Device {
            id: super::capture::DEFAULT_DEVICE.into(),
            name: "Default microphone".into(),
            is_default: true,
        }],
    })
}

/// Applications ScreenCaptureKit can capture (macOS has no per-app audio-session list).
pub fn processes() -> Result<Vec<AudioProcess>, String> {
    let content = shareable_content()?;
    let own = std::process::id();
    // SAFETY: Foundation / SCK getters on valid objects.
    unsafe {
        let mut v: Vec<AudioProcess> = content
            .applications()
            .iter()
            .map(|a| AudioProcess {
                pid: a.processID() as u32,
                name: a.applicationName().to_string(),
                title: Some(a.bundleIdentifier().to_string()),
            })
            .filter(|p| p.pid != own && !p.name.is_empty())
            .collect();
        v.sort_by_key(|p| p.name.to_lowercase());
        Ok(v)
    }
}
