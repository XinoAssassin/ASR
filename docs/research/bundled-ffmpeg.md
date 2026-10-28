# Research: Bundled FFmpeg options for the ASR backend (Python 3.12, FastAPI, Win/Linux/Docker)

## Summary
Use **PyAV (`av`)** as the primary decoder/prober: its PyPI wheels bundle FFmpeg 9.0.2 shared libraries for Windows x64/ARM64, manylinux/musllinux x86_64+aarch64 and macOS, require Python >= 3.12 (abi3), and expose an in-process API that covers decode-to-16 kHz-mono, probing, channel selection and transcoding for a browser player. Use **imageio-ffmpeg** as the fallback: it ships a static `ffmpeg` CLI inside the wheel (no ffprobe) and is driven via `subprocess`. Both bundle GPL-configured FFmpeg builds (x264/x265 linked), which is fine for a self-hosted/SaaS service but matters if you ever redistribute a proprietary desktop build. `python-ffmpeg`, `audioread`, `pydub`, `torchaudio`/`torchcodec` do **not** bundle FFmpeg; `soundfile` and `miniaudio` are solid but cannot read AAC/M4A/MP4.

Date context: PyPI pages were fetched during this research; the newest uploads seen are dated Oct 2026, so treat "latest" as of late 2026.

## Findings

### (1) PyPI packages shipping ffmpeg binaries

