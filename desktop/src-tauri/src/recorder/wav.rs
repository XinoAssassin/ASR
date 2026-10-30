//! Incremental 16-bit PCM WAV writer whose header is patched periodically, so a crash leaves a
//! playable file, plus repair / streaming read for recovery and FLAC conversion.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
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

/// Where a [`WavWriter`] puts its bytes (a `File`; tests inject failing storage).
pub trait Storage: Write + Seek + Send {
    fn sync_data(&mut self) -> io::Result<()>;
}

impl Storage for File {
    fn sync_data(&mut self) -> io::Result<()> {
        File::sync_data(self)
    }
}

impl<S: Storage + ?Sized> Storage for Box<S> {
    fn sync_data(&mut self) -> io::Result<()> {
        (**self).sync_data()
    }
}

/// Flush to the file once this much is buffered.
const FLUSH_AT: usize = 256 * 1024;

/// Appends interleaved PCM. Bytes are buffered here and leave the buffer only once the OS has
/// accepted them, so a failed / short write (disk full, network share gone) loses nothing: the
/// unwritten tail stays buffered and the next `write` / `checkpoint` retries it exactly where
/// the file ends. The header is only ever patched to the length actually on disk.
pub struct WavWriter<F: Storage = File> {
    file: F,
    channels: u16,
    rate: u32,
    buf: Vec<u8>,
    /// PCM bytes the OS has accepted (the file is `HEADER_LEN + flushed` long).
    flushed: u64,
    patched_len: u64,
    /// The file position is not known to be at the end of the data (after a header patch or
    /// an error): seek before the next data write.
    reseek: bool,
}

impl WavWriter<File> {
    pub fn create(path: &Path, channels: u16, rate: u32) -> io::Result<Self> {
        let f = OpenOptions::new().write(true).create_new(true).open(path)?;
        let w = Self::with_storage(f, channels, rate)?;
        w.file.sync_all()?;
        Ok(w)
    }
}

impl<F: Storage> WavWriter<F> {
    /// Write the initial header to `file` (positioned at 0).
    pub fn with_storage(mut file: F, channels: u16, rate: u32) -> io::Result<Self> {
        file.write_all(&header(channels, rate, 0))?;
        Ok(Self {
            file,
            channels,
            rate,
            buf: Vec::with_capacity(FLUSH_AT),
            flushed: 0,
            patched_len: 0,
            reseek: false,
        })
    }

    /// Append interleaved samples. They are always accepted (buffered); an error only means
    /// the buffered data could not be flushed yet, it is retried by the next call.
    pub fn write(&mut self, samples: &[i16]) -> io::Result<()> {
        debug_assert_eq!(samples.len() % self.channels as usize, 0);
        self.buf.reserve(samples.len() * 2);
        for s in samples {
            self.buf.extend_from_slice(&s.to_le_bytes());
        }
        if self.buf.len() >= FLUSH_AT {
            self.flush_data()?;
        }
        Ok(())
    }

