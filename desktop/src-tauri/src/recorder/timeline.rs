//! Multi-track alignment: puts audio from independent device clocks onto one 16 kHz timeline.
//!
//! * Every packet carries a timestamp from one monotonic clock (QPC on Windows, seconds).
//! * Track 0 is the **reference** (system loopback when enabled, else the mic). While it delivers
//!   data its own sample clock defines the timeline: it is resampled at the fixed nominal ratio
//!   and a [`ClockMap`] (monotonic time -> output frame) is anchored to it.
//! * The other track is a **follower** (the mic): its resampling ratio is slewed by a PI
//!   controller so each packet lands where the map says its timestamp belongs ("mic resampled
//!   to the loopback clock").
//! * WASAPI loopback delivers nothing while the render device is idle. [`Timeline::tick`] keeps
//!   every track advancing in real time by inserting silence up to `map(now) - FILL_MARGIN`; when
//!   data resumes it is placed at `map(timestamp)`, so the idle gap has its real duration.

use super::resample::Resampler;

pub const OUT_RATE: f64 = 16_000.0;
/// A continuous track that is off by more than this is re-placed (silence insert / drop)
/// instead of slewed.
const RESYNC_S: f64 = 0.040;
/// Idle tracks are silence-filled up to `map(now) - FILL_MARGIN_S`, leaving room for late packets.
const FILL_MARGIN_S: f64 = 0.25;
/// A gap in the packet timestamps longer than this starts a new segment (exact re-placement).
const SEGMENT_GAP_S: f64 = 0.050;
const ANCHOR_ALPHA: f64 = 0.05;
const RATE_MIN_SPAN_S: f64 = 20.0;
const MAX_RATE_DEV: f64 = 0.005;
// Follower PI controller (error in seconds, adjustment as a relative step change).
const KP: f64 = 0.5;
const KI: f64 = 0.0625;
const ERR_TAU_S: f64 = 0.1;
const MAX_ADJ: f64 = 0.01;
const MAX_INTEG: f64 = 0.005;
/// Below ~ -66 dBFS counts as silence for the "silent for N s" warnings.
pub const SILENCE_THRESHOLD: f32 = 0.0005;

/// Monotonic time -> output frame position, following the reference device clock.
#[derive(Debug, Clone)]
pub struct ClockMap {
    anchor_t: f64,
    anchor_pos: f64,
    rate: f64,
    segment: Option<Fit>,
}

/// Running least-squares fit of output position vs. time within one continuous segment
/// (coordinates relative to the segment start for numerical stability). A single badly
/// timestamped packet barely moves the slope, unlike a two-point estimate.
#[derive(Debug, Clone)]
struct Fit {
    t0: f64,
    p0: f64,
    n: f64,
    sx: f64,
    sy: f64,
    sxx: f64,
    sxy: f64,
}

impl Fit {
    fn new(t0: f64, p0: f64) -> Self {
        Self {
            t0,
            p0,
            n: 0.0,
            sx: 0.0,
            sy: 0.0,
            sxx: 0.0,
            sxy: 0.0,
        }
    }

    fn add(&mut self, t: f64, pos: f64) {
        let (x, y) = (t - self.t0, pos - self.p0);
        self.n += 1.0;
        self.sx += x;
        self.sy += y;
        self.sxx += x * x;
        self.sxy += x * y;
    }

    fn span(&self, t: f64) -> f64 {
        t - self.t0
    }

    /// Slope in output frames per second.
    fn slope(&self) -> Option<f64> {
        let d = self.n * self.sxx - self.sx * self.sx;
        (d > 0.0).then(|| (self.n * self.sxy - self.sx * self.sy) / d)
    }
}

impl ClockMap {
    pub fn new(start_t: f64) -> Self {
        Self {
            anchor_t: start_t,
            anchor_pos: 0.0,
            rate: 1.0,
            segment: None,
        }
    }

    pub fn pos_at(&self, t: f64) -> f64 {
        self.anchor_pos + (t - self.anchor_t) * self.rate * OUT_RATE
    }

