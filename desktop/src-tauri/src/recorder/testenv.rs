//! Fake devices / files for engine and recorder tests (real time, synthetic audio).

use std::fs::File;
use std::io::{self, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use super::capture::{CaptureSource, Packet, SourceKind};
use super::engine::Env;
use super::wav::{Storage, WavWriter};

/// Constant-level source producing exact 10 ms packets in real time.
struct Tone {
    clock: Arc<Instant>,
    rate: f64,
    next: f64,
    level: f32,
    dies_at: Option<f64>,
}

fn clock_now(c: &Instant) -> f64 {
    1000.0 + c.elapsed().as_secs_f64()
}

impl CaptureSource for Tone {
    fn rate(&self) -> f64 {
        self.rate
    }

    fn poll(&mut self, out: &mut Vec<Packet>) -> Result<(), String> {
        let now = clock_now(&self.clock);
        if self.dies_at.is_some_and(|d| now >= d) {
            return Err("unplugged".into());
        }
        let n = (self.rate / 100.0) as usize;
        while self.next + 0.01 <= now {
            out.push(Packet {
                t: self.next,
                samples: vec![self.level; n],
            });
            self.next += 0.01;
        }
        Ok(())
    }
}

/// File whose writes fail while `fail` is set.
struct FlakyFile {
    file: File,
    fail: Arc<AtomicBool>,
}

impl Write for FlakyFile {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(io::Error::other("disk full (simulated)"));
        }
        self.file.write(b)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

impl Seek for FlakyFile {
    fn seek(&mut self, p: SeekFrom) -> io::Result<u64> {
        self.file.seek(p)
    }
}

impl Storage for FlakyFile {
    fn sync_data(&mut self) -> io::Result<()> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(io::Error::other("disk full (simulated)"));
        }
        self.file.sync_data()
    }
}

pub struct FakeEnv {
    clock: Arc<Instant>,
    /// Delay of the first open of each source.
    pub open_delay: Mutex<Duration>,
    /// Delay of every later open (a reopen).
    pub reopen_delay: Mutex<Duration>,
    /// The first mic source fails this many seconds after it opened.
    pub mic_dies_after: Mutex<Option<f64>>,
    /// Pause inside `create_wav` after the file exists (widens start races).
    pub create_delay: Mutex<Duration>,
    pub fail_writes: Arc<AtomicBool>,
    pub opens: [AtomicUsize; 2],
}

impl FakeEnv {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            clock: Arc::new(Instant::now()),
            open_delay: Mutex::new(Duration::ZERO),
            reopen_delay: Mutex::new(Duration::ZERO),
            mic_dies_after: Mutex::new(None),
            create_delay: Mutex::new(Duration::ZERO),
            fail_writes: Arc::new(AtomicBool::new(false)),
            opens: [AtomicUsize::new(0), AtomicUsize::new(0)],
        })
    }

    pub fn opens(&self, kind: SourceKind) -> usize {
        self.opens[kind as usize].load(Ordering::SeqCst)
    }
}

impl Env for FakeEnv {
    fn now(&self) -> f64 {
        clock_now(&self.clock)
    }

    fn open(
        &self,
        kind: SourceKind,
        _device: Option<&str>,
        _process: Option<u32>,
    ) -> Result<Box<dyn CaptureSource>, String> {
        let n = self.opens[kind as usize].fetch_add(1, Ordering::SeqCst);
        let delay = if n == 0 {
            *self.open_delay.lock().unwrap()
        } else {
            *self.reopen_delay.lock().unwrap()
        };
        std::thread::sleep(delay);
        let now = self.now();
        let dies_at = match kind {
            SourceKind::Mic if n == 0 => self.mic_dies_after.lock().unwrap().map(|d| now + d),
            _ => None,
        };
        let (rate, level) = match kind {
            SourceKind::System => (48000.0, 0.25),
            SourceKind::Mic => (44100.0, -0.125),
        };
        Ok(Box::new(Tone {
            clock: self.clock.clone(),
            rate,
            next: now,
            level,
            dies_at,
        }))
    }

    fn default_device_id(&self, _kind: SourceKind) -> Option<String> {
        None
    }

    fn process_exists(&self, _pid: u32) -> bool {
        true
    }

    fn create_wav(
        &self,
        path: &Path,
        channels: u16,
        rate: u32,
    ) -> io::Result<WavWriter<Box<dyn Storage>>> {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?;
        let st = FlakyFile {
            file,
            fail: self.fail_writes.clone(),
        };
        let w = WavWriter::with_storage(Box::new(st) as Box<dyn Storage>, channels, rate)?;
        std::thread::sleep(*self.create_delay.lock().unwrap());
        Ok(w)
    }
}
