//! Meeting end detection (D78): which output frames carry audio.
//!
//! Runs on the interleaved 16 kHz frames exactly as they go to the partial WAV, so a frame index
//! here is a frame index in the file (the auto-stop trim cuts at it). Silence-filled gaps (a lost
//! device, an idle loopback) are digital zeros and count as silence.
//!
//! Per channel, 100 ms blocks are compared with an adaptive noise floor: a block carries audio
//! when its RMS is 12 dB above the floor, clamped to [-60 dBFS, -34 dBFS]. So a steady mic hiss
//! or fan is ignored, anything louder than -34 dBFS always counts, and digital silence with a
//! quiet remote speaker still counts from -60 dBFS. The floor follows quieter blocks at once and
//! rises by 6 dB per 10 s otherwise. Wrongly seeing audio only delays an auto-stop; wrongly
//! seeing silence would end a meeting recording early, so the thresholds lean to "audio".

/// Block length in frames (100 ms at 16 kHz).
pub const BLOCK: usize = 1600;
/// Lowest threshold (RMS, full scale = 1): -60 dBFS.
pub const ABS_FLOOR: f32 = 0.001;
/// Highest threshold: a block above -34 dBFS RMS always carries audio.
pub const LOUD: f32 = 0.02;
/// Audio = this factor above the noise floor (+12 dB).
const ABOVE_FLOOR: f32 = 4.0;
/// Floor rise per block without a quieter one: +0.06 dB = +6 dB per 10 s.
const FLOOR_RISE: f32 = 1.006_932;
const FLOOR_MIN: f32 = 1e-5;

pub struct Activity {
    channels: usize,
    /// Sum of squares of the current block, per channel.
    acc: Vec<f64>,
    in_block: usize,
    floors: Vec<Option<f32>>,
    frames: u64,
    last_audio_end: u64,
}

impl Activity {
    pub fn new(channels: usize) -> Self {
        let channels = channels.max(1);
        Self {
            channels,
            acc: vec![0.0; channels],
            in_block: 0,
            floors: vec![None; channels],
            frames: 0,
            last_audio_end: 0,
        }
    }

    /// Feed interleaved i16 frames (whole frames).
    pub fn push(&mut self, interleaved: &[i16]) {
        for frame in interleaved.chunks_exact(self.channels) {
            for (a, &x) in self.acc.iter_mut().zip(frame) {
                let v = x as f64 / 32768.0;
                *a += v * v;
            }
            self.in_block += 1;
            self.frames += 1;
            if self.in_block == BLOCK {
                self.close_block();
            }
        }
    }

    /// Judge the incomplete last block too (at the end of the recording).
    pub fn flush(&mut self) {
        if self.in_block > 0 {
            self.close_block();
        }
    }

    fn close_block(&mut self) {
        let n = self.in_block.max(1) as f64;
        let mut audio = false;
        for (a, floor) in self.acc.iter_mut().zip(self.floors.iter_mut()) {
            let rms = (*a / n).sqrt() as f32;
            *a = 0.0;
            let f = match *floor {
                None => rms,
                Some(f) if rms < f => rms,
                Some(f) => f * FLOOR_RISE,
            }
            .max(FLOOR_MIN);
            // Compared with the floor *before* this block raised it.
            let threshold = (floor.unwrap_or(rms) * ABOVE_FLOOR).clamp(ABS_FLOOR, LOUD);
            if rms > threshold {
                audio = true;
            }
            *floor = Some(f);
        }
        self.in_block = 0;
        if audio {
            self.last_audio_end = self.frames;
        }
    }

    /// Frames fed so far.
    pub fn frames(&self) -> u64 {
        self.frames
    }

    /// End (exclusive frame index) of the last block that carried audio; 0 = none yet.
    pub fn last_audio_end(&self) -> u64 {
        self.last_audio_end
    }

    /// Frames since the last audio (or since the start).
    pub fn silent_frames(&self) -> u64 {
        self.frames - self.last_audio_end
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn block(level: f32, ch: usize) -> Vec<i16> {
        // a 400 Hz square wave at `level` (RMS = level)
        (0..BLOCK)
            .flat_map(|i| {
                let v = if (i / 20) % 2 == 0 { level } else { -level };
                std::iter::repeat_n((v * 32767.0) as i16, ch)
            })
            .collect()
    }

    #[test]
    fn silence_speech_and_steady_noise() {
        let mut a = Activity::new(2);
        for _ in 0..10 {
            a.push(&block(0.0, 2));
        }
        assert_eq!(a.last_audio_end(), 0);
        assert_eq!(a.silent_frames(), 10 * BLOCK as u64);
        // a quiet voice over digital silence (-50 dBFS) counts
        a.push(&block(0.003, 2));
        assert_eq!(a.last_audio_end(), 11 * BLOCK as u64);
        // steady hiss at -50 dBFS: once the floor has risen to it (~1 min from digital
        // silence) it is silence
        for _ in 0..800 {
            a.push(&block(0.003, 2));
        }
        let before = a.last_audio_end();
        for _ in 0..100 {
            a.push(&block(0.003, 2));
        }
        assert_eq!(a.last_audio_end(), before, "steady noise is not audio");
        // speech 12+ dB above the hiss counts again
        a.push(&block(0.02, 2));
        assert_eq!(a.last_audio_end(), a.frames());
    }

    #[test]
    fn loud_steady_audio_always_counts() {
        let mut a = Activity::new(1);
        for _ in 0..3000 {
            a.push(&block(0.1, 1));
        }
        assert_eq!(a.silent_frames(), 0);
    }

    #[test]
    fn one_channel_with_audio_is_enough_and_flush_judges_the_tail() {
        let mut a = Activity::new(2);
        for _ in 0..5 {
            a.push(&block(0.0, 2));
        }
        // channel 1 only, half a block
        let mut half: Vec<i16> = Vec::new();
        for i in 0..BLOCK / 2 {
            half.push(0);
            half.push(if (i / 20) % 2 == 0 { 3000 } else { -3000 });
        }
        a.push(&half);
        assert_eq!(a.last_audio_end(), 0, "incomplete block not judged yet");
        a.flush();
        assert_eq!(a.last_audio_end(), a.frames());
        assert_eq!(a.frames(), 5 * BLOCK as u64 + BLOCK as u64 / 2);
    }
}