    /// Reference device clock / monotonic clock, as estimated so far.
    pub fn rate(&self) -> f64 {
        self.rate
    }

    fn observe(&mut self, t: f64, pos: f64, new_segment: bool) {
        if new_segment {
            let mut fit = Fit::new(t, pos);
            fit.add(t, pos);
            self.segment = Some(fit);
            self.anchor_t = t;
            self.anchor_pos = pos;
            return;
        }
        if let Some(fit) = self.segment.as_mut() {
            fit.add(t, pos);
            if fit.span(t) >= RATE_MIN_SPAN_S {
                if let Some(slope) = fit.slope() {
                    let r = slope / OUT_RATE;
                    self.rate = r.clamp(1.0 - MAX_RATE_DEV, 1.0 + MAX_RATE_DEV);
                }
            }
        }
        let predicted = self.pos_at(t);
        self.anchor_pos = predicted + ANCHOR_ALPHA * (pos - predicted);
        self.anchor_t = t;
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Reference,
    Follower,
}

pub struct Track {
    role: Role,
    in_rate: f64,
    nominal_step: f64,
    step: f64,
    rs: Resampler,
    out: Vec<f32>,
    /// Input samples still to drop from upcoming packets (track was ahead of the map).
    drop_pending: usize,
    /// End time (monotonic) of the last packet, None before the first one.
    last_end_t: Option<f64>,
    idle_filled: bool,
    err_f: Option<f64>,
    integ: f64,
    peak: f32,
    last_sound_t: f64,
    pub stats: TrackStats,
}

#[derive(Debug, Clone, Default)]
pub struct TrackStats {
    pub packets: u64,
    pub input_frames: u64,
    pub gaps_filled: u64,
    pub silence_inserted_s: f64,
    pub dropped_s: f64,
}

impl Track {
    fn new(role: Role, in_rate: f64, start_t: f64) -> Self {
        let step = in_rate / OUT_RATE;
        Self {
            role,
            in_rate,
            nominal_step: step,
            step,
            rs: Resampler::new(in_rate, OUT_RATE),
            out: Vec::new(),
            drop_pending: 0,
            last_end_t: None,
            idle_filled: false,
            err_f: None,
            integ: 0.0,
            peak: 0.0,
            last_sound_t: start_t,
            stats: TrackStats::default(),
        }
    }

    fn landing_next(&self) -> f64 {
        self.rs.landing(self.rs.in_end() as f64, self.step)
    }

    fn insert_silence(&mut self, out_frames: f64) {
        let n = (out_frames * self.step).round().max(0.0) as usize;
        if n > 0 {
            self.rs.push_zeros(n);
            self.stats.silence_inserted_s += n as f64 / self.in_rate;
        }
    }

    fn feed(&mut self, map: &mut ClockMap, mut t: f64, mut samples: &[f32]) {
        self.stats.packets += 1;
        self.stats.input_frames += samples.len() as u64;
        let len_s = samples.len() as f64 / self.in_rate;
        for &s in samples {
            let a = s.abs();
            if a > self.peak {
                self.peak = a;
            }
        }
        if samples.iter().any(|s| s.abs() > SILENCE_THRESHOLD) {
            self.last_sound_t = t + len_s;
        }
        if self.drop_pending > 0 {
            let d = self.drop_pending.min(samples.len());
            samples = &samples[d..];
            self.drop_pending -= d;
            t += d as f64 / self.in_rate;
            self.stats.dropped_s += d as f64 / self.in_rate;
            if samples.is_empty() {
                self.last_end_t = Some(t);
                return;
            }
        }
        let new_segment = self.idle_filled
            || match self.last_end_t {
                None => true,
                Some(end) => (t - end).abs() > SEGMENT_GAP_S,
            };
        let expected = map.pos_at(t);
        let diff = expected - self.landing_next();
        let resync = RESYNC_S * OUT_RATE;
        if new_segment || diff.abs() > resync {
            // Place exactly: silence for a gap, drop input when ahead.
            if diff > 0.0 {
                self.insert_silence(diff);
                if !new_segment || self.last_end_t.is_some() {
                    self.stats.gaps_filled += 1;
                }
            } else if diff < 0.0 {
                let n = (-diff * self.step).round() as usize;
                let d = n.min(samples.len());
                samples = &samples[d..];
                self.drop_pending = n - d;
                t += d as f64 / self.in_rate;
                self.stats.dropped_s += d as f64 / self.in_rate;
            }
            self.err_f = None;
        } else if self.role == Role::Follower {
            self.control(-diff / OUT_RATE, len_s);
        }
        self.idle_filled = false;
        self.last_end_t = Some(t + samples.len() as f64 / self.in_rate);
        if samples.is_empty() {
            return;
        }
        let land_first = self.landing_next();
        self.rs.push(samples);
        self.rs.process(self.step, &mut self.out);
        if self.role == Role::Reference {
            map.observe(t, land_first, new_segment);
        }
    }

