//! Linux capture through the PulseAudio native protocol (PulseAudio or PipeWire-pulse), pure Rust:
//! system audio = the monitor source of the default (or chosen) sink, mic = the default (or
//! chosen) source. One reader thread per stream blocks on the socket and hands mono packets,
//! stamped `now - duration`, to `poll`. Monitor sources deliver (silent) data continuously, so
//! the timeline's silence fill is rarely needed here.

use std::ffi::CString;
use std::io::{BufReader, Read};
use std::os::unix::net::UnixStream;
use std::sync::mpsc;

use pulseaudio::protocol;

use super::capture::{CaptureSource, Packet, SourceKind};
use crate::ipc::{Device, DeviceList};

type Sock = BufReader<UnixStream>;

fn e2s(e: impl std::fmt::Display) -> String {
    e.to_string()
}

fn connect() -> Result<(Sock, u16), String> {
    let path = pulseaudio::socket_path_from_env().ok_or("no PulseAudio / PipeWire-pulse server")?;
    let stream = UnixStream::connect(&path).map_err(e2s)?;
    // A wedged server must not hang the recorder: requests and the data stream time out
    // (a monitor / source delivers data continuously, so 5 s of nothing means trouble).
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
        .map_err(e2s)?;
    stream
        .set_write_timeout(Some(std::time::Duration::from_secs(5)))
        .map_err(e2s)?;
    let mut sock = BufReader::new(stream);
    // The server expects a 256-byte cookie even with anonymous auth (an empty one is a
    // protocol error); without a cookie file send zeros, like libpulse with a fresh cookie.
    let cookie = pulseaudio::cookie_path_from_env()
        .and_then(|p| std::fs::read(p).ok())
        .filter(|c| c.len() == 256)
        .unwrap_or_else(|| vec![0u8; 256]);
    protocol::write_command_message(
        sock.get_mut(),
        0,
        &protocol::Command::Auth(protocol::AuthParams {
            version: protocol::MAX_VERSION,
            supports_shm: false,
            supports_memfd: false,
            cookie,
        }),
        protocol::MAX_VERSION,
    )
    .map_err(e2s)?;
    let (_, auth) =
        protocol::read_reply_message::<protocol::AuthReply>(&mut sock, protocol::MAX_VERSION)
            .map_err(|e| format!("pulse auth: {e}"))?;
    let version = protocol::MAX_VERSION.min(auth.version);
    let mut props = protocol::Props::new();
    props.set(
        protocol::Prop::ApplicationName,
        CString::new("ASR Recorder").unwrap(),
    );
    protocol::write_command_message(
        sock.get_mut(),
        1,
        &protocol::Command::SetClientName(props),
        version,
    )
    .map_err(e2s)?;
    protocol::read_reply_message::<protocol::SetClientNameReply>(&mut sock, version)
        .map_err(e2s)?;
    Ok((sock, version))
}

fn source_info(
    sock: &mut Sock,
    v: u16,
    seq: u32,
    name: &CString,
) -> Result<protocol::SourceInfo, String> {
    protocol::write_command_message(
        sock.get_mut(),
        seq,
        &protocol::Command::GetSourceInfo(protocol::GetSourceInfo {
            name: Some(name.clone()),
            ..Default::default()
        }),
        v,
    )
    .map_err(e2s)?;
    Ok(
        protocol::read_reply_message::<protocol::SourceInfo>(sock, v)
            .map_err(e2s)?
            .1,
    )
}

/// Monitor source name of `sink` (None = default sink).
fn monitor_of(sock: &mut Sock, v: u16, sink: Option<&str>) -> Result<CString, String> {
    let name = match sink {
        Some(s) => CString::new(s).map_err(e2s)?,
        None => protocol::DEFAULT_SINK.to_owned(),
    };
    protocol::write_command_message(
        sock.get_mut(),
        2,
        &protocol::Command::GetSinkInfo(protocol::GetSinkInfo {
            index: None,
            name: Some(name),
        }),
        v,
    )
    .map_err(e2s)?;
    let (_, info) = protocol::read_reply_message::<protocol::SinkInfo>(sock, v).map_err(e2s)?;
    info.monitor_source_name
        .ok_or_else(|| "sink has no monitor source".into())
}

pub struct PulseSource {
    rate: f64,
    rx: mpsc::Receiver<Result<Packet, String>>,
    id: String,
}