1. **Claim:** `imageio-ffmpeg` 0.7.0 (uploaded Oct 28, 2026) ships the `ffmpeg` executable inside platform wheels: win_amd64 31.2 MB, win32 19.7 MB, manylinux2014_x86_64 29.5 MB, manylinux2014_aarch64 25.6 MB, macOS arm64 21.1 MB, macOS x86_64 24.9 MB. No win_arm64, no musllinux. Pure-Python wrapper (BSD-2), called via subprocess; `get_ffmpeg_exe()` returns the path. **Sources:** [PyPI imageio-ffmpeg](https://pypi.org/project/imageio-ffmpeg/), [GitHub README](https://github.com/imageio/imageio-ffmpeg). **Support:** direct evidence. **Confidence:** high.
   - The README itself says: "You should probably use PyAV instead; it is faster and offers more features." (direct quote).
   - It does **not** ship `ffprobe` (README: wheels include "the ffmpeg executable"; issue imageio/imageio-ffmpeg#23). Probing must be done by parsing `ffmpeg -i` stderr or by decoding. **Support:** direct + interpretation. **Confidence:** high.
   - Bundled FFmpeg version for 0.7.0 is not stated in the release notes (empty); query at runtime with `imageio_ffmpeg.get_ffmpeg_version()`. Binaries are static community builds (GPL-configured per search summaries; not verified against the 0.7.0 binary). **Confidence:** medium.

2. **Claim:** `static-ffmpeg` 3.0 (wheel uploaded Jan 16, 2026, 7.9 kB) does **not** bundle binaries in the wheel; it downloads ffmpeg+ffprobe **8.0** on first call (`add_paths()` / `get_or_fetch_platform_executables_else_raise()`) for win32-x64, darwin x64/arm64, linux x64 (Ubuntu 20+), linux_arm64. Binaries hosted in zackees/ffmpeg_bins (BtbN-derived GPL builds per search summary). **Sources:** [PyPI static-ffmpeg](https://pypi.org/project/static-ffmpeg/). **Support:** direct evidence (download-on-first-use, platform list, version 8.0); GPL provenance = interpretation. **Confidence:** high / medium.
   - Docker: needs network at image build time; pre-warm with `RUN python -c "import static_ffmpeg; static_ffmpeg.add_paths()"`. (researcher inference)

3. **Claim:** `ffmpeg-binaries` 1.1.0 (Jan 4, 2026, with PyPI provenance) bundles static ffmpeg+ffprobe in wheels: win_amd64 91.2 MB, manylinux1_x86_64 60.3 MB, macOS universal2 50.3 MB. No Linux aarch64, no win_arm64. API: `ffmpeg.init()`, `run_as_ffmpeg()`, `run_as_ffprobe()`, `FFMPEG_PATH`, `add_to_path()`. Import name `ffmpeg` collides with `ffmpeg-python`; use `ffmpeg-binaries-compat` to import as `ffmpeg_binaries`. **Sources:** [PyPI ffmpeg-binaries](https://pypi.org/project/ffmpeg-binaries/). **Support:** direct. **Confidence:** high. FFmpeg version/license not stated on PyPI page (missing evidence).

4. **Claim:** `ffmpeg-downloader` 0.5.3 (Aug 11, 2026, 49 kB) is a pip-like downloader (`ffdl install [version@build]`) pulling gyan.dev / BtbN / johnvansickle builds; it lets you pick **`lgpl` vs `gpl`** BtbN builds explicitly (e.g. `8.1.1.post2 gpl, gpl-shared, lgpl, lgpl-shared`). Python attrs: `ffdl.ffmpeg_path`, `ffdl.ffprobe_path`, `ffdl.ffmpeg_version`. README says Linux/macOS users are "encouraged to install via the OS package manager", i.e. it is Windows-oriented. **Sources:** [PyPI ffmpeg-downloader](https://pypi.org/project/ffmpeg-downloader/). **Support:** direct. **Confidence:** high.

5. **Claim:** `python-ffmpeg` 2.0.12 (Apr 15, 2024, 14.4 kB wheel) is a sync/async **wrapper only**; it does not ship binaries and expects `ffmpeg` on PATH. Last release > 2 years old. **Sources:** [PyPI python-ffmpeg](https://pypi.org/project/python-ffmpeg/). **Support:** direct (wheel is 14 kB, docs never mention bundled binaries). **Confidence:** high.

### (2) Library bindings that bundle FFmpeg libs

6. **Claim:** `PyAV` 19.0.2 (Oct 21, 2026) requires Python >= 3.12 and provides abi3 wheels with FFmpeg bundled: win_amd64 28.2 MB, win_arm64 20.7 MB, manylinux_2_28 x86_64 35.0 MB, manylinux_2_28 aarch64 32.7 MB, musllinux_1_2 x86_64 36.0 MB / aarch64 33.8 MB, armv7l 44.4 MB, macOS arm64 18.6 MB / x86_64 22.6 MB. "This release supports FFmpeg 9.x." **Sources:** [PyPI av](https://pypi.org/project/av/). **Support:** direct. **Confidence:** high.

7. **Claim:** The bundled libs come from `pyav-ffmpeg`: FFmpeg **9.0.2** built with `--enable-version3`, `--enable-libx264`, `--enable-libx265`, libmp3lame (bwlame), opus, dav1d, svt-av1, vpx, and `--disable-programs` (so **no ffmpeg/ffprobe CLI** inside the wheel). **Sources:** [pyav-ffmpeg README](https://github.com/PyAV-Org/pyav-ffmpeg), [scripts/build-ffmpeg.py](https://raw.githubusercontent.com/PyAV-Org/pyav-ffmpeg/main/scripts/build-ffmpeg.py), [scripts/pkg.py](https://raw.githubusercontent.com/PyAV-Org/pyav-ffmpeg/main/scripts/pkg.py). **Support:** direct for the flags. **Confidence:** high.
   - **Licensing (researcher inference, high confidence):** x264/x265 are GPL and FFmpeg's configure only allows them with `--enable-gpl`, so the FFmpeg libraries inside the PyPI wheels are effectively **GPL-2+/3**, even though PyAV's own code is BSD-3. `source_check` returned no usable passages for this claim (missing-evidence status); the build scripts above were inspected directly instead. For an LGPL stack, build PyAV from source (`pip install av --no-binary av`) against an LGPL FFmpeg.

8. **Claim:** `torchaudio` >= 2.9 routes `torchaudio.load()` through `torchcodec`, and `torchcodec` wheels **do not bundle FFmpeg** — they dynamically load system FFmpeg *shared* libraries (4-7 on Windows; 8 on Linux/macOS); on Windows you must `os.add_dll_directory()` a shared build, static/CLI-only builds do not work. Plus a multi-hundred-MB `torch` dependency. **Sources:** [torchcodec README](https://github.com/meta-pytorch/torchcodec), [torchaudio 2.9 docs](https://docs.pytorch.org/audio/2.9.0/torchaudio.html), [meta-pytorch/torchcodec#1149](https://github.com/meta-pytorch/torchcodec/issues/1149). **Support:** interpretation of search-result summaries; primary pages not fetched. **Confidence:** medium-high. Does not satisfy the "bundled" requirement.

9. **Claim:** `soundfile` 0.14.0 (Jun 6, 2026) bundles libsndfile 1.2.2 (LGPL) in ~1 MB wheels for win_amd64/win32/**win_arm64**, macOS, Linux x86_64 and arm64. Reads WAV/FLAC/OGG(Vorbis)/MP3, **not** AAC/M4A/MP4 (libsndfile has no MPEG-4 support). **Sources:** [PyPI soundfile](https://pypi.org/project/soundfile/). **Support:** direct (news log) + interpretation for format gap. **Confidence:** high.

10. **Claim:** `audioread` 3.1.0 (Oct 26, 2025, MIT) is a backend dispatcher (GStreamer, CoreAudio, MAD, **ffmpeg CLI**, stdlib wave/aifc); it bundles nothing. **Sources:** [PyPI audioread](https://pypi.org/project/audioread/). **Support:** direct. **Confidence:** high.

### (3) Pure-Python / alternative decoders

11. **Claim:** `miniaudio` (pyminiaudio) 1.71 wraps miniaudio C 0.11.25, MIT, Python 3.10+, wheels for win_amd64, macOS x64/arm64, Linux x86_64/aarch64 (no win_arm64). Built-in decoders: WAV, MP3, FLAC, Vorbis; **no AAC/M4A/MP4**. Has `get_file_info()` (duration/channels/rate) and resampling on decode. **Sources:** [PyPI miniaudio](https://pypi.org/project/miniaudio/). **Support:** interpretation of search summaries (PyPI page not fetched). **Confidence:** medium.

12. **Claim:** `pydub` 0.25.1 (Mar 9, 2021) is dormant; without ffmpeg it handles only WAV/raw, and it relies on `audioop` (removed in Python 3.13; fine on 3.12 but a dead end). **Sources:** [PyPI pydub](https://pypi.org/project/pydub/), [jiaaro/pydub#827](https://github.com/jiaaro/pydub/issues/827). **Support:** interpretation. **Confidence:** medium-high.

### (4) Comparison matrix

| Package | Bundled? | Latest (date) | FFmpeg | Win x64 / ARM64 | Linux x64 / arm64 | macOS | Wheel size | License of binary | Call style | Docker |
|---|---|---|---|---|---|---|---|---|---|---|
| av (PyAV) | yes (libs) | 19.0.2 (Oct 2026) | 9.0.2 | yes / yes | glibc 2.28 + musl / yes | yes | 19-36 MB | GPL (x264/x265) | API | excellent (offline) |
| imageio-ffmpeg | yes (ffmpeg exe) | 0.7.0 (Oct 2026) | unverified | yes / no | manylinux2014 / yes | yes | 20-31 MB | GPL static build (unverified) | subprocess | good (offline); no ffprobe |
| ffmpeg-binaries | yes (ffmpeg+ffprobe) | 1.1.0 (Jan 2026) | unverified | yes / no | x64 only / no | universal2 | 50-91 MB | unverified | subprocess | ok (x64 only) |
| static-ffmpeg | download at runtime | 3.0 (Jan 2026) | 8.0 | yes / emulated | yes / yes | yes | 8 kB + download | GPL (BtbN) | subprocess | needs pre-warm + network |
| ffmpeg-downloader | download at runtime | 0.5.3 (Aug 2026) | selectable | yes | yes | yes | 49 kB + download | selectable gpl/lgpl | subprocess | needs pre-warm + network |
| python-ffmpeg | no | 2.0.12 (Apr 2024) | host | - | - | - | 14 kB | n/a | subprocess wrapper | needs host ffmpeg |
| torchaudio/torchcodec | no | 2.9 / 0.12 | host shared libs | painful | yes | yes | + torch | n/a | API | needs host ffmpeg |
| soundfile | yes (libsndfile) | 0.14.0 (Jun 2026) | n/a | yes / yes | yes / yes | yes | ~1 MB | LGPL | API | excellent; no AAC/M4A |
| miniaudio | yes (C in ext) | 1.71 | n/a | yes / no | yes / yes | yes | small | MIT | API | excellent; no AAC/M4A |
| audioread / pydub | no | 3.1.0 / 0.25.1 (2021) | host CLI | - | - | - | tiny | n/a | subprocess | needs host ffmpeg |

## Recommendation
- **Primary: PyAV (`av>=19`)** - one `pip install`, truly offline-bundled on every target (incl. win_arm64, aarch64, Alpine/musl), in-process decode with built-in resampler (`AudioResampler(format="s16", layout="mono", rate=16000)`), probing from stream metadata, channel extraction via the `pan` filter, and transcoding to MP3/Opus/WAV for the browser. GPL build is acceptable for a hosted service; revisit if you ship a proprietary desktop installer.
- **Fallback: imageio-ffmpeg** - static `ffmpeg` CLI inside the wheel, subprocess-based, zero native-extension risk, offline Docker. Loses `ffprobe` (parse `ffmpeg -i` stderr or decode-and-count). If a real `ffprobe` JSON API matters more than offline wheels, swap in `static-ffmpeg` and pre-warm the download in the Dockerfile.
- Do not rely on `soundfile`/`miniaudio` alone: no AAC/M4A/MP4. Skip `pydub`, `audioread`, `python-ffmpeg`, `torchcodec` for this requirement (they use host ffmpeg).

## Code sketch (decode + probe helper)

```python
# audio_io.py - PyAV primary, imageio-ffmpeg subprocess fallback
from __future__ import annotations
import subprocess, numpy as np

TARGET_RATE = 16000

def probe(path: str) -> dict:
    import av
    with av.open(path) as c:
        s = c.streams.audio[0]
        dur = float(s.duration * s.time_base) if s.duration else (c.duration / av.time_base if c.duration else None)
        return {"duration": dur, "sample_rate": s.rate, "channels": s.layout.nb_channels,  # verify attr on your PyAV
                "codec": s.codec_context.name, "container": c.format.name}

def decode_pcm16k(path: str, channel: int | None = None) -> np.ndarray:
    """Return int16 mono @16 kHz. channel=None -> downmix all channels; channel=i -> keep only channel i."""
    import av
    with av.open(path) as c:
        s = c.streams.audio[0]
        s.thread_type = "AUTO"
        if channel is None:  # simple path: swresample downmix + resample
            rs = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
            out = [f.to_ndarray() for fr in c.decode(s) for f in rs.resample(fr)]
            out += [f.to_ndarray() for f in rs.resample(None)]
        else:                # single-channel path: libavfilter pan -> aformat
            g = av.filter.Graph()
            src = g.add_abuffer(template=s)
            pan = g.add("pan", f"mono|c0=c{channel}")
            fmt = g.add("aformat", f"sample_fmts=s16:sample_rates={TARGET_RATE}:channel_layouts=mono")
            sink = g.add("abuffersink")
            src.link_to(pan); pan.link_to(fmt); fmt.link_to(sink); g.configure()
            out = []
            def drain():
                while True:
                    try: out.append(g.pull().to_ndarray())
                    except (av.BlockingIOError, av.EOFError): return
            for fr in c.decode(s):
                g.push(fr); drain()
            g.push(None); drain()
    return np.concatenate(out, axis=1)[0] if out else np.zeros(0, np.int16)

def decode_pcm16k_cli(path: str, channel: int | None = None) -> np.ndarray:
    """Fallback: bundled ffmpeg CLI from imageio-ffmpeg; no ffprobe, so duration = len/16000."""
    import imageio_ffmpeg
    af = [f"pan=mono|c0=c{channel}"] if channel is not None else []
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-i", path,
           *(["-af", *af] if af else []), "-ar", str(TARGET_RATE), "-ac", "1", "-f", "s16le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16)

def transcode_for_browser(path: str, out_path: str, fmt: str = "mp3") -> None:
    """Pre-transcode once to a browser-safe file; serve with FastAPI FileResponse so HTTP Range seeking works."""
    import av
    with av.open(path) as src, av.open(out_path, "w", format=fmt) as dst:
        ostream = dst.add_stream("libmp3lame" if fmt == "mp3" else "libopus", rate=44100 if fmt == "mp3" else 48000)
        for frame in src.decode(audio=0):
            for pkt in ostream.encode(frame): dst.mux(pkt)
        for pkt in ostream.encode(None): dst.mux(pkt)
```
Notes: `-ac 1` without `-af` lets ffmpeg downmix; with `pan` the layout is already mono. Serve the player from a pre-transcoded MP3/Opus file; streaming a live transcode via `StreamingResponse` works but breaks seeking (no Range support). Wrap PyAV calls in `run_in_threadpool` from FastAPI since they are blocking CPU work.

## Contradictions
- A search-engine summary stated "no 0.7.0 release" of imageio-ffmpeg; the PyPI page directly shows 0.7.0 uploaded Oct 28, 2026. PyPI wins.
- Search summaries claimed PyAV wheels are "configured under LGPL terms"; the pyav-ffmpeg build scripts show `--enable-libx264 --enable-libx265`, which implies a GPL build. Build scripts win.

## Missing evidence
- Exact FFmpeg version and license flags of the binaries in imageio-ffmpeg 0.7.0 and ffmpeg-binaries 1.1.0 (run `ffmpeg -version` / `-buildconf` after install).
- miniaudio and torchcodec PyPI pages were not fetched directly; version numbers come from search summaries.
- PyAV attribute names (`layout.nb_channels` vs `channels`) and filter-graph API should be checked against the installed 19.x; `source_check` for the GPL claim returned no passages.

## Sources
- Kept: PyPI av (https://pypi.org/project/av/) - wheel list, sizes, Python/FFmpeg requirements
- Kept: pyav-ffmpeg README + scripts (https://github.com/PyAV-Org/pyav-ffmpeg) - FFmpeg 9.0.2, codec list, configure flags, `--disable-programs`
- Kept: PyPI imageio-ffmpeg (https://pypi.org/project/imageio-ffmpeg/), GitHub README - wheels, subprocess model, "use PyAV" note
- Kept: PyPI static-ffmpeg (https://pypi.org/project/static-ffmpeg/) - download-on-first-use, FFmpeg 8.0, platforms
- Kept: PyPI ffmpeg-binaries (https://pypi.org/project/ffmpeg-binaries/) - wheel sizes/platforms, API
- Kept: PyPI ffmpeg-downloader (https://pypi.org/project/ffmpeg-downloader/) - gpl/lgpl selectable builds
- Kept: PyPI python-ffmpeg (https://pypi.org/project/python-ffmpeg/) - wrapper only, 2024
- Kept: PyPI soundfile (https://pypi.org/project/soundfile/) - libsndfile 1.2.2, win_arm64, format scope
- Kept: PyPI audioread (https://pypi.org/project/audioread/) - backend dispatcher, 2025
- Deprioritized: torchcodec/torchaudio, miniaudio, pydub pages - not fetched; used only via search summaries, marked medium confidence
- Rejected: Qiita/note/CSDN blog posts - secondary, non-authoritative

## Next steps
- Install `av` in the project venv and run `python -c "import av; print(av.ffmpeg_version, av.library_versions)"` plus a decode test on one file per format (wav/mp3/m4a/aac/flac/ogg/mp4).
- Decide the licensing posture (hosted-only vs redistributed) before locking in GPL-built wheels.