    /// PI step: `err` > 0 means this track lands later than the map (too many output samples),
    /// so the step (input per output) grows.
    fn control(&mut self, err: f64, dt: f64) {
        let a = (dt / ERR_TAU_S).min(1.0);
        let e = match self.err_f {
            None => err,
            Some(f) => f + a * (err - f),
        };
        self.err_f = Some(e);
        self.integ = (self.integ + KI * e * dt).clamp(-MAX_INTEG, MAX_INTEG);
        let adj = (KP * e + self.integ).clamp(-MAX_ADJ, MAX_ADJ);
        self.step = self.nominal_step * (1.0 + adj);
    }

    fn tick(&mut self, map: &ClockMap, now: f64) {
        let target = map.pos_at(now) - FILL_MARGIN_S * OUT_RATE;
        let land = self.landing_next();
        if target > land + 1.0 {
            self.insert_silence(target - land);
            self.idle_filled = true;
            self.err_f = None;
        }
        self.rs.process(self.step, &mut self.out);
    }

    /// Pad with silence / truncate so exactly `end` output frames exist in total.
    fn finish(&mut self, end: u64) {
        let produced = |t: &Self| t.rs.out_count();
        if produced(self) < end {
            let need_out = (end - produced(self)) as f64;
            let n = (need_out * self.step).ceil() as usize + self.rs.half_width() * 2 + 4;
            self.rs.push_zeros(n);
            self.rs.process(self.step, &mut self.out);
        }
        let start = self.rs.out_count() - self.out.len() as u64;
        let keep = end.saturating_sub(start) as usize;
        self.out.truncate(keep);
    }

    /// Switch to a new input rate (device reopened with another mix format). Pending input is
    /// flushed; the next packet starts a new segment and is placed by its timestamp.
    fn reset_rate(&mut self, in_rate: f64) {
        self.rs.push_zeros(self.rs.half_width() * 2 + 2);
        self.rs.process(self.step, &mut self.out);
        let out_count = self.rs.out_count();
        self.in_rate = in_rate;
        self.nominal_step = in_rate / OUT_RATE;
        self.step = self.nominal_step;
        self.rs = Resampler::new_at(in_rate, OUT_RATE, out_count);
        self.err_f = None;
        self.integ = 0.0;
        self.drop_pending = 0;
        self.idle_filled = true;
    }

    fn take_peak(&mut self) -> f32 {
        std::mem::take(&mut self.peak)
    }
}

/// All tracks of one recording. Track 0 is the reference; channel order = track order.
pub struct Timeline {
    pub map: ClockMap,
    tracks: Vec<Track>,
    written: u64,
    start_t: f64,
}

impl Timeline {
    /// `rates[i]` is the nominal input rate of track i (mono input).
    pub fn new(start_t: f64, rates: &[f64]) -> Self {
        assert!(!rates.is_empty() && rates.len() <= 2);
        let tracks = rates
            .iter()
            .enumerate()
            .map(|(i, &r)| {
                let role = if i == 0 {
                    Role::Reference
                } else {
                    Role::Follower
                };
                Track::new(role, r, start_t)
            })
            .collect();
        Self {
            map: ClockMap::new(start_t),
            tracks,
            written: 0,
            start_t,
        }
    }

