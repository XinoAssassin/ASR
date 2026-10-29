//! Band-limited variable-ratio resampler (windowed-sinc interpolation, J. O. Smith style).
//!
//! The ratio can change on every output sample, which is what the drift controller needs: the
//! follower track is slewed by a few hundred ppm to stay locked to the reference clock. Input and
//! output positions are tracked as absolute indices, so the caller can compute exactly where an
//! input sample will land in the output (`landing`).

const OVERSAMPLE: usize = 512;
const ZERO_CROSSINGS: f64 = 16.0;
const KAISER_BETA: f64 = 8.0;
/// Passband edge as a fraction of the lower Nyquist frequency of the two rates.
const CUTOFF: f64 = 0.90;

pub struct Resampler {
    half: usize,
    table: Vec<f32>,
    buf: Vec<f32>,
    /// Absolute input index of `buf[0]`.
    buf_start: u64,
    /// Absolute input position (fractional) of the next output sample.
    pos: f64,
    out_count: u64,
}

fn bessel_i0(x: f64) -> f64 {
    let mut sum = 1.0;
    let mut term = 1.0;
    let q = x * x / 4.0;
    for k in 1..64 {
        term *= q / (k as f64 * k as f64);
        sum += term;
        if term < sum * 1e-16 {
            break;
        }
    }
    sum
}

impl Resampler {
    /// `in_rate` and `out_rate` are nominal; the actual step is passed to `process`.
    pub fn new(in_rate: f64, out_rate: f64) -> Self {
        Self::new_at(in_rate, out_rate, 0)
    }

    /// Like `new`, but the first output sample gets absolute index `out_count` (used when a
    /// device reopens at a different rate mid-recording).
    pub fn new_at(in_rate: f64, out_rate: f64, out_count: u64) -> Self {
        // Cutoff in cycles per input sample.
        let fc = CUTOFF * 0.5 * out_rate.min(in_rate) / in_rate;
        let half_f = ZERO_CROSSINGS / (2.0 * fc);
        let half = half_f.ceil() as usize;
        let n = half * OVERSAMPLE + 2;
        let i0b = bessel_i0(KAISER_BETA);
        let table = (0..n)
            .map(|i| {
                let x = i as f64 / OVERSAMPLE as f64;
                if x >= half_f {
                    return 0.0;
                }
                let arg = 2.0 * fc * x;
                let sinc = if arg.abs() < 1e-12 {
                    1.0
                } else {
                    (std::f64::consts::PI * arg).sin() / (std::f64::consts::PI * arg)
                };
                let r = x / half_f;
                let w = bessel_i0(KAISER_BETA * (1.0 - r * r).max(0.0).sqrt()) / i0b;
                (2.0 * fc * sinc * w) as f32
            })
            .collect();
        Self {
            half,
            table,
            buf: Vec::with_capacity(16384),
            buf_start: 0,
            pos: 0.0,
            out_count,
        }
    }

    /// One past the absolute index of the last pushed input sample.
    pub fn in_end(&self) -> u64 {
        self.buf_start + self.buf.len() as u64
    }

    /// Number of output samples produced so far (= absolute index of the next one).
    pub fn out_count(&self) -> u64 {
        self.out_count
    }

    /// Output position (fractional) at which absolute input index `input` lands when the step
    /// stays at `step` input samples per output sample.
    pub fn landing(&self, input: f64, step: f64) -> f64 {
        self.out_count as f64 + (input - self.pos) / step
    }

    pub fn push(&mut self, samples: &[f32]) {
        self.buf.extend_from_slice(samples);
    }

    pub fn push_zeros(&mut self, n: usize) {
        self.buf.resize(self.buf.len() + n, 0.0);
    }

    #[inline]
    fn kernel(&self, x: f64) -> f32 {
        let t = x.abs() * OVERSAMPLE as f64;
        let i = t as usize;
        if i + 1 >= self.table.len() {
            return 0.0;
        }
        let frac = (t - i as f64) as f32;
        self.table[i] + (self.table[i + 1] - self.table[i]) * frac
    }

