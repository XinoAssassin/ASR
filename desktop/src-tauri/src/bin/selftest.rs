//! Headless M9 acceptance test (Windows): records system audio (+ mic) with the real recorder
//! while this process plays a chirp marker track through the default render device via WASAPI,
//! with an idle gap (render stream released) in the middle. Then it locates every marker in the
//! FLAC by cross-correlation and reports channel-0 offsets / drift, and optionally uploads the
//! file to a backend and creates a job.
//!
//! ```text
//! recorder-selftest [--minutes 5] [--gap-at 150] [--gap 20] [--out DIR] [--no-mic]
//!                   [--backend http://127.0.0.1:8090 --target-dir DIR --asr-model seaco-paraformer]
//! ```

#[cfg(not(windows))]
fn main() {
    eprintln!("recorder-selftest needs Windows (WASAPI)");
    std::process::exit(2);
}

#[cfg(windows)]
fn main() {
    if let Err(e) = win::main() {
        eprintln!("SELFTEST FAILED: {e}");
        std::process::exit(1);
    }
}

#[cfg(windows)]
mod win {
    use std::path::{Path, PathBuf};
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use asr_recorder::config::ConfigStore;
    use asr_recorder::ipc::{BackendConfig, Provider, StartOptions, UploadState};
    use asr_recorder::recorder::{capture, Recorder};
    use asr_recorder::uploader::{queue::Store, Uploader};
    use wasapi::{DeviceEnumerator, Direction, SampleType, StreamMode};

    type Res<T> = Result<T, String>;

    const MARKER_EVERY_S: f64 = 2.0;
    const CHIRP_S: f64 = 0.1;
    const AMP: f32 = 0.08;
    const TONE_HZ: f64 = 1000.0;

    struct StderrLog;
    impl log::Log for StderrLog {
        fn enabled(&self, m: &log::Metadata) -> bool {
            m.level() <= log::Level::Info
        }
        fn log(&self, r: &log::Record) {
            if self.enabled(r.metadata()) {
                eprintln!("[{}] {}", r.level(), r.args());
            }
        }
        fn flush(&self) {}
    }

    struct Args {
        minutes: f64,
        gap_at: f64,
        gap: f64,
        out: PathBuf,
        mic: bool,
        backend: Option<String>,
        target_dir: String,
        asr_model: Option<String>,
        wait_job: u64,
        mic_device: Option<String>,
        /// Child mode: "chirp" or "tone" (render only, no recording).
        play: Option<String>,
        seconds: f64,
        process_test: bool,
        probe: bool,
    }

    fn parse() -> Res<Args> {
        let mut a = Args {
            minutes: 5.0,
            gap_at: -1.0,
            gap: 20.0,
            out: PathBuf::from(r"D:\tmp\asr-desktop\rec"),
            mic: true,
            backend: None,
            target_dir: "selftest".into(),
            asr_model: None,
            wait_job: 0,
            mic_device: None,
            play: None,
            seconds: 60.0,
            process_test: false,
            probe: false,
        };
        let mut it = std::env::args().skip(1);
        while let Some(k) = it.next() {
            let mut v = || it.next().ok_or(format!("{k} needs a value"));
            match k.as_str() {
                "--minutes" => a.minutes = v()?.parse().map_err(|e| format!("{e}"))?,
                "--gap-at" => a.gap_at = v()?.parse().map_err(|e| format!("{e}"))?,
                "--gap" => a.gap = v()?.parse().map_err(|e| format!("{e}"))?,
                "--out" => a.out = PathBuf::from(v()?),
                "--no-mic" => a.mic = false,
                "--backend" => a.backend = Some(v()?),
                "--target-dir" => a.target_dir = v()?,
                "--asr-model" => a.asr_model = Some(v()?),
                "--wait-job" => a.wait_job = v()?.parse().map_err(|e| format!("{e}"))?,
                // A render device id here records its loopback as the "mic" track (machines
                // without a microphone, e.g. over RDP).
                "--mic-device" => a.mic_device = Some(v()?),
                "--play" => a.play = Some(v()?),
                "--seconds" => a.seconds = v()?.parse().map_err(|e| format!("{e}"))?,
                "--process-test" => a.process_test = true,
                "--probe-process" => a.probe = true,
                "--list-processes" => {
                    capture::init_thread();
                    let p = capture::processes()?;
                    println!("{}", serde_json::to_string_pretty(&p).unwrap());
                    std::process::exit(0);
                }
                "--list-devices" => {
                    capture::init_thread();
                    let d = capture::devices()?;
                    println!("{}", serde_json::to_string_pretty(&d).unwrap());
                    std::process::exit(0);
                }
                other => return Err(format!("unknown argument {other}")),
            }
        }
        if a.gap_at < 0.0 {
            a.gap_at = if a.play.is_some() || a.process_test {
                a.seconds / 2.0
            } else {
                a.minutes * 60.0 / 2.0
            };
        }
        Ok(a)
    }

