//! Streaming WAV -> FLAC (16-bit) conversion with a lossless verify pass.
//!
//! flacenc's high-level API keeps the whole stream in memory; here frames are encoded one block
//! at a time and written straight to disk, and the STREAMINFO header (sizes, total samples, MD5)
//! is rewritten at the end. Memory stays flat for multi-hour recordings.

use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use std::path::Path;

use flacenc::bitsink::ByteSink;
use flacenc::component::{BitRepr, Stream};
use flacenc::error::Verify;
use flacenc::source::{Context, Fill, FrameBuf};

use super::wav::{WavInfo, WavReader};

const BLOCK: usize = 4096;

fn err<E: std::fmt::Debug>(e: E) -> io::Error {
    io::Error::other(format!("{e:?}"))
}

fn header_bytes(stream: &Stream) -> io::Result<Vec<u8>> {
    let mut sink = ByteSink::new();
    stream.write(&mut sink).map_err(err)?;
    Ok(sink.as_slice().to_vec())
}

/// Encode `wav` into `flac` (created / truncated). Returns the WAV info.
pub fn encode(wav: &Path, flac: &Path) -> io::Result<WavInfo> {
    let mut reader = WavReader::open(wav)?;
    let info = reader.info;
    let channels = info.channels as usize;
    // Without the `par` feature the encoder is single-threaded.
    let config = flacenc::config::Encoder::default()
        .into_verified()
        .map_err(|e| err(e.1))?;
    let mut stream = Stream::new(info.rate as usize, channels, 16).map_err(err)?;
    let placeholder = header_bytes(&stream)?;
    let mut out = BufWriter::with_capacity(
        256 * 1024,
        OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(flac)?,
    );
    out.write_all(&placeholder)?;

    let mut fb = FrameBuf::with_size(channels, BLOCK).map_err(err)?;
    let mut ctx = Context::new(16, channels);
    let mut bytes = Vec::new();
    let mut sink = ByteSink::new();
    let mut frame_no = 0usize;
    loop {
        let n = reader.read_bytes(BLOCK, &mut bytes)?;
        if n == 0 {
            break;
        }
        fb.fill_le_bytes(&bytes, 2).map_err(err)?;
        ctx.fill_le_bytes(&bytes, 2).map_err(err)?;
        let frame = flacenc::encode_fixed_size_frame(&config, &fb, frame_no, stream.stream_info())
            .map_err(err)?;
        sink.clear();
        frame.write(&mut sink).map_err(err)?;
        out.write_all(sink.as_slice())?;
        stream.stream_info_mut().update_frame_info(&frame);
        frame_no += 1;
    }
    if frame_no == 0 {
        let si = stream.stream_info_mut();
        si.set_block_sizes(BLOCK, BLOCK).map_err(err)?;
        si.set_frame_sizes(0, 0).map_err(err)?;
    }
    stream.stream_info_mut().set_md5_digest(&ctx.md5_digest());
    let header = header_bytes(&stream)?;
    debug_assert_eq!(header.len(), placeholder.len());
    let mut f = out.into_inner().map_err(|e| e.into_error())?;
    f.seek(SeekFrom::Start(0))?;
    f.write_all(&header)?;
    f.sync_all()?;
    Ok(info)
}

/// Decode `flac` and compare it sample by sample with `wav`. Errors on any mismatch.
pub fn verify(flac: &Path, wav: &Path) -> io::Result<u64> {
    let mut reader = WavReader::open(wav)?;
    let info = reader.info;
    let mut dec = claxon::FlacReader::new(File::open(flac)?).map_err(err)?;
    let si = dec.streaminfo();
    if si.channels != info.channels as u32
        || si.sample_rate != info.rate
        || si.bits_per_sample != 16
        || si.samples.unwrap_or(0) != info.frames
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "FLAC header mismatch: {} ch {} Hz {:?} frames (expected {} / {} / {})",
                si.channels, si.sample_rate, si.samples, info.channels, info.rate, info.frames
            ),
        ));
    }
    let mut wav_bytes = Vec::new();
    let mut wav_pos = 0usize;
    let mut checked = 0u64;
    let mut blocks = dec.blocks();
    let mut buf = Vec::new();
    loop {
        let block = match blocks.read_next_or_eof(buf) {
            Ok(Some(b)) => b,
            Ok(None) => break,
            Err(e) => return Err(err(e)),
        };
        for i in 0..block.duration() {
            if wav_pos >= wav_bytes.len() {
                reader.read_bytes(BLOCK, &mut wav_bytes)?;
                wav_pos = 0;
                if wav_bytes.is_empty() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "FLAC longer than WAV",
                    ));
                }
            }
            for c in 0..block.channels() {
                let w = i16::from_le_bytes([wav_bytes[wav_pos], wav_bytes[wav_pos + 1]]);
                wav_pos += 2;
                if block.sample(c, i) != w as i32 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("sample mismatch at frame {}", checked + i as u64),
                    ));
                }
            }
        }
        checked += block.duration() as u64;
        buf = block.into_buffer();
    }
    if checked != info.frames {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("FLAC has {checked} frames, WAV {}", info.frames),
        ));
    }
    Ok(checked)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::recorder::wav::WavWriter;

    #[test]
    fn roundtrip_is_lossless_and_mismatch_is_detected() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("a.wav");
        let flac = dir.path().join("a.flac");
        let mut w = WavWriter::create(&wav, 2, 16000).unwrap();
        let n = 16000 * 3 + 123;
        let mut s = Vec::with_capacity(n * 2);
        let mut seed = 1u32;
        for i in 0..n {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            s.push(((i as f32 * 0.05).sin() * 8000.0) as i16);
            s.push((seed >> 20) as i16 - 2048);
        }
        w.write(&s).unwrap();
        w.finish().unwrap();
        let info = encode(&wav, &flac).unwrap();
        assert_eq!(info.frames, n as u64);
        assert_eq!(verify(&flac, &wav).unwrap(), n as u64);
        assert!(std::fs::metadata(&flac).unwrap().len() < std::fs::metadata(&wav).unwrap().len());

        // Corrupt one sample in the WAV: verify must fail.
        let mut b = std::fs::read(&wav).unwrap();
        b[44 + 4000] ^= 0x40;
        std::fs::write(&wav, b).unwrap();
        assert!(verify(&flac, &wav).is_err());
    }

    #[test]
    fn empty_and_mono() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("e.wav");
        let flac = dir.path().join("e.flac");
        WavWriter::create(&wav, 1, 16000).unwrap().finish().unwrap();
        encode(&wav, &flac).unwrap();
        assert_eq!(verify(&flac, &wav).unwrap(), 0);
    }
}