    /// Produce every output sample whose kernel support is fully available. `step` is input
    /// samples per output sample (in_rate / out_rate, possibly slewed).
    pub fn process(&mut self, step: f64, out: &mut Vec<f32>) {
        let half = self.half as i64;
        let end = self.in_end() as i64;
        loop {
            let center = self.pos.floor() as i64;
            if center + half >= end {
                break;
            }
            let mut acc = 0.0f32;
            let lo = (center - half + 1).max(self.buf_start as i64);
            for k in lo..=center + half {
                let s = self.buf[(k - self.buf_start as i64) as usize];
                if s != 0.0 {
                    acc += s * self.kernel(self.pos - k as f64);
                }
            }
            out.push(acc);
            self.pos += step;
            self.out_count += 1;
        }
        // Drop history no future output can reach.
        let keep_from = (self.pos.floor() as i64 - half).max(0) as u64;
        if keep_from > self.buf_start + 8192 {
            let drop = (keep_from - self.buf_start) as usize;
            self.buf.drain(..drop);
            self.buf_start = keep_from;
        }
    }

    /// Kernel half width in input samples (look-ahead needed before an output is produced).
    pub fn half_width(&self) -> usize {
        self.half
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(rate: f64, freq: f64, n: usize, phase0: f64) -> Vec<f32> {
        (0..n)
            .map(|i| {
                (2.0 * std::f64::consts::PI * freq * i as f64 / rate + phase0).sin() as f32 * 0.5
            })
            .collect()
    }

    #[test]
    fn downsample_48k_keeps_passband_and_phase() {
        let mut r = Resampler::new(48000.0, 16000.0);
        let x = sine(48000.0, 1000.0, 48000, 0.0);
        let mut out = Vec::new();
        for chunk in x.chunks(480) {
            r.push(chunk);
            r.process(3.0, &mut out);
        }
        assert!(out.len() > 15800);
        // Output sample k sits at input position 3k, i.e. time k/16000: compare with the ideal.
        let ideal = sine(16000.0, 1000.0, out.len(), 0.0);
        let err = out[200..out.len() - 10]
            .iter()
            .zip(&ideal[200..])
            .map(|(a, b)| (a - b).abs())
            .fold(0.0f32, f32::max);
        assert!(err < 2e-3, "max error {err}");
    }

    #[test]
    fn rejects_alias_band() {
        // 12 kHz at 48 kHz would alias to 4 kHz at 16 kHz; it must be strongly attenuated.
        let mut r = Resampler::new(48000.0, 16000.0);
        let x = sine(48000.0, 12000.0, 48000, 0.3);
        let mut out = Vec::new();
        r.push(&x);
        r.process(3.0, &mut out);
        let peak = out[200..].iter().fold(0.0f32, |m, v| m.max(v.abs()));
        assert!(peak < 1e-3, "alias peak {peak}");
    }

    #[test]
    fn landing_matches_output_position() {
        let mut r = Resampler::new(44100.0, 16000.0);
        let step = 44100.0 / 16000.0 * 1.003;
        let mut out = Vec::new();
        let mut x = vec![0.0f32; 44100];
        x[30000] = 1.0; // impulse
        let predicted = r.landing(30000.0, step);
        r.push(&x);
        r.process(step, &mut out);
        let argmax = out
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap())
            .unwrap()
            .0;
        assert!(
            (argmax as f64 - predicted).abs() <= 1.0,
            "{argmax} vs {predicted}"
        );
    }

    #[test]
    fn upsample_8k() {
        let mut r = Resampler::new(8000.0, 16000.0);
        let x = sine(8000.0, 440.0, 8000, 0.0);
        let mut out = Vec::new();
        r.push(&x);
        r.process(0.5, &mut out);
        let ideal = sine(16000.0, 440.0, out.len(), 0.0);
        let err = out[400..]
            .iter()
            .zip(&ideal[400..])
            .map(|(a, b)| (a - b).abs())
            .fold(0.0f32, f32::max);
        assert!(err < 3e-3, "max error {err}");
    }
}