    fn chirp(rate: f64) -> Vec<f32> {
        let n = (CHIRP_S * rate) as usize;
        let (f0, f1) = (400.0, 5000.0);
        (0..n)
            .map(|i| {
                let t = i as f64 / rate;
                let ph =
                    2.0 * std::f64::consts::PI * (f0 * t + (f1 - f0) * t * t / (2.0 * CHIRP_S));
                let w = 0.5 - 0.5 * (2.0 * std::f64::consts::PI * i as f64 / n as f64).cos();
                (ph.sin() * w) as f32
            })
            .collect()
    }

    /// One played marker: frame index in its part, QPC time it reached the device.
    struct Played {
        part: usize,
        t_qpc: f64,
        source_t: f64,
    }

    /// Play `dur` seconds with a chirp every MARKER_EVERY_S (first at 1 s) on the default render
    /// device, then release the stream. Returns marker play times (QPC) from IAudioClock.
    fn play_part(part: usize, dur: f64, source_offset: f64, tone: bool) -> Res<Vec<Played>> {
        capture::init_thread();
        let e2s = |e: wasapi::WasapiError| e.to_string();
        let en = DeviceEnumerator::new().map_err(e2s)?;
        let dev = en.get_default_device(&Direction::Render).map_err(e2s)?;
        let mut client = dev.get_iaudioclient().map_err(e2s)?;
        let fmt = client.get_mixformat().map_err(e2s)?;
        if fmt.get_subformat().map_err(e2s)? != SampleType::Float || fmt.get_bitspersample() != 32 {
            return Err("render mix format is not float32".into());
        }
        let rate = fmt.get_samplespersec() as f64;
        let ch = fmt.get_nchannels() as usize;
        client
            .initialize_client(
                &fmt,
                &Direction::Render,
                &StreamMode::EventsShared {
                    autoconvert: false,
                    buffer_duration_hns: 2_000_000,
                },
            )
            .map_err(e2s)?;
        let ev = client.set_get_eventhandle().map_err(e2s)?;
        let render = client.get_audiorenderclient().map_err(e2s)?;
        let clock = client.get_audioclock().map_err(e2s)?;
        let freq = clock.get_frequency().map_err(e2s)? as f64;
        let total = (dur * rate) as usize;
        let c = chirp(rate);
        let marker_frames: Vec<usize> = (0..)
            .map(|k| ((1.0 + k as f64 * MARKER_EVERY_S) * rate) as usize)
            .take_while(|&f| f + c.len() < total)
            .collect();
        let sample = |i: usize| -> f32 {
            if tone {
                return (2.0 * std::f64::consts::PI * TONE_HZ * i as f64 / rate).sin() as f32 * AMP;
            }
            // markers are sparse: find the one covering i
            let k = ((i as f64 / rate - 1.0) / MARKER_EVERY_S).floor();
            if k < 0.0 {
                return 0.0;
            }
            let start = ((1.0 + k * MARKER_EVERY_S) * rate) as usize;
            if i >= start && i - start < c.len() && start + c.len() < total {
                c[i - start] * AMP
            } else {
                0.0
            }
        };
        let mut written = 0usize;
        let mut pos_samples: Vec<(f64, f64)> = Vec::new();
        let block = fmt.get_blockalign() as usize;
        let mut buf: Vec<u8> = Vec::new();
        client.start_stream().map_err(e2s)?;
        let mut last_pos_sample = Instant::now() - Duration::from_secs(1);
        loop {
            let space = client.get_available_space_in_frames().map_err(e2s)? as usize;
            let n = space.min(total - written);
            if n > 0 {
                buf.clear();
                for i in written..written + n {
                    let s = sample(i).to_le_bytes();
                    for _ in 0..ch {
                        buf.extend_from_slice(&s);
                    }
                }
                debug_assert_eq!(buf.len(), n * block);
                render.write_to_device(n, &buf, None).map_err(e2s)?;
                written += n;
            }
            if last_pos_sample.elapsed() >= Duration::from_millis(50) {
                let (pos, qpc) = clock.get_position().map_err(e2s)?;
                pos_samples.push((pos as f64 / freq * rate, qpc as f64 * 1e-7));
                last_pos_sample = Instant::now();
            }
            if written >= total && client.get_current_padding().map_err(e2s)? == 0 {
                break;
            }
            let _ = ev.wait_for_event(1000);
        }
        client.stop_stream().map_err(e2s)?;
        drop(render);
        drop(client);
        // Fit frames -> QPC seconds over the steady part (skip the first 0.5 s).
        let pts: Vec<(f64, f64)> = pos_samples
            .into_iter()
            .filter(|(f, _)| *f > 0.5 * rate && *f < total as f64 - 0.1 * rate)
            .collect();
        let (a, b) = linfit(&pts);
        log::info!(
            "part {part}: played {:.1} s at {rate} Hz; device clock vs QPC {:+.1} ppm ({} clock samples)",
            total as f64 / rate,
            (1.0 / (b * rate) - 1.0) * 1e6,
            pts.len()
        );
        if tone {
            return Ok(Vec::new());
        }
        Ok(marker_frames
            .iter()
            .map(|&f| Played {
                part,
                t_qpc: a + b * f as f64,
                source_t: source_offset + f as f64 / rate,
            })
            .collect())
    }