impl PulseSource {
    pub fn open(kind: SourceKind, device: Option<&str>) -> Result<Box<dyn CaptureSource>, String> {
        let (mut sock, v) = connect()?;
        let source_name = match kind {
            SourceKind::System => monitor_of(&mut sock, v, device)?,
            SourceKind::Mic => match device {
                Some(d) => CString::new(d).map_err(e2s)?,
                None => protocol::DEFAULT_SOURCE.to_owned(),
            },
        };
        let info = source_info(&mut sock, v, 3, &source_name)?;
        let channels = info.channel_map.num_channels().max(1);
        let rate = info.sample_spec.sample_rate;
        let frame_bytes = 4 * channels as u32;
        let spec = protocol::SampleSpec {
            format: protocol::SampleFormat::Float32Le,
            channels,
            sample_rate: rate,
        };
        protocol::write_command_message(
            sock.get_mut(),
            4,
            &protocol::Command::CreateRecordStream(protocol::RecordStreamParams {
                source_index: Some(info.index),
                sample_spec: spec,
                channel_map: info.channel_map,
                buffer_attr: protocol::stream::BufferAttr {
                    max_length: rate * frame_bytes,         // 1 s
                    fragment_size: rate / 50 * frame_bytes, // 20 ms packets
                    ..Default::default()
                },
                flags: protocol::stream::StreamFlags {
                    adjust_latency: true,
                    ..Default::default()
                },
                ..Default::default()
            }),
            v,
        )
        .map_err(e2s)?;
        let (_, stream) =
            protocol::read_reply_message::<protocol::CreateRecordStreamReply>(&mut sock, v)
                .map_err(e2s)?;
        let rate_f = stream.sample_spec.sample_rate as f64;
        let ch = stream.sample_spec.channels.max(1) as usize;
        let id = info.name.to_string_lossy().into_owned();
        log::info!("opened {kind:?} '{id}': {rate_f} Hz, {ch} ch (pulse)");
        let (tx, rx) = mpsc::channel();
        std::thread::Builder::new()
            .name(format!("pulse-{kind:?}"))
            .spawn(move || reader(sock, v, rate_f, ch, tx))
            .map_err(e2s)?;
        Ok(Box::new(Self {
            rate: rate_f,
            rx,
            id,
        }))
    }
}

/// Packet timestamps from the sample count (the server clock), anchored to arrival times:
/// arrival jitter is smoothed away, a jump of more than 200 ms (overrun, suspend) re-anchors.
struct Stamper {
    rate: f64,
    t0: Option<f64>,
    frames: u64,
}

impl Stamper {
    fn stamp(&mut self, now: f64, n: usize) -> f64 {
        let observed = now - n as f64 / self.rate;
        let t = match self.t0 {
            Some(t0) => {
                let predicted = t0 + self.frames as f64 / self.rate;
                let err = observed - predicted;
                if err.abs() > 0.2 {
                    self.t0 = Some(observed - self.frames as f64 / self.rate);
                    observed
                } else {
                    // Arrival is never earlier than capture: follow the earliest arrivals
                    // quickly, later (delayed) ones slowly.
                    let k = if err < 0.0 { 0.5 } else { 0.002 };
                    self.t0 = Some(t0 + k * err);
                    predicted + k * err
                }
            }
            None => {
                self.t0 = Some(observed);
                observed
            }
        };
        self.frames += n as u64;
        t
    }
}

fn reader(mut sock: Sock, v: u16, rate: f64, ch: usize, tx: mpsc::Sender<Result<Packet, String>>) {
    let mut buf = Vec::new();
    let mut stamper = Stamper {
        rate,
        t0: None,
        frames: 0,
    };
    loop {
        let res = (|| -> Result<Option<Packet>, String> {
            let desc = protocol::read_descriptor(&mut sock).map_err(e2s)?;
            if desc.channel == u32::MAX {
                let (_, cmd) = protocol::Command::read_tag_prefixed(&mut sock, v).map_err(e2s)?;
                if let protocol::Command::RecordStreamKilled(_) = cmd {
                    return Err("record stream killed by the server".into());
                }
                return Ok(None);
            }
            buf.resize(desc.length as usize, 0);
            sock.read_exact(&mut buf).map_err(e2s)?;
            let now = super::capture::now();
            let frames = buf.len() / (4 * ch);
            let mut samples = Vec::with_capacity(frames);
            for f in 0..frames {
                let mut acc = 0.0f32;
                for c in 0..ch {
                    let o = (f * ch + c) * 4;
                    acc += f32::from_le_bytes([buf[o], buf[o + 1], buf[o + 2], buf[o + 3]]);
                }
                samples.push(acc / ch as f32);
            }
            Ok(Some(Packet {
                t: stamper.stamp(now, frames),
                samples,
            }))
        })();
        match res {
            Ok(Some(p)) => {
                if tx.send(Ok(p)).is_err() {
                    return; // source dropped
                }
            }
            Ok(None) => {}
            Err(e) => {
                let _ = tx.send(Err(e));
                return;
            }
        }
    }
}