    /// Hand buffered bytes to the OS; on error the unwritten rest stays buffered.
    fn flush_data(&mut self) -> io::Result<()> {
        if self.buf.is_empty() {
            return Ok(());
        }
        if self.reseek {
            self.file.seek(SeekFrom::Start(HEADER_LEN + self.flushed))?;
            self.reseek = false;
        }
        let mut done = 0;
        let res = loop {
            if done == self.buf.len() {
                break Ok(());
            }
            match self.file.write(&self.buf[done..]) {
                Ok(0) => {
                    break Err(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "recording file accepts no more data",
                    ))
                }
                Ok(n) => done += n,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => break Err(e),
            }
        };
        self.buf.drain(..done);
        self.flushed += done as u64;
        if res.is_err() {
            self.reseek = true;
        }
        if self.buf.is_empty() && self.buf.capacity() > 4 * FLUSH_AT {
            // A long outage grew the buffer; give the memory back.
            self.buf.shrink_to(FLUSH_AT);
        }
        res
    }

    /// Frames on disk (accepted by the OS).
    pub fn frames(&self) -> u64 {
        self.flushed / (self.channels as u64 * 2)
    }

    /// Frames accepted by `write` but not yet on disk.
    pub fn pending_frames(&self) -> u64 {
        self.buf.len() as u64 / (self.channels as u64 * 2)
    }

    /// Flush buffered data and patch the RIFF/data sizes. `sync` also forces it to disk.
    pub fn checkpoint(&mut self, sync: bool) -> io::Result<()> {
        self.flush_data()?;
        if self.flushed != self.patched_len {
            let h = header(self.channels, self.rate, clamp_u32(self.flushed));
            self.reseek = true;
            self.file.seek(SeekFrom::Start(0))?;
            self.file.write_all(&h)?;
            self.file.seek(SeekFrom::Start(HEADER_LEN + self.flushed))?;
            self.reseek = false;
            self.patched_len = self.flushed;
        }
        if sync {
            self.file.sync_data()?;
        }
        Ok(())
    }

    /// Final checkpoint; returns the frames on disk. On error the data that reached the disk
    /// is still a valid (repairable) WAV.
    pub fn finish(mut self) -> io::Result<u64> {
        self.checkpoint(true)?;
        Ok(self.frames())
    }
}

impl<F: Storage> Drop for WavWriter<F> {
    /// Best effort (an unwinding recorder thread): whatever is buffered goes to the file. The
    /// header may then lag behind; [`repair`] re-derives it from the file length.
    fn drop(&mut self) {
        let _ = self.flush_data();
    }
}

/// Largest data size a RIFF header can state; [`WavWriter`] saturates here past ~4 GiB.
const SATURATED: u32 = u32::MAX - 36;

fn clamp_u32(v: u64) -> u32 {
    v.min(SATURATED as u64) as u32
}