    /// Least squares y = a + b x.
    fn linfit(p: &[(f64, f64)]) -> (f64, f64) {
        let n = p.len() as f64;
        let mx = p.iter().map(|q| q.0).sum::<f64>() / n;
        let my = p.iter().map(|q| q.1).sum::<f64>() / n;
        let sxy: f64 = p.iter().map(|q| (q.0 - mx) * (q.1 - my)).sum();
        let sxx: f64 = p.iter().map(|q| (q.0 - mx) * (q.0 - mx)).sum();
        let b = sxy / sxx;
        (my - b * mx, b)
    }

    fn read_flac(p: &Path) -> Res<(u32, Vec<Vec<f32>>)> {
        let mut r = claxon::FlacReader::open(p).map_err(|e| e.to_string())?;
        let ch = r.streaminfo().channels as usize;
        let rate = r.streaminfo().sample_rate;
        let mut out = vec![Vec::new(); ch];
        for (i, s) in r.samples().enumerate() {
            out[i % ch].push(s.map_err(|e| e.to_string())? as f32 / 32768.0);
        }
        Ok((rate, out))
    }

    /// Best match of `tpl` in `x` for lags in [lo, hi): (lag with sub-sample refinement, normalised score).
    fn xcorr(x: &[f32], tpl: &[f32], lo: usize, hi: usize) -> Option<(f64, f64)> {
        let hi = hi.min(x.len().saturating_sub(tpl.len()));
        if lo >= hi {
            return None;
        }
        let te: f64 = tpl.iter().map(|v| (*v as f64).powi(2)).sum::<f64>().sqrt();
        let score = |l: usize| -> f64 {
            let w = &x[l..l + tpl.len()];
            let dot: f64 = w.iter().zip(tpl).map(|(a, b)| *a as f64 * *b as f64).sum();
            let we: f64 = w.iter().map(|v| (*v as f64).powi(2)).sum::<f64>().sqrt();
            if we == 0.0 {
                0.0
            } else {
                dot / (we * te)
            }
        };
        let (mut best, mut bs) = (lo, f64::MIN);
        for l in lo..hi {
            let s = score(l);
            if s > bs {
                bs = s;
                best = l;
            }
        }
        let mut lag = best as f64;
        if best > lo && best + 1 < hi {
            let (y0, y1, y2) = (score(best - 1), bs, score(best + 1));
            let d = y0 - 2.0 * y1 + y2;
            if d.abs() > 1e-12 {
                lag += 0.5 * (y0 - y2) / d;
            }
        }
        Some((lag, bs))
    }