    pub fn channels(&self) -> usize {
        self.tracks.len()
    }

    pub fn start_t(&self) -> f64 {
        self.start_t
    }

    /// Feed one mono packet whose first sample was captured at monotonic time `t`.
    pub fn feed(&mut self, track: usize, t: f64, samples: &[f32]) {
        let (map, tracks) = (&mut self.map, &mut self.tracks);
        tracks[track].feed(map, t, samples);
    }

    /// The device of track `i` was reopened with nominal rate `rate`.
    pub fn reset_rate(&mut self, i: usize, rate: f64) {
        self.tracks[i].reset_rate(rate);
    }

    /// Advance idle tracks to real time (silence fill).
    pub fn tick(&mut self, now: f64) {
        for tr in &mut self.tracks {
            tr.tick(&self.map, now);
        }
    }

    /// Move every frame that all tracks have produced into `out` (interleaved i16).
    pub fn take_frames(&mut self, out: &mut Vec<i16>) -> usize {
        let n = self.tracks.iter().map(|t| t.out.len()).min().unwrap_or(0);
        if n == 0 {
            return 0;
        }
        out.reserve(n * self.tracks.len());
        for i in 0..n {
            for tr in &self.tracks {
                out.push(to_i16(tr.out[i]));
            }
        }
        for tr in &mut self.tracks {
            tr.out.drain(..n);
        }
        self.written += n as u64;
        n
    }

    /// Frames handed out by `take_frames` so far.
    pub fn written(&self) -> u64 {
        self.written
    }

    /// Close the timeline at monotonic time `t_stop`: every track gets exactly the same length.
    pub fn finish(&mut self, t_stop: f64) {
        let end = (self.map.pos_at(t_stop).round().max(0.0) as u64).max(self.written);
        for tr in &mut self.tracks {
            tr.finish(end);
        }
    }

    pub fn take_peaks(&mut self) -> Vec<f32> {
        self.tracks.iter_mut().map(Track::take_peak).collect()
    }

    /// Seconds since track `i` last carried sound (or since start).
    pub fn silent_for(&self, i: usize, now: f64) -> f64 {
        (now - self.tracks[i].last_sound_t).max(0.0)
    }

    /// Residual follower-vs-reference misalignment (ms) after resampling, if measurable.
    pub fn drift_ms(&self) -> Option<f64> {
        self.tracks
            .get(1)
            .and_then(|t| t.err_f)
            .map(|e| (e * 1000.0 * 100.0).round() / 100.0)
    }

    /// Follower clock correction currently applied (ppm, + = follower runs fast).
    pub fn follower_ppm(&self) -> Option<f64> {
        self.tracks
            .get(1)
            .map(|t| (t.step / t.nominal_step - 1.0) * 1e6)
    }