impl CaptureSource for PulseSource {
    fn rate(&self) -> f64 {
        self.rate
    }

    fn poll(&mut self, out: &mut Vec<Packet>) -> Result<(), String> {
        loop {
            match self.rx.try_recv() {
                Ok(Ok(p)) => out.push(p),
                Ok(Err(e)) => return Err(e),
                Err(mpsc::TryRecvError::Empty) => return Ok(()),
                Err(mpsc::TryRecvError::Disconnected) => return Err("pulse reader ended".into()),
            }
        }
    }

    fn device_id(&self) -> Option<String> {
        Some(self.id.clone())
    }
}

pub fn devices() -> Result<DeviceList, String> {
    let (mut sock, v) = connect()?;
    protocol::write_command_message(sock.get_mut(), 2, &protocol::Command::GetServerInfo, v)
        .map_err(e2s)?;
    let (_, server) =
        protocol::read_reply_message::<protocol::ServerInfo>(&mut sock, v).map_err(e2s)?;
    protocol::write_command_message(sock.get_mut(), 3, &protocol::Command::GetSinkInfoList, v)
        .map_err(e2s)?;
    let (_, sinks) =
        protocol::read_reply_message::<protocol::SinkInfoList>(&mut sock, v).map_err(e2s)?;
    protocol::write_command_message(sock.get_mut(), 4, &protocol::Command::GetSourceInfoList, v)
        .map_err(e2s)?;
    let (_, sources) =
        protocol::read_reply_message::<protocol::SourceInfoList>(&mut sock, v).map_err(e2s)?;
    let name =
        |n: &CString, d: &Option<CString>| d.as_ref().unwrap_or(n).to_string_lossy().into_owned();
    Ok(DeviceList {
        outputs: sinks
            .iter()
            .map(|s| Device {
                id: s.name.to_string_lossy().into_owned(),
                name: name(&s.name, &s.description),
                is_default: server.default_sink_name.as_ref() == Some(&s.name),
            })
            .collect(),
        inputs: sources
            .iter()
            .filter(|s| s.monitor_of_sink_name.is_none())
            .map(|s| Device {
                id: s.name.to_string_lossy().into_owned(),
                name: name(&s.name, &s.description),
                is_default: server.default_source_name.as_ref() == Some(&s.name),
            })
            .collect(),
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn stamper_smooths_jitter_and_reanchors() {
        let mut s = super::Stamper {
            rate: 48000.0,
            t0: None,
            frames: 0,
        };
        let mut last = 0.0;
        for k in 0..500u64 {
            let true_t = k as f64 * 0.02;
            let jitter = if k % 3 == 0 { 0.015 } else { 0.0 }; // late deliveries
            let t = s.stamp(true_t + 0.02 + jitter, 960);
            if k > 5 {
                assert!((t - true_t).abs() < 0.004, "k {k}: {t} vs {true_t}");
            }
            last = t;
        }
        // a 1 s hole (suspend): re-anchored
        let t = s.stamp(500.0 * 0.02 + 1.0 + 0.02, 960);
        assert!((t - (10.0 + 1.0)).abs() < 0.01, "{t} {last}");
    }

    /// Needs a Pulse / PipeWire server (e.g. WSLg): `cargo test -- --ignored pulse`.
    #[test]
    #[ignore]
    fn pulse_records_system_and_mic() {
        eprintln!("devices: {:?}", super::devices());
        let dir = tempfile::tempdir().unwrap();
        let rec = crate::recorder::Recorder::new();
        let st = rec
            .start(&crate::ipc::StartOptions::default(), dir.path())
            .expect("start");
        eprintln!("start: sources {:?} warnings {:?}", st.sources, st.warnings);
        std::thread::sleep(std::time::Duration::from_secs(4));
        let s = rec.status(false);
        eprintln!(
            "status: elapsed {} levels {:?} drift {:?} warnings {:?}",
            s.elapsed, s.levels, s.drift_ms, s.warnings
        );
        let f = rec.stop().expect("stop");
        eprintln!(
            "finished {:?} {} ch {:.3} s {} bytes",
            f.path, f.channels, f.duration, f.size
        );
        assert!(f.duration > 3.5 && f.duration < 5.0);
        assert_eq!(f.path.extension().unwrap(), "flac");
    }
}