    fn rms(x: &[f32]) -> f64 {
        (x.iter().map(|v| (*v as f64).powi(2)).sum::<f64>() / x.len().max(1) as f64).sqrt()
    }

    pub fn main() -> Res<()> {
        let _ = log::set_logger(&StderrLog).map(|()| log::set_max_level(log::LevelFilter::Info));
        let a = parse()?;
        if let Some(kind) = a.play.clone() {
            return player(&kind, &a);
        }
        std::fs::create_dir_all(&a.out).map_err(|e| e.to_string())?;
        if a.probe {
            return probe_process(&a);
        }
        if a.process_test {
            return process_test(&a);
        }
        let total = a.minutes * 60.0;
        let part_a = a.gap_at;
        let part_b = total - a.gap_at - a.gap;
        if part_b < 10.0 {
            return Err("recording too short for the gap".into());
        }
        log::info!(
            "plan: {part_a:.0} s playback, {:.0} s idle (stream released), {part_b:.0} s playback",
            a.gap
        );
        let gap = a.gap;
        let (fin, played, drift_log) = record(&a, None, a.mic, move || {
            let mut played = play_part(0, part_a, 0.0, false)?;
            log::info!("idle gap: render stream released for {gap:.0} s");
            std::thread::sleep(Duration::from_secs_f64(gap));
            played.extend(play_part(1, part_b, part_a + gap, false)?);
            Ok(played)
        })?;
        analyze(&fin, &played, &drift_log, a.gap)?;
        // ---- upload
        if let Some(url) = a.backend {
            let data = a.out.join(format!("upload-state-{}", std::process::id()));
            upload(
                &url,
                &a.target_dir,
                a.asr_model.as_deref(),
                a.wait_job,
                &fin,
                &data,
            )?;
        }
        Ok(())
    }

    fn upload(
        url: &str,
        target_dir: &str,
        model: Option<&str>,
        wait_job: u64,
        fin: &asr_recorder::recorder::Finished,
        dir: &Path,
    ) -> Res<()> {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        let cfg = Arc::new(ConfigStore::open(dir));
        let mut opts = serde_json::json!({});
        if let Some(m) = model {
            opts["local"] = serde_json::json!({ "asr_model": m });
        }
        cfg.set(BackendConfig {
            base_url: url.into(),
            api_key: None,
            target_dir: target_dir.into(),
            auto_upload: true,
            job_options: Some(opts),
            provider: Some(Provider::Local),
            term_set_ids: vec![],
            extra_terms: vec![],
        })?;
        let store = Arc::new(Store::open(&dir.join("recordings.json")));
        let up = Uploader::new(
            store,
            cfg,
            Arc::new(|r| {
                log::info!(
                    "recordings://changed {} -> {:?} {:?}",
                    r.id,
                    r.upload.state,
                    r.upload.error
                )
            }),
        );
        let rec = up.add_finished(fin, true);
        let deadline = Instant::now() + Duration::from_secs(wait_job.max(60));
        loop {
            up.step(&rec.id);
            let e = up.store().get(&rec.id).unwrap();
            let u = &e.recording.upload;
            match u.state {
                UploadState::Transcribing if wait_job == 0 => break,
                UploadState::Done => break,
                UploadState::Failed if u.next_retry_at.is_none() => {
                    return Err(format!("upload failed: {:?}", u.error));
                }
                _ => {}
            }
            if Instant::now() > deadline {
                break;
            }
            std::thread::sleep(Duration::from_secs(3));
        }
        let e = up.store().get(&rec.id).unwrap();
        println!("== upload ==");
        println!(
            "state {:?} uploadId {:?} jobId {:?} transcriptId {:?} attempts {}",
            e.recording.upload.state,
            e.recording.upload.upload_id,
            e.recording.upload.job_id,
            e.recording.upload.transcript_id,
            e.recording.upload.attempts
        );
        if let Some(job) = &e.recording.upload.job_id {
            let v: serde_json::Value = reqwest::blocking::Client::new()
                .get(format!("{url}/api/jobs/{job}"))
                .send()
                .and_then(|r| r.json())
                .map_err(|e| e.to_string())?;
            println!(
                "job {job}: status {} provider {} preprocess.channels {} asr_model {}",
                v["status"],
                v["provider"],
                v["options"]["preprocess"]["channels"],
                v["options"]["local"]["asr_model"]
            );
        }
        Ok(())
    }

