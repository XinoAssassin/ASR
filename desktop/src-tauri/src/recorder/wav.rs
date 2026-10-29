//! Incremental 16-bit PCM WAV writer whose header is patched periodically, so a crash leaves a
//! playable file, plus repair / streaming read for recovery and FLAC conversion.

use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::Path;

pub const HEADER_LEN: u64 = 44;

fn header(channels: u16, rate: u32, data_len: u32) -> [u8; 44] {
    let block_align = channels * 2;
    let mut h = [0u8; 44];
    h[0..4].copy_from_slice(b"RIFF");
    h[4..8].copy_from_slice(&data_len.saturating_add(36).to_le_bytes());
    h[8..12].copy_from_slice(b"WAVE");
    h[12..16].copy_from_slice(b"fmt ");
    h[16..20].copy_from_slice(&16u32.to_le_bytes());
    h[20..22].copy_from_slice(&1u16.to_le_bytes());
    h[22..24].copy_from_slice(&channels.to_le_bytes());
    h[24..28].copy_from_slice(&rate.to_le_bytes());
    h[28..32].copy_from_slice(&(rate * block_align as u32).to_le_bytes());
    h[32..34].copy_from_slice(&block_align.to_le_bytes());
    h[34..36].copy_from_slice(&16u16.to_le_bytes());
    h[36..40].copy_from_slice(b"data");
    h[40..44].copy_from_slice(&data_len.to_le_bytes());
    h
}

pub struct WavWriter {
    file: BufWriter<File>,
    channels: u16,
    rate: u32,
    data_len: u64,
    patched_len: u64,
}

impl WavWriter {
    pub fn create(path: &Path, channels: u16, rate: u32) -> io::Result<Self> {
        let mut f = OpenOptions::new().write(true).create_new(true).open(path)?;
        f.write_all(&header(channels, rate, 0))?;
        f.sync_all()?;
        Ok(Self {
            file: BufWriter::with_capacity(256 * 1024, f),
            channels,
            rate,
            data_len: 0,
            patched_len: 0,
        })
    }

    /// Append interleaved samples.
    pub fn write(&mut self, samples: &[i16]) -> io::Result<()> {
        debug_assert_eq!(samples.len() % self.channels as usize, 0);
        let mut bytes = Vec::with_capacity(samples.len() * 2);
        for s in samples {
            bytes.extend_from_slice(&s.to_le_bytes());
        }
        self.file.write_all(&bytes)?;
        self.data_len += bytes.len() as u64;
        Ok(())
    }

    pub fn frames(&self) -> u64 {
        self.data_len / (self.channels as u64 * 2)
    }

    /// Flush buffered data and patch the RIFF/data sizes. `sync` also forces it to disk.
    pub fn checkpoint(&mut self, sync: bool) -> io::Result<()> {
        self.file.flush()?;
        if self.data_len != self.patched_len {
            let f = self.file.get_mut();
            let h = header(self.channels, self.rate, clamp_u32(self.data_len));
            f.seek(SeekFrom::Start(0))?;
            f.write_all(&h)?;
            f.seek(SeekFrom::End(0))?;
            self.patched_len = self.data_len;
        }
        if sync {
            self.file.get_ref().sync_data()?;
        }
        Ok(())
    }

    pub fn finish(mut self) -> io::Result<u64> {
        self.checkpoint(true)?;
        Ok(self.frames())
    }
}