    pub fn stats(&self) -> Vec<TrackStats> {
        self.tracks.iter().map(|t| t.stats.clone()).collect()
    }
}

#[inline]
pub fn to_i16(x: f32) -> i16 {
    (x * 32767.0).round().clamp(-32768.0, 32767.0) as i16
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Simulated capture device: true sample rate = nominal * (1 + ppm), packets with jittered
    /// timestamps, unit impulses at given true times, optional idle windows (no packets).
    struct SimDev {
        true_rate: f64,
        packet: usize,
        next_sample: u64,
        impulses: Vec<f64>,
        idle: Vec<(f64, f64)>,
        jitter_s: f64,
        seed: u64,
    }

    impl SimDev {
        fn new(nominal: f64, ppm: f64, impulses: Vec<f64>) -> Self {
            Self {
                true_rate: nominal * (1.0 + ppm * 1e-6),
                packet: (nominal / 100.0) as usize,
                next_sample: 0,
                impulses,
                idle: Vec::new(),
                jitter_s: 0.0005,
                seed: 7,
            }
        }

        fn rand(&mut self) -> f64 {
            self.seed = self
                .seed
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            ((self.seed >> 11) as f64 / (1u64 << 53) as f64) * 2.0 - 1.0
        }

        /// Packets fully captured before true time `now`: (timestamp, samples).
        fn poll(&mut self, now: f64, t0: f64) -> Vec<(f64, Vec<f32>)> {
            let mut out = Vec::new();
            loop {
                let first = self.next_sample;
                let end_t = t0 + (first + self.packet as u64) as f64 / self.true_rate;
                if end_t > now {
                    break;
                }
                let start_t = t0 + first as f64 / self.true_rate;
                self.next_sample += self.packet as u64;
                if self.idle.iter().any(|&(a, b)| start_t >= a && start_t < b) {
                    continue; // render device idle: loopback delivers nothing
                }
                let mut buf = vec![0.0f32; self.packet];
                for &imp in &self.impulses {
                    let idx = ((imp - t0) * self.true_rate).round() as i64 - first as i64;
                    if idx >= 0 && (idx as usize) < self.packet {
                        buf[idx as usize] = 0.9;
                    }
                }
                let j = self.rand() * self.jitter_s;
                out.push((start_t + j, buf));
            }
            out
        }
    }

    fn peaks(ch: &[f32], expected: &[f64]) -> Vec<f64> {
        // Position (s) of the max within +-0.5 s of each expected impulse.
        expected
            .iter()
            .map(|&e| {
                let c = (e * OUT_RATE) as i64;
                let lo = (c - 8000).max(0) as usize;
                let hi = ((c + 8000) as usize).min(ch.len());
                let (i, _) = ch[lo..hi]
                    .iter()
                    .enumerate()
                    .max_by(|a, b| a.1.abs().partial_cmp(&b.1.abs()).unwrap())
                    .unwrap();
                (lo + i) as f64 / OUT_RATE
            })
            .collect()
    }

    fn run(devs: &mut [SimDev], t0: f64, dur: f64, rates: &[f64]) -> (Vec<Vec<f32>>, Timeline) {
        let mut tl = Timeline::new(t0, rates);
        let mut frames = Vec::new();
        let mut now = t0;
        while now < t0 + dur {
            now += 0.01;
            for (i, d) in devs.iter_mut().enumerate() {
                for (t, buf) in d.poll(now, t0) {
                    tl.feed(i, t, &buf);
                }
            }
            tl.tick(now);
            tl.take_frames(&mut frames);
        }
        tl.finish(now);
        tl.take_frames(&mut frames);
        let ch = tl.channels();
        let chans = (0..ch)
            .map(|c| {
                frames
                    .iter()
                    .skip(c)
                    .step_by(ch)
                    .map(|&v| v as f32 / 32767.0)
                    .collect()
            })
            .collect();
        (chans, tl)
    }

    #[test]
    fn follower_locks_to_reference_with_idle_gap() {
        // 150 s, reference +300 ppm @48 kHz, follower -400 ppm @44.1 kHz (700 ppm apart =
        // 105 ms uncorrected), reference idle from 60 s to 80 s.
        let t0 = 1000.0;
        let imps: Vec<f64> = (1..15).map(|k| t0 + k as f64 * 10.0 + 0.123).collect();
        let mut sys = SimDev::new(48000.0, 300.0, imps.clone());
        sys.idle.push((t0 + 60.0, t0 + 80.0));
        let mic = SimDev::new(44100.0, -400.0, imps.clone());
        let mut devs = [sys, mic];
        let (ch, tl) = run(&mut devs, t0, 150.0, &[48000.0, 44100.0]);
        assert_eq!(ch[0].len(), ch[1].len());
        // Total length follows the reference clock (+300 ppm) over 150 s.
        let expect_len = 150.0 * (1.0 + 300e-6) * OUT_RATE;
        assert!(
            (ch[0].len() as f64 - expect_len).abs() < 0.02 * OUT_RATE,
            "{}",
            ch[0].len()
        );
        let rel: Vec<f64> = imps.iter().map(|t| t - t0).collect();
        let present: Vec<f64> = rel
            .iter()
            .copied()
            .filter(|t| !(60.0..80.0).contains(t))
            .collect();
        let p0 = peaks(&ch[0], &present);
        let p1 = peaks(&ch[1], &present);
        let worst = p0
            .iter()
            .zip(&p1)
            .map(|(a, b)| (a - b).abs())
            .fold(0.0, f64::max);
        eprintln!(
            "sys-mic worst {:.3} ms, ppm {:?}",
            worst * 1e3,
            tl.follower_ppm()
        );
        for (k, (a, b)) in p0.iter().zip(&p1).enumerate() {
            // after the controller settles (first impulse at 10 s) channels agree within 1 ms
            assert!((a - b).abs() < 0.001, "impulse {k}: sys {a:.4} mic {b:.4}");
        }
        // The reference stays on real time across the idle gap: impulse positions follow the
        // reference clock (true time * (1+300ppm)) within a couple of ms.
        for (&a, &e) in p0.iter().zip(&present) {
            let want = e * (1.0 + 300e-6);
            assert!((a - want).abs() < 0.003, "sys {a:.4} want {want:.4}");
        }
        let st = tl.stats();
        assert!(st[0].silence_inserted_s > 19.0, "{:?}", st[0]);
        assert!(st[1].silence_inserted_s < 0.1, "{:?}", st[1]);
        assert!(tl.drift_ms().unwrap().abs() < 1.0);
    }

    #[test]
    fn reference_silent_at_start_and_mic_only() {
        let t0 = 5.0;
        let imps: Vec<f64> = vec![t0 + 12.0, t0 + 25.0];
        let mut sys = SimDev::new(48000.0, 0.0, imps.clone());
        sys.idle.push((t0, t0 + 10.0)); // nothing plays for the first 10 s
        let mic = SimDev::new(48000.0, 150.0, imps.clone());
        let mut devs = [sys, mic];
        let (ch, _) = run(&mut devs, t0, 30.0, &[48000.0, 48000.0]);
        let p0 = peaks(&ch[0], &[12.0, 25.0]);
        let p1 = peaks(&ch[1], &[12.0, 25.0]);
        for (a, b) in p0.iter().zip(&p1) {
            assert!((a - b).abs() < 0.002, "{a} {b}");
            assert!((a - 12.0).abs() < 0.003 || (a - 25.0).abs() < 0.003, "{a}");
        }

        // Single track: the mic is the reference.
        let mut devs = [SimDev::new(44100.0, 80.0, imps)];
        let (ch, tl) = run(&mut devs, t0, 30.0, &[44100.0]);
        assert_eq!(tl.channels(), 1);
        let p = peaks(&ch[0], &[12.0, 25.0]);
        assert!(
            (p[0] - 12.0).abs() < 0.003 && (p[1] - 25.0).abs() < 0.003,
            "{p:?}"
        );
    }

    #[test]
    fn stale_packets_before_start_are_dropped() {
        let mut tl = Timeline::new(10.0, &[16000.0]);
        tl.feed(0, 9.9, &[0.5; 3200]); // 9.9 .. 10.1 s: first 0.1 s precedes the start
        tl.tick(10.2);
        tl.finish(10.2);
        let mut f = Vec::new();
        tl.take_frames(&mut f);
        assert_eq!(f.len(), 3200);
        // 0.1 s of signal kept, then silence
        let loud = f.iter().filter(|v| v.abs() > 1000).count();
        assert!((1500..1700).contains(&loud), "{loud}");
    }

    #[test]
    fn rate_fit_ignores_one_bad_timestamp() {
        let mut m = ClockMap::new(0.0);
        // first packet stamped 60 ms early, then 40 s of exact 10 ms packets at +100 ppm
        m.observe(-0.06, 0.0, true);
        for k in 1..4000 {
            let t = k as f64 * 0.01;
            m.observe(t, t * OUT_RATE * (1.0 + 100e-6), false);
        }
        assert!((m.rate() - (1.0 + 100e-6)).abs() < 20e-6, "{}", m.rate());
    }

    #[test]
    fn i16_conversion_clamps() {
        assert_eq!(to_i16(2.0), 32767);
        assert_eq!(to_i16(-2.0), -32768);
        assert_eq!(to_i16(0.0), 0);
    }
}