    type Recorded = (
        asr_recorder::recorder::Finished,
        Vec<Played>,
        Vec<(f64, f64)>,
    );

    /// Record (system [+ mic]) while `play` runs; returns the finished file and marker times.
    fn record(
        a: &Args,
        process_id: Option<u32>,
        mic: bool,
        play: impl FnOnce() -> Res<Vec<Played>>,
    ) -> Res<Recorded> {
        let rec = Arc::new(Recorder::new());
        let st = rec.start(
            &StartOptions {
                system: true,
                mic,
                output_device: None,
                input_device: a.mic_device.clone(),
                out_dir: None,
                process_id,
            },
            &a.out,
        )?;
        log::info!(
            "recording to {:?} sources {:?} processId {:?} warnings {:?}",
            st.file,
            st.sources,
            st.process_id,
            st.warnings
        );
        let monitor = {
            let rec = rec.clone();
            std::thread::spawn(move || {
                let mut last = Instant::now();
                let mut drift: Vec<(f64, f64)> = Vec::new();
                while rec.state() == asr_recorder::ipc::RecorderState::Recording {
                    std::thread::sleep(Duration::from_millis(250));
                    let s = rec.status(true);
                    if let Some(d) = s.drift_ms {
                        drift.push((s.elapsed, d));
                    }
                    if last.elapsed() >= Duration::from_secs(30) {
                        last = Instant::now();
                        log::info!(
                            "t={:.0}s levels sys {:.3} mic {:.3} driftMs {:?} warnings {:?}",
                            s.elapsed,
                            s.levels.system,
                            s.levels.mic,
                            s.drift_ms,
                            s.warnings
                        );
                    }
                }
                drift
            })
        };
        std::thread::sleep(Duration::from_millis(1500));
        let t_play = Instant::now();
        let played = play();
        log::info!(
            "playback done after {:.1} s",
            t_play.elapsed().as_secs_f64()
        );
        std::thread::sleep(Duration::from_millis(1500));
        let fin = rec.stop()?;
        let drift_log = monitor.join().unwrap_or_default();
        log::info!(
            "finished {} ({} ch, {:.2} s, {} bytes)",
            fin.path.display(),
            fin.channels,
            fin.duration,
            fin.size
        );
        Ok((fin, played?, drift_log))
    }