fn clamp_u32(v: u64) -> u32 {
    v.min(u32::MAX as u64 - 36) as u32
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WavInfo {
    pub channels: u16,
    pub rate: u32,
    pub frames: u64,
}

/// Re-derive the sizes of a WAV written by [`WavWriter`] from the file length (after a crash)
/// and patch the header. Trailing partial frames are cut off.
pub fn repair(path: &Path) -> io::Result<WavInfo> {
    let mut f = OpenOptions::new().read(true).write(true).open(path)?;
    let mut h = [0u8; 44];
    f.read_exact(&mut h)?;
    if &h[0..4] != b"RIFF" || &h[8..12] != b"WAVE" || &h[36..40] != b"data" {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "not a recorder WAV",
        ));
    }
    let channels = u16::from_le_bytes([h[22], h[23]]);
    let rate = u32::from_le_bytes([h[24], h[25], h[26], h[27]]);
    if channels == 0 || channels > 8 || u16::from_le_bytes([h[34], h[35]]) != 16 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unsupported WAV layout",
        ));
    }
    let len = f.metadata()?.len();
    let block = channels as u64 * 2;
    let data = (len.saturating_sub(HEADER_LEN) / block) * block;
    f.set_len(HEADER_LEN + data)?;
    f.seek(SeekFrom::Start(0))?;
    f.write_all(&header(channels, rate, clamp_u32(data)))?;
    f.sync_all()?;
    Ok(WavInfo {
        channels,
        rate,
        frames: data / block,
    })
}

/// Streaming reader over the PCM payload of a recorder WAV (sizes taken from the header).
pub struct WavReader {
    file: io::BufReader<File>,
    pub info: WavInfo,
    remaining: u64,
}

impl WavReader {
    pub fn open(path: &Path) -> io::Result<Self> {
        let mut f = File::open(path)?;
        let mut h = [0u8; 44];
        f.read_exact(&mut h)?;
        if &h[0..4] != b"RIFF" || &h[36..40] != b"data" {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "not a recorder WAV",
            ));
        }
        let channels = u16::from_le_bytes([h[22], h[23]]);
        let rate = u32::from_le_bytes([h[24], h[25], h[26], h[27]]);
        let data = u32::from_le_bytes([h[40], h[41], h[42], h[43]]) as u64;
        let block = channels as u64 * 2;
        Ok(Self {
            file: io::BufReader::with_capacity(256 * 1024, f),
            info: WavInfo {
                channels,
                rate,
                frames: data / block,
            },
            remaining: (data / block) * block,
        })
    }

    /// Read up to `max_frames` frames as raw little-endian bytes into `buf` (cleared first).
    pub fn read_bytes(&mut self, max_frames: usize, buf: &mut Vec<u8>) -> io::Result<usize> {
        let block = self.info.channels as usize * 2;
        let n = ((max_frames * block) as u64).min(self.remaining) as usize;
        buf.resize(n, 0);
        self.file.read_exact(buf)?;
        self.remaining -= n as u64;
        Ok(n / block)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_is_patched_and_crash_is_repairable() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("a.partial.wav");
        let mut w = WavWriter::create(&p, 2, 16000).unwrap();
        w.write(&[1, -1, 2, -2]).unwrap();
        w.checkpoint(false).unwrap();
        let r = WavReader::open(&p).unwrap();
        assert_eq!(r.info.frames, 2);
        // More data written after the last checkpoint, then a "crash" (no finish) with a torn frame.
        w.write(&[3, -3, 4, -4]).unwrap();
        w.file.flush().unwrap();
        drop(w);
        {
            let mut f = OpenOptions::new().append(true).open(&p).unwrap();
            f.write_all(&[9, 9]).unwrap();
        }
        assert_eq!(WavReader::open(&p).unwrap().info.frames, 2);
        let info = repair(&p).unwrap();
        assert_eq!(
            info,
            WavInfo {
                channels: 2,
                rate: 16000,
                frames: 4
            }
        );
        let mut r = WavReader::open(&p).unwrap();
        let mut buf = Vec::new();
        assert_eq!(r.read_bytes(100, &mut buf).unwrap(), 4);
        assert_eq!(&buf[12..16], &[4, 0, 0xfc, 0xff]);
        assert_eq!(std::fs::metadata(&p).unwrap().len(), 44 + 16);
    }

    #[test]
    fn create_refuses_to_overwrite() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("x.wav");
        std::fs::write(&p, b"keep").unwrap();
        assert!(WavWriter::create(&p, 1, 16000).is_err());
        assert_eq!(std::fs::read(&p).unwrap(), b"keep");
    }
}