/// PCM payload length: the header's value, or (header saturated: > 4 GiB) the file length.
fn data_len(header_len: u32, file_len: u64, block: u64) -> u64 {
    let from_file = (file_len.saturating_sub(HEADER_LEN) / block) * block;
    if header_len >= SATURATED {
        from_file
    } else {
        ((header_len as u64 / block) * block).min(from_file)
    }
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
        if &h[0..4] != b"RIFF" || &h[8..12] != b"WAVE" || &h[36..40] != b"data" {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "not a recorder WAV",
            ));
        }
        let channels = u16::from_le_bytes([h[22], h[23]]);
        let rate = u32::from_le_bytes([h[24], h[25], h[26], h[27]]);
        if channels == 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "0 channels"));
        }
        let block = channels as u64 * 2;
        let data = data_len(
            u32::from_le_bytes([h[40], h[41], h[42], h[43]]),
            f.metadata()?.len(),
            block,
        );
        Ok(Self {
            file: io::BufReader::with_capacity(256 * 1024, f),
            info: WavInfo {
                channels,
                rate,
                frames: data / block,
            },
            remaining: data,
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
        w.flush_data().unwrap();
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

    /// `open` and `repair` agree on what a recorder WAV is: a RIFF file that is not `WAVE` is
    /// refused by both.
    #[test]
    fn open_checks_the_wave_tag() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("x.wav");
        let mut w = WavWriter::create(&p, 1, 16000).unwrap();
        w.write(&[1i16; 100]).unwrap();
        w.finish().unwrap();
        assert_eq!(WavReader::open(&p).unwrap().info.frames, 100);
        let mut b = std::fs::read(&p).unwrap();
        b[8..12].copy_from_slice(b"AVI ");
        std::fs::write(&p, &b).unwrap();
        assert!(WavReader::open(&p).is_err());
        assert!(repair(&p).is_err());
    }

    /// In-memory storage that fails or accepts only part of a write on demand.
    struct Flaky {
        data: io::Cursor<Vec<u8>>,
        fail: std::sync::Arc<std::sync::atomic::AtomicU32>,
    }

    impl Write for Flaky {
        fn write(&mut self, b: &[u8]) -> io::Result<usize> {
            use std::sync::atomic::Ordering;
            match self.fail.load(Ordering::SeqCst) {
                0 => self.data.write(b),
                n => {
                    self.fail.store(n - 1, Ordering::SeqCst);
                    if n % 2 == 0 && b.len() > 3 {
                        // short write of an odd byte count (splits a sample)
                        self.data.write(&b[..3])
                    } else {
                        Err(io::Error::other("disk full"))
                    }
                }
            }
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    impl Seek for Flaky {
        fn seek(&mut self, p: SeekFrom) -> io::Result<u64> {
            self.data.seek(p)
        }
    }

    impl Storage for Flaky {
        fn sync_data(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn failed_and_short_writes_lose_and_duplicate_nothing() {
        use std::sync::atomic::{AtomicU32, Ordering};
        let fail = std::sync::Arc::new(AtomicU32::new(0));
        let st = Flaky {
            data: io::Cursor::new(Vec::new()),
            fail: fail.clone(),
        };
        let mut w = WavWriter::with_storage(st, 2, 16000).unwrap();
        let mut expect: Vec<i16> = Vec::new();
        let mut k = 0i16;
        let mut errors = 0;
        for round in 0..40 {
            if round % 7 == 3 {
                fail.store(5, Ordering::SeqCst);
            }
            let chunk: Vec<i16> = (0..2 * 1000)
                .map(|_| {
                    k = k.wrapping_add(1);
                    k
                })
                .collect();
            expect.extend_from_slice(&chunk);
            if w.write(&chunk).is_err() {
                errors += 1;
            }
            if w.checkpoint(round % 5 == 0).is_err() {
                errors += 1;
            }
        }
        assert!(errors > 0);
        fail.store(0, Ordering::SeqCst);
        w.checkpoint(true).unwrap();
        assert_eq!(w.pending_frames(), 0);
        assert_eq!(w.frames(), expect.len() as u64 / 2);
        let bytes = w.file.data.get_ref().clone();
        assert_eq!(bytes.len() as u64, HEADER_LEN + expect.len() as u64 * 2);
        assert_eq!(
            u32::from_le_bytes(bytes[40..44].try_into().unwrap()) as usize,
            expect.len() * 2
        );
        let got: Vec<i16> = bytes[44..]
            .chunks(2)
            .map(|c| i16::from_le_bytes([c[0], c[1]]))
            .collect();
        assert!(got == expect, "payload differs");
    }

    #[test]
    fn data_len_uses_file_length_when_header_saturated() {
        assert_eq!(data_len(SATURATED, 5_000_000_000, 4), 4_999_999_956);
        assert_eq!(data_len(u32::MAX, 5_000_000_000, 4), 4_999_999_956);
        assert_eq!(data_len(400, 10_000, 4), 400);
        assert_eq!(data_len(400, 44 + 100, 4), 100); // truncated file
    }

    /// A > 4 GiB (sparse) recording: repair and the reader both see every frame.
    #[cfg(target_os = "linux")]
    #[test]
    fn over_4gib_wav_is_read_in_full() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("big.partial.wav");
        let w = WavWriter::create(&p, 2, 16000).unwrap();
        drop(w);
        let len = 5_000_000_000u64;
        OpenOptions::new()
            .write(true)
            .open(&p)
            .unwrap()
            .set_len(len)
            .unwrap();
        let frames = (len - HEADER_LEN) / 4;
        let info = repair(&p).unwrap();
        assert_eq!(info.frames, frames);
        let mut r = WavReader::open(&p).unwrap();
        assert_eq!(r.info.frames, frames);
        assert_eq!(r.remaining, frames * 4);
        let mut buf = Vec::new();
        assert_eq!(r.read_bytes(10, &mut buf).unwrap(), 10);
    }
}