    /// Locate the markers and print offsets / drift. Returns marker positions (s) in the file.
    fn analyze(
        fin: &asr_recorder::recorder::Finished,
        played: &[Played],
        drift_log: &[(f64, f64)],
        gap: f64,
    ) -> Res<Vec<f64>> {
        let (rate, chans) = read_flac(&fin.path)?;
        let rate_f = rate as f64;
        let tpl = chirp(rate_f);
        let ch0 = &chans[0];
        // anchor: first marker anywhere in the first 6 s
        let (l0, s0) = xcorr(ch0, &tpl, 0, (6.0 * rate_f) as usize).ok_or("no audio")?;
        if s0 < 0.5 {
            return Err(format!(
                "first marker not found in channel 0 (score {s0:.2}); is the default output device captured?"
            ));
        }
        let q0 = played[0].t_qpc;
        let mut rows = Vec::new();
        let mut missing = 0;
        for p in played {
            let expect = l0 + (p.t_qpc - q0) * rate_f;
            let lo = (expect - 0.3 * rate_f).max(0.0) as usize;
            let hi = (expect + 0.3 * rate_f) as usize;
            match xcorr(ch0, &tpl, lo, hi) {
                Some((lag, s)) if s > 0.5 => {
                    let offset_ms = (lag - expect) / rate_f * 1000.0;
                    rows.push((p.part, p.source_t, lag / rate_f, offset_ms, s));
                }
                _ => missing += 1,
            }
        }
        if rows.len() < 4 {
            return Err(format!("only {} markers found", rows.len()));
        }
        let off = |part: usize, first: bool| {
            let v: Vec<_> = rows.iter().filter(|r| r.0 == part).collect();
            let r = if first { v.first() } else { v.last() };
            r.map(|r| (r.2, r.3))
        };
        println!("== channel 0 (system loopback) vs. QPC play time of each marker ==");
        println!(
            "markers found {} / {} (missing {missing})",
            rows.len(),
            played.len()
        );
        for r in rows.iter().step_by((rows.len() / 12).max(1)) {
            println!(
                "  part {} src {:7.2}s  rec {:8.3}s  offset {:+7.2} ms  score {:.2}",
                r.0, r.1, r.2, r.3, r.4
            );
        }
        let (a0, a1) = (off(0, true), off(0, false));
        let (b0, b1) = (off(1, true), off(1, false));
        if let (Some(a0), Some(a1)) = (a0, a1) {
            println!(
                "part A: offset start {:+.2} ms (t={:.1}s) end {:+.2} ms (t={:.1}s) -> drift {:+.2} ms",
                a0.1, a0.0, a1.1, a1.0, a1.1 - a0.1
            );
        }
        if let (Some(b0), Some(b1)) = (b0, b1) {
            println!(
                "part B: offset start {:+.2} ms (t={:.1}s) end {:+.2} ms (t={:.1}s) -> drift {:+.2} ms",
                b0.1, b0.0, b1.1, b1.0, b1.1 - b0.1
            );
        }
        if let (Some(a1), Some(b0)) = (a1, b0) {
            println!(
                "across the {:.0} s idle gap: offset change {:+.2} ms (silence fill timing error)",
                gap,
                b0.1 - a1.1
            );
        }
        let first = rows.first().unwrap();
        let last = rows.last().unwrap();
        println!(
            "OVERALL: offset at start {:+.2} ms, at end {:+.2} ms (t={:.1}s) -> drift {:+.2} ms over {:.1} min",
            first.3,
            last.3,
            last.2,
            last.3 - first.3,
            (last.2 - first.2) / 60.0
        );
        if chans.len() > 1 {
            let ch1 = &chans[1];
            println!("== channel 1 (mic) ==");
            println!(
                "mic rms {:.5} (ch0 rms {:.5}), samples {} = ch0 {}",
                rms(ch1),
                rms(ch0),
                ch1.len(),
                ch0.len()
            );
            // Acoustic pickup of the markers (only if speakers + mic are live).
            let mut mic_rows = Vec::new();
            for r in &rows {
                let c = (r.2 * rate_f) as usize;
                let lo = c.saturating_sub((0.05 * rate_f) as usize);
                let hi = c + (0.2 * rate_f) as usize;
                if let Some((lag, s)) = xcorr(ch1, &tpl, lo, hi) {
                    if s > 0.4 {
                        mic_rows.push((r.2, (lag / rate_f - r.2) * 1000.0));
                    }
                }
            }
            if mic_rows.len() >= 4 {
                let (f, l) = (mic_rows.first().unwrap(), mic_rows.last().unwrap());
                println!(
                    "channel 1 carries {} markers (acoustic pickup, or loopback via --mic-device): ch1-ch0 offset start {:+.2} ms, end {:+.2} ms (t={:.1}s) -> drift {:+.2} ms",
                    mic_rows.len(), f.1, l.1, l.0, l.1 - f.1
                );
            } else {
                println!("channel 1 does not carry the markers ({} found); inter-channel sync not measurable", mic_rows.len());
            }
            if let (Some(f), Some(l)) = (drift_log.first(), drift_log.last()) {
                let max = drift_log.iter().map(|d| d.1.abs()).fold(0.0, f64::max);
                println!(
                    "controller residual driftMs: first {:+.2} (t={:.0}s) last {:+.2} (t={:.0}s) max |{max:.2}|",
                    f.1, f.0, l.1, l.0
                );
            }
        }
        Ok(rows.iter().map(|r| r.2).collect())
    }

