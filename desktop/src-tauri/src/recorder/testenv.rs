//! Fake devices / files for engine and recorder tests (synthetic audio). The clock is real time
//! by default; [`FakeEnv::manual`] gives a clock that only moves when the test advances it, so
//! timing assertions do not depend on how loaded the machine is.

use std::fs::File;
use std::io::{self, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use super::capture::{CaptureSource, Packet, SourceKind};
use super::engine::{Env, Live};
use super::wav::{Storage, WavWriter};

/// Packet timestamps / `Env::now` (seconds).
pub enum Clock {
    Real(Instant),
    /// Seconds; moved only by [`FakeEnv::advance`].
    Manual(Mutex<f64>),
}

const T0: f64 = 1000.0;

impl Clock {
    fn now(&self) -> f64 {
        match self {
            Clock::Real(i) => T0 + i.elapsed().as_secs_f64(),
            Clock::Manual(t) => *t.lock().unwrap_or_else(|p| p.into_inner()),
        }
    }
}

/// Constant-level source producing exact 10 ms packets up to the clock.
struct Tone {
    clock: Arc<Clock>,
    rate: f64,
    next: f64,
    level: f32,
    dies_at: Option<f64>,
    panics_at: Option<f64>,
    id: Option<String>,
}

impl CaptureSource for Tone {
    fn rate(&self) -> f64 {
        self.rate
    }

    fn poll(&mut self, out: &mut Vec<Packet>) -> Result<(), String> {
        let now = self.clock.now();
        if self.panics_at.is_some_and(|d| now >= d) {
            panic!("capture backend bug (simulated)");
        }
        if self.dies_at.is_some_and(|d| now >= d) {
            return Err("unplugged".into());
        }
        let n = (self.rate / 100.0) as usize;
        // 1 µs slack: the manual clock and `next` are sums of the same steps in another order.
        while self.next + 0.01 <= now + 1e-6 {
            out.push(Packet {
                t: self.next,
                samples: vec![self.level; n],
            });
            self.next += 0.01;
        }
        Ok(())
    }

    fn device_id(&self) -> Option<String> {
        self.id.clone()
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
    clock: Arc<Clock>,
    /// Delay of the first open of each source.
    pub open_delay: Mutex<Duration>,
    /// Delay of every later open (a reopen).
    pub reopen_delay: Mutex<Duration>,
    /// The first mic source fails this many seconds after it opened.
    pub mic_dies_after: Mutex<Option<f64>>,
    /// The first mic source panics in `poll` this many seconds after it opened.
    pub mic_panics_after: Mutex<Option<f64>>,
    /// Every reopen (not the first open) fails while set.
    pub fail_reopens: AtomicBool,
    /// Opening with a process id (process loopback) fails while set.
    pub fail_process_open: AtomicBool,
    /// Nominal rate of reopened sources, per kind (None: the rate of the first open).
    pub reopen_rate: [Mutex<Option<f64>>; 2],
    /// Current default endpoint id, per kind; an opened default source reports it.
    pub default_id: [Mutex<Option<String>>; 2],
    /// Pause inside `create_wav` after the file exists (widens start races).
    pub create_delay: Mutex<Duration>,
    pub fail_writes: Arc<AtomicBool>,
    pub opens: [AtomicUsize; 2],
    /// `(kind, process)` of every open call, in order.
    pub open_log: Mutex<Vec<(SourceKind, Option<u32>)>>,
    /// Clock time at which each running open returns (manual clock: `run_to` waits for the
    /// ones that are due).
    opening: Mutex<Vec<f64>>,
}

fn get<T: Clone>(m: &Mutex<T>) -> T {
    m.lock().unwrap_or_else(|p| p.into_inner()).clone()
}

impl FakeEnv {
    /// Real-time clock.
    pub fn new() -> Arc<Self> {
        Self::with_clock(Clock::Real(Instant::now()))
    }

    /// Clock that moves only through [`advance`](Self::advance) / [`run_to`](Self::run_to).
    pub fn manual() -> Arc<Self> {
        Self::with_clock(Clock::Manual(Mutex::new(T0)))
    }

    fn with_clock(clock: Clock) -> Arc<Self> {
        Arc::new(Self {
            clock: Arc::new(clock),
            open_delay: Mutex::new(Duration::ZERO),
            reopen_delay: Mutex::new(Duration::ZERO),
            mic_dies_after: Mutex::new(None),
            mic_panics_after: Mutex::new(None),
            fail_reopens: AtomicBool::new(false),
            fail_process_open: AtomicBool::new(false),
            reopen_rate: [Mutex::new(None), Mutex::new(None)],
            default_id: [Mutex::new(None), Mutex::new(None)],
            create_delay: Mutex::new(Duration::ZERO),
            fail_writes: Arc::new(AtomicBool::new(false)),
            opens: [AtomicUsize::new(0), AtomicUsize::new(0)],
            open_log: Mutex::new(Vec::new()),
            opening: Mutex::new(Vec::new()),
        })
    }

    pub fn opens(&self, kind: SourceKind) -> usize {
        self.opens[kind as usize].load(Ordering::SeqCst)
    }

    pub fn set_default_id(&self, kind: SourceKind, id: Option<&str>) {
        *self.default_id[kind as usize]
            .lock()
            .unwrap_or_else(|p| p.into_inner()) = id.map(str::to_string);
    }

    /// Manual clock: move it forward by `dt` seconds.
    pub fn advance(&self, dt: f64) {
        match &*self.clock {
            Clock::Manual(t) => *t.lock().unwrap_or_else(|p| p.into_inner()) += dt,
            Clock::Real(_) => panic!("advance() needs FakeEnv::manual()"),
        }
    }

    /// Manual clock, session started at the initial clock value: advance in `step`s until
    /// `elapsed` seconds into the session, waiting after each step until every open that is
    /// due has returned and the recorder thread has processed the step (`Live::elapsed`
    /// caught up). Returns false if the thread stopped following (it ended, or 10 s of real
    /// time passed). Reopens run on helper threads, so when exactly the recorder picks up a
    /// reopened source can still vary by a step or two.
    pub fn run_to(&self, live: &Mutex<Live>, elapsed: f64, step: f64) -> bool {
        loop {
            let at = self.clock.now() - T0;
            if at >= elapsed - 1e-9 {
                return true;
            }
            self.advance(step.min(elapsed - at));
            let want = self.clock.now() - T0;
            let deadline = Instant::now() + Duration::from_secs(10);
            let due = || {
                let now = self.clock.now();
                get(&self.opening).iter().any(|t| *t <= now + 1e-9)
            };
            while due() || live.lock().unwrap_or_else(|p| p.into_inner()).elapsed < want - 1e-9 {
                if Instant::now() >= deadline {
                    return false;
                }
                std::thread::sleep(Duration::from_millis(1));
            }
        }
    }

    /// Block for `d` on the session clock (real sleep, or until the manual clock passed it).
    fn wait(&self, d: Duration) {
        if d.is_zero() {
            return;
        }
        match &*self.clock {
            Clock::Real(_) => std::thread::sleep(d),
            Clock::Manual(_) => {
                let until = self.clock.now() + d.as_secs_f64();
                let cap = Instant::now() + Duration::from_secs(30);
                while self.clock.now() < until - 1e-9 && Instant::now() < cap {
                    std::thread::sleep(Duration::from_millis(1));
                }
            }
        }
    }

    fn open_tracked(
        &self,
        kind: SourceKind,
        device: Option<&str>,
        process: Option<u32>,
    ) -> Result<Box<dyn CaptureSource>, String> {
        let n = self.opens[kind as usize].fetch_add(1, Ordering::SeqCst);
        self.open_log
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push((kind, process));
        let delay = if n == 0 {
            get(&self.open_delay)
        } else {
            get(&self.reopen_delay)
        };
        let returns_at = self.clock.now() + delay.as_secs_f64();
        self.opening
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push(returns_at);
        let res = self.open_inner(kind, device, process, n, delay);
        let mut o = self.opening.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(i) = o.iter().position(|t| *t == returns_at) {
            o.swap_remove(i);
        }
        res
    }

    fn open_inner(
        &self,
        kind: SourceKind,
        device: Option<&str>,
        process: Option<u32>,
        n: usize,
        delay: Duration,
    ) -> Result<Box<dyn CaptureSource>, String> {
        self.wait(delay);
        if n > 0 && self.fail_reopens.load(Ordering::SeqCst) {
            return Err("device not found (simulated)".into());
        }
        if process.is_some() && self.fail_process_open.load(Ordering::SeqCst) {
            return Err("process loopback not supported (simulated)".into());
        }
        let now = self.clock.now();
        let first_mic = kind == SourceKind::Mic && n == 0;
        let dies_at = get(&self.mic_dies_after)
            .filter(|_| first_mic)
            .map(|d| now + d);
        let panics_at = get(&self.mic_panics_after)
            .filter(|_| first_mic)
            .map(|d| now + d);
        let (rate, level) = match kind {
            SourceKind::System => (48000.0, 0.25),
            SourceKind::Mic => (44100.0, -0.125),
        };
        let rate = match get(&self.reopen_rate[kind as usize]) {
            Some(r) if n > 0 => r,
            _ => rate,
        };
        let id = match (device, process) {
            (Some(d), _) => Some(d.to_string()),
            (None, Some(_)) => None,
            (None, None) => get(&self.default_id[kind as usize]),
        };
        Ok(Box::new(Tone {
            clock: self.clock.clone(),
            rate,
            next: now,
            level,
            dies_at,
            panics_at,
            id,
        }))
    }
}

impl Env for FakeEnv {
    fn now(&self) -> f64 {
        self.clock.now()
    }

    fn open(
        &self,
        kind: SourceKind,
        device: Option<&str>,
        process: Option<u32>,
    ) -> Result<Box<dyn CaptureSource>, String> {
        self.open_tracked(kind, device, process)
    }

    fn default_device_id(&self, kind: SourceKind) -> Option<String> {
        get(&self.default_id[kind as usize])
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
        std::thread::sleep(get(&self.create_delay));
        Ok(w)
    }
}