    /// Child mode: render a chirp track (waits for "GO" on stdin, prints marker QPC times) or a
    /// continuous 1 kHz tone.
    fn player(kind: &str, a: &Args) -> Res<()> {
        use std::io::{BufRead, Write};
        match kind {
            "tone" => {
                println!("READY {}", std::process::id());
                std::io::stdout().flush().ok();
                play_part(0, a.seconds, 0.0, true)?;
                Ok(())
            }
            "chirp" => {
                println!("READY {}", std::process::id());
                std::io::stdout().flush().ok();
                let mut line = String::new();
                std::io::stdin().lock().read_line(&mut line).ok();
                let part_a = a.gap_at.min(a.seconds);
                let mut played = play_part(0, part_a, 0.0, false)?;
                if a.seconds - part_a - a.gap >= 5.0 {
                    std::thread::sleep(Duration::from_secs_f64(a.gap));
                    played.extend(play_part(
                        1,
                        a.seconds - part_a - a.gap,
                        part_a + a.gap,
                        false,
                    )?);
                }
                for p in &played {
                    println!("M {} {:.7} {:.4}", p.part, p.t_qpc, p.source_t);
                }
                std::io::stdout().flush().ok();
                Ok(())
            }
            other => Err(format!("unknown --play {other}")),
        }
    }

    struct Child {
        child: std::process::Child,
        pid: u32,
        out: std::io::BufReader<std::process::ChildStdout>,
    }

    fn spawn_player(kind: &str, a: &Args, seconds: f64) -> Res<Child> {
        use std::io::BufRead;
        use std::process::{Command, Stdio};
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let mut child = Command::new(exe)
            .args(["--play", kind, "--seconds", &seconds.to_string()])
            .args([
                "--gap-at",
                &a.gap_at.to_string(),
                "--gap",
                &a.gap.to_string(),
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .map_err(|e| e.to_string())?;
        let mut out = std::io::BufReader::new(child.stdout.take().unwrap());
        let mut line = String::new();
        out.read_line(&mut line).map_err(|e| e.to_string())?;
        let pid = line
            .strip_prefix("READY ")
            .and_then(|p| p.trim().parse().ok())
            .ok_or(format!("player {kind} did not start: {line:?}"))?;
        Ok(Child { child, pid, out })
    }

    /// RMS (dBFS) and 1 kHz Goertzel level of channel 0 outside the chirp markers.
    fn tone_level(fin: &asr_recorder::recorder::Finished, markers: &[f64]) -> Res<(f64, f64)> {
        let (rate, chans) = read_flac(&fin.path)?;
        let r = rate as f64;
        let ch0 = &chans[0];
        // middle of the recording, away from start/stop edges
        let lo = (3.0 * r) as usize;
        let hi = ch0.len().saturating_sub((3.0 * r) as usize);
        let mut kept: Vec<f32> = Vec::new();
        for (i, v) in ch0.iter().enumerate().take(hi).skip(lo) {
            let t = i as f64 / r;
            if markers.iter().any(|m| t > m - 0.1 && t < m + 0.35) {
                continue;
            }
            kept.push(*v);
        }
        let rms_db = 20.0 * rms(&kept).max(1e-10).log10();
        // Goertzel at 1 kHz over the kept samples (normalised to a sine amplitude).
        let w = 2.0 * std::f64::consts::PI * TONE_HZ / r;
        let (mut s1, mut s2) = (0.0f64, 0.0f64);
        for v in &kept {
            let s0 = *v as f64 + 2.0 * w.cos() * s1 - s2;
            s2 = s1;
            s1 = s0;
        }
        let power = s1 * s1 + s2 * s2 - 2.0 * w.cos() * s1 * s2;
        let amp = 2.0 * power.max(0.0).sqrt() / kept.len().max(1) as f64;
        Ok((rms_db, 20.0 * amp.max(1e-10).log10()))
    }

    /// Two players at once (chirps / 1 kHz tone, separate processes). Run 1 records only the
    /// chirp player's process tree, run 2 the whole device.
    fn process_test(a: &Args) -> Res<()> {
        use std::io::{BufRead, Write};
        let mut results = Vec::new();
        for (label, by_process) in [
            ("process loopback (chirp player only)", true),
            ("whole device", false),
        ] {
            println!("== {label} ==");
            let mut tone = spawn_player("tone", a, a.seconds + 8.0)?;
            let mut chirp = spawn_player("chirp", a, a.seconds)?;
            log::info!("players: chirp pid {} tone pid {}", chirp.pid, tone.pid);
            let pid = by_process.then_some(chirp.pid);
            let stdin = chirp.child.stdin.take();
            let (fin, played, drift) = record(a, pid, false, move || {
                if let Some(mut i) = stdin {
                    writeln!(i, "GO").map_err(|e| e.to_string())?;
                }
                let mut played = Vec::new();
                let mut line = String::new();
                while chirp.out.read_line(&mut line).map_err(|e| e.to_string())? > 0 {
                    let f: Vec<&str> = line.split_whitespace().collect();
                    if f.len() == 4 && f[0] == "M" {
                        played.push(Played {
                            part: f[1].parse().unwrap_or(0),
                            t_qpc: f[2].parse().unwrap_or(0.0),
                            source_t: f[3].parse().unwrap_or(0.0),
                        });
                    }
                    line.clear();
                }
                let _ = chirp.child.wait();
                Ok(played)
            })?;
            let _ = tone.child.kill();
            let _ = tone.child.wait();
            let markers = analyze(&fin, &played, &drift, a.gap)?;
            let (rms_db, tone_db) = tone_level(&fin, &markers)?;
            println!(
                "markers found {}/{}; channel 0 outside markers: RMS {rms_db:.1} dBFS, 1 kHz tone {tone_db:.1} dBFS (played at {:.1} dBFS)",
                markers.len(),
                played.len(),
                20.0 * (AMP as f64).log10()
            );
            results.push((label, markers.len(), played.len(), rms_db, tone_db));
        }
        println!("== summary ==");
        for r in &results {
            println!(
                "{}: chirps {}/{}, other process (1 kHz tone) {:.1} dBFS, residual RMS {:.1} dBFS",
                r.0, r.1, r.2, r.4, r.3
            );
        }
        Ok(())
    }

    /// Diagnostic: packet timing of process loopback while a chirp player runs (with its gap).
    fn probe_process(a: &Args) -> Res<()> {
        use std::io::Write;
        let mut chirp = spawn_player("chirp", a, a.seconds)?;
        capture::init_thread();
        let mut src = capture::open(capture::SourceKind::System, None, Some(chirp.pid))?;
        let rate = src.rate();
        if let Some(mut i) = chirp.child.stdin.take() {
            writeln!(i, "GO").map_err(|e| e.to_string())?;
        }
        let t0 = capture::now();
        let mut frames = 0u64;
        let mut first_ts: Option<f64> = None;
        let mut prev_end: Option<f64> = None;
        let (mut maxjump, mut packets, mut zero_ts) = (0.0f64, 0u64, 0u64);
        let mut last_print = t0;
        let mut pk = Vec::new();
        while capture::now() - t0 < a.seconds + 2.0 {
            pk.clear();
            src.poll(&mut pk)?;
            let now = capture::now();
            for p in &pk {
                packets += 1;
                if p.t <= 0.0 {
                    zero_ts += 1;
                }
                if let Some(e) = prev_end {
                    maxjump = maxjump.max((p.t - e).abs());
                }
                prev_end = Some(p.t + p.samples.len() as f64 / rate);
                first_ts.get_or_insert(p.t);
                frames += p.samples.len() as u64;
                let lag = now - p.t;
                if now - last_print >= 2.0 {
                    last_print = now;
                    let el = now - t0;
                    println!(
                        "t={el:6.2}s frames/rate={:7.3}s ts-span={:7.3}s lag(now-ts)={:6.1}ms max ts discontinuity {:.2}ms packets {packets} zero-ts {zero_ts}",
                        frames as f64 / rate,
                        p.t - first_ts.unwrap(),
                        lag * 1000.0,
                        maxjump * 1000.0
                    );
                    maxjump = 0.0;
                }
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ = chirp.child.wait();
        Ok(())
    }
}
