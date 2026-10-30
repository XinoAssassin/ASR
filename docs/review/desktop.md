# Desktop recorder (Rust) review — `desktop/src-tauri` @ main db52182

Read-only. Scope: `recorder/**`, `uploader/**`, `sidecar.rs`, `app/mod.rs`, `ipc.rs`, `config.rs`, `util.rs`, `bin/selftest.rs` (skimmed), `Cargo.toml`, `tauri.conf.json`, `capabilities/default.json`, `build.rs`, against AGENTS rule 8, `docs/desktop-ipc.md`, ARCH_REVIEW §4 g, EXECUTION_LOG D49/D58 and §13–14. `cargo fmt` / `clippy -D warnings` / `cargo test` could not be executed in this review (no shell); EXECUTION_LOG §15 reports them clean — a supervisor should re-run `cd desktop/src-tauri && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test --no-default-features`.

## Summary

| # | Severity | Area | Location | Finding |
|---|---|---|---|---|
| 1 | Major | Sidecar / env | `sidecar.rs:185-200` | App-started backend inherits the user env only; default `ASR_CORS_ORIGINS` lacks `http://tauri.localhost`, so the SPA's own calls to the sidecar fail (CORS + Origin check) |
| 2 | Major | Rule 8 / thread death | `recorder/mod.rs:170-175`, `app/mod.rs:424` | A recorder thread that panics or exits without setting `fatal` is never detected; UI stays `recording` while no audio is written |
| 3 | Minor | Rule 8 / uploads | `app/mod.rs:388-412` | Uploader worker starts only after recovery finishes; a panic in recovery silently disables all uploads for the session |
| 4 | Minor | Finalize / duplicates | `recorder/mod.rs:446-483` | Crash between `rename(tmp, flac)` and `remove_file(partial)` → partial becomes `<stem>.wav`, both files get queued (two jobs) |
| 5 | Minor | Device follow | `engine.rs:386-396, 415-418` | After a default-device switch, a failing reopen never sets `lost_at` → silence-filled track with no "disconnected" warning |
| 6 | Minor | Process loopback | `engine.rs:274-294` | Process-loopback open failure (pre-2004 Windows, denied) does not fall back to whole-device loopback; system track silently absent |
| 7 | Minor | Uploader | `uploader/mod.rs:245-252` | Missing file is a permanent failure even when `upload_id` exists and only `POST /api/jobs` remains |
| 8 | Minor | Linux capture | `pulse_capture.rs:29` + `reader()` | 5 s socket read timeout vs. a suspended idle sink monitor → reopen loop every ~7 s |
| 9 | Minor | Sidecar config | `sidecar.rs:165-185`, `ipc.rs` `SidecarConfig` | `port` and `args --port` are independent; mismatch → 120 s health timeout then kill; SPA can set an arbitrary `command` |
| 10 | Minor | Contract | `engine.rs:533-537` | "device disconnected" warning is Chinese-only; contract says warnings are bilingual |
| 11 | Minor | Tests (D58) | `engine.rs` tests, `testenv.rs` | No test for default-device switch / `reset_rate` after reopen; app-level auto-stop on `fatal` untested; noted flaky timing test |
| 12 | Nit | Error handling | `app/mod.rs`, `config.rs`, `uploader/*`, `sidecar.rs` | `lock().unwrap()` (poison panics) and `expect` on runtime paths (`uploader/mod.rs:109`, `sidecar.rs:266`, `recorder/mod.rs:255`) |
| 13 | Nit | Shell hardening | `app/mod.rs:262` | `explorer.exe` resolved by name instead of `%WINDIR%\explorer.exe` |
| 14 | Nit | Capabilities | `capabilities/default.json` | `core:default` grants tray/menu/window perms the SPA does not use |
| 15 | Nit | Uploader | `client.rs:142, 216` | Fixed 300 s per-request timeout; an 8 MiB part on a very slow uplink can never complete |
| 16 | Nit | WAV | `wav.rs` `WavReader::open` | Does not check the `WAVE` tag (repair does) |

No Critical findings.

## What is correct (evidence)

- **Incremental WAV, patching, fsync, recovery.** `WavWriter` buffers until the OS accepts bytes; failed/short writes keep the tail and re-seek (`wav.rs:98-137`); header patched every 1 s, `sync_data` every 10 s (`engine.rs:16-17, 478-487`); `repair` re-derives sizes from file length and truncates torn frames (`wav.rs:198-227`); `Drop` flushes on unwind. Tests: `failed_and_short_writes_lose_and_duplicate_nothing`, `header_is_patched_and_crash_is_repairable`.
- **>4 GiB.** Header saturates at `u32::MAX-36`; `data_len` falls back to file length; `over_4gib_wav_is_read_in_full` (Linux, sparse). FLAC total-samples is 36-bit; encode/verify stream in 4096-frame blocks (`flac.rs`).
- **Disk full / write failure (D58).** Frames are held up to `MAX_BACKLOG_S = 300`, then the session ends itself with `fatal`; state stays `Recording` until `stop`, which finalises what reached disk (`engine.rs:488-505`, `recorder/mod.rs:170-175`, app status loop `app/mod.rs:420-431`). Tests: `transient_write_errors_lose_nothing`, `backlog_overflow_stops_the_session`, `failure_is_reported_and_stop_finalises`.
- **FLAC verify.** Decode-and-compare sample by sample; partial deleted only when verified frames == frames on disk; otherwise WAV is kept as `<stem>.wav` (`recorder/mod.rs:437-490`, test `finalize_keeps_wav_when_flac_is_short`).
- **Recovery race safety (D58).** Stems are reserved under the recorder lock before the file exists; `recover_dir` claims per stem and re-checks existence (`recorder/mod.rs:200-215, 290-335`). Test `recovery_races_with_start_and_stop` with an injected `create_delay`.
- **Timeline / drift.** Reference-anchored `ClockMap` with least-squares rate fit (one bad stamp ignored: `rate_fit_ignores_one_bad_timestamp`), PI follower bounded ±1 %, exact re-placement > 40 ms, silence fill to `map(now) − 250 ms`, gaps > 10 s with no audio cut (`hour_long_stall_is_cut_with_bounded_time_and_memory`), buffered-audio stall not cut (`stall_with_buffered_audio_is_not_cut`), equal channel lengths at stop (`Timeline::finish`). `now` is read before polling so a stall after that is harmless (`engine.rs:380-383`).
- **Resampler.** Kaiser-sinc, absolute positions, exact `landing`; passband, alias and landing tests present.
- **WASAPI.** QPC seconds match `u64QPCPosition` (`wasapi_capture.rs:16-28`); `init_thread` (MTA) on the recorder thread, reopen threads, and command blocking threads (`engine.rs:155, 253`; `app/mod.rs:149, 180`); device errors → silence fill + reopen on a helper thread every 2 s; default-device id polled every 2 s; drop stops the stream; packets read before an error are still delivered. `unsafe impl Send` is justified by MTA + single-thread use.
- **Threading / shutdown.** `stop()` sets the flag, joins, and finalises even on `Err`/panic (`recorder/mod.rs:243-275`); `spawn` has a start timeout with an ABANDONED phase so no orphan file is created (`start_times_out_without_leaving_a_file`); reopen threads hold nothing but a source; `quit` finalises before `app.exit`; `RunEvent::Exit` stops the sidecar.
- **Uploader.** Atomic JSON writes under the store lock (`util.rs:write_atomic_mode`, `queue.rs:save`); per-part resume gated on size+mtime; idempotent `complete`, 409 transient, 404/4xx drops the session; 404 on jobs disambiguated via `GET /api/uploads/{id}` (no re-upload loop; test `missing_term_set_is_a_permanent_job_failure_not_a_reupload`); backoff 10 s → 30 min ±20 %; redirects refused so `X-API-Key` never leaves the configured host (`redirects_are_not_followed`); key stored in its own file, masked, `Debug` masks it, messages redacted; recordings never deleted.
- **Sidecar.** Job object with `KILL_ON_JOB_CLOSE`, own process group, CTRL_BREAK then kill after 10 s, port-in-use check, serialised start/stop, health timeout, log rotation; tests with a Python stand-in.
- **Tauri.** CSP has `script-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`; `style-src 'unsafe-inline'` is justified and documented; no shell/fs plugins; capabilities limited to `core:default`; async commands use `spawn_blocking` for long operations; GUI subsystem in release.

## Findings

### 1. Major — sidecar: SPA cannot talk to the backend the app starts (CORS / Origin)
`sidecar.rs:185-200` builds `Command::new(&cfg.command).args(&cfg.args)` with no `env()`; the child inherits the GUI app's environment. The backend's default `cors_origins` is `["http://localhost:5173", "http://localhost:8080"]` (`backend/server/config.py:62-64`) and `auth.py:14-16` refuses state-changing requests whose `Origin` is not listed. The WebView origin is `http://tauri.localhost` (`docs/desktop-ipc.md` "Backend notes"; `frontend/src/api/client.ts:8` confirms the SPA calls the backend directly). With the default `SidecarConfig` (`asr serve --port 8000`) the Rust uploads work (not browser requests) but the SPA's own reads fail CORS and writes are refused, unless the user has set `ASR_CORS_ORIGINS` system-wide. The README documents the requirement but the sidecar, whose purpose is "zero-config local backend", does not honour it.
**Fix:** in `Sidecar::start`, before spawn: if `std::env::var_os("ASR_CORS_ORIGINS")` is unset, `cmd.env("ASR_CORS_ORIGINS", "http://localhost:5173,http://localhost:8080,http://tauri.localhost")`; if set and missing the origin, append it. Add a unit test asserting the env value via a stand-in that prints `os.environ`.

### 2. Major — a dead recorder thread is invisible (rule 8)
`Recorder::failure()` (`recorder/mod.rs:170-175`) returns only `Live.fatal`, which is set solely on the backlog-overflow path (`engine.rs:493-503`). If the recorder thread panics (e.g. inside a capture backend, `wasapi` crate, or an indexing bug) or returns early, nothing sets `fatal`; the 10 Hz loop (`app/mod.rs:420-431`) keeps emitting `state: recording` with frozen `elapsed`, and no audio is written until the user notices and presses stop (which does finalise correctly, `recorder/mod.rs:258-266`). Audio from the panic onward is silently lost while the UI claims recording.
**Fix:** in `failure()`, also return `Some("recorder thread ended unexpectedly / 录音线程异常退出")` when `s.thread.is_finished()` and `fatal.is_none()`; the existing status-loop `do_stop` then finalises and queues the partial. Add an engine test with a `FakeEnv` source whose `poll` panics.

### 3. Minor — uploader never starts if recovery panics
`app/mod.rs:388-412`: `state.uploader.start()` is the last statement of the recovery thread. `recover_dir → finalize → flac::encode/verify` run third-party decoders on arbitrary leftover files; a panic there kills the thread before `start()`, and no upload happens for the whole app session (recordings stopped later are queued but never processed). Also, a long orphan (minutes of FLAC encode) delays all uploads.
**Fix:** call `state.uploader.start()` before the recovery loop (the worker handles entries added later via `wake()`), or wrap the loop body in `std::panic::catch_unwind` and log.

### 4. Minor — crash between FLAC rename and partial removal produces two queued files
`finalize_with` (`recorder/mod.rs:446-483`): if `<stem>.flac` already exists the partial is renamed to `<stem>.wav` and returned as the recording. At the next start, `recover_dir` also adopts the `<stem>.flac` (`finished_stem` accepts both). Both get indexed (different paths) and auto-uploaded → two jobs for one recording. Window is small (between `rename` and `remove_file`) but it is exactly the crash case recovery exists for.
**Fix:** when `flac_path.exists()`, run `flac::verify(&flac_path, partial)`; if it returns `info.frames`, remove the partial and return the FLAC; otherwise keep the current fallback.

### 5. Minor — default-device switch with a failing reopen shows no warning
`engine.rs:386-396` clears `s.src`, sets `lost_at = None` and forces an immediate reopen. If that reopen fails (`engine.rs:415-418`) `lost_at` stays `None`, so the "设备已断开" warning is never pushed (`engine.rs:531-537`) while the track is silence-filled and retried every 2 s indefinitely. The user sees a healthy status with a silent channel (the 60 s silence warning fires later, with a misleading cause).
**Fix:** in the `Ok(Err(e))` and `Disconnected` arms set `s.lost_at.get_or_insert(now)`.

### 6. Minor — process-loopback open failure does not fall back to the device
`engine.rs:274-294`: only a *gone* pid falls back to whole-device capture. If `AudioClient::new_application_loopback_client` fails (Windows < 10 2004, policy, transient), the system track is dropped for the session ("不可用，本次未录制") and the recording proceeds mic-only. Consistent with the contract's wording but surprising for the primary use case.
**Fix:** on `Err` with `process.is_some()`, retry `env.open(kind, device, None)` once and push a bilingual "per-app capture unavailable, recording the whole device" warning.

### 7. Minor — missing file is permanent even when only job creation remains
`uploader/mod.rs:245-252` fails permanently when `job_id.is_none()` and the file is gone, although with `upload_id.is_some()` the next step (`create_job`) needs no file. A user who moved the FLAC after a successful upload but before the job existed (e.g. backend down at that moment) must restore the file to get a job.
**Fix:** condition on `u.job_id.is_none() && u.upload_id.is_none()`; the 404-upload-gone path already handles the re-upload case (it will then hit the missing-file check on the next step).

### 8. Minor — Linux: idle sink monitor vs 5 s read timeout
`pulse_capture.rs:29` sets a 5 s read timeout; `reader()` treats any read error as a dead stream and the engine reopens every 2 s. With `module-suspend-on-idle` (default on PulseAudio) the monitor source stops delivering data while the sink is suspended, so a quiet system produces a reopen/warn cycle every ~7 s. Linux is secondary (EXECUTION_LOG §14), so Minor.
**Fix:** distinguish `WouldBlock`/`TimedOut` from other errors in `reader()` and `continue` (the timeline silence-fills), or send an empty "keep-alive" packet.

### 9. Minor — sidecar config consistency and command surface
`SidecarConfig.port` (health check) and `args` (`--port 8000`) are independent (`ipc.rs` defaults, `sidecar.rs:165-185`); a user editing one leaves the other stale → 120 s timeout then kill of a healthy server on another port. `sidecar_set_config` (`app/mod.rs:192`) lets the WebView set an arbitrary executable + args that the app then runs; CSP mitigates XSS, but this is the only command→RCE path in the IPC surface.
**Fix:** in `set_config`, reject configs whose `args` contain `--port`/`--port=` with a different value (or inject `--port {port}` when absent). Consider confirming a changed `command` with a native dialog.

### 10. Minor — contract: non-bilingual warning
`engine.rs:533-537` pushes `"{}设备已断开，正在重新连接（期间以静音填充）"` without an English half; all other warnings and `docs/desktop-ipc.md` ("bilingual, shown verbatim") are bilingual.
**Fix:** append ` / {label_en} device disconnected, reconnecting (silence filled meanwhile)`.

### 11. Minor — test coverage gaps for D58-adjacent failure modes
`FakeEnv::default_device_id` always returns `None`, so the default-device-switch path (`engine.rs:386-396`) and `Timeline::reset_rate` after a reopen with a new rate (`engine.rs:405-411`) are never exercised; the app-level auto-stop (`app/mod.rs:424`) is Tauri-bound and untested (acceptable, but finding 2 shows the gap). EXECUTION_LOG §15 notes `slow_reopen_does_not_drop_healthy_track` failed once under load on Windows; it asserts real-time counts (`engine.rs` test, ±0.2 s) and will stay flaky on loaded CI.
**Fix:** add a `FakeEnv` knob for `default_device_id` and a reopen rate (e.g. 48 k → 44.1 k) with assertions on `stats` and output length; widen the timing tolerance or drive the fake clock deterministically.

### 12. Nit — unwrap/expect on runtime paths
`recorder/mod.rs` uses poison-tolerant `lock()`; `app/mod.rs:52`, `config.rs`, `uploader/mod.rs:113, 224`, `queue.rs`, `sidecar.rs` use `lock().unwrap()`. `uploader/mod.rs:109` `.expect("spawn uploader")`, `sidecar.rs:266` `.expect("http client")`, `recorder/mod.rs:255` `.expect(...)` (invariant-guarded). **Fix:** a shared `fn lock<T>(m: &Mutex<T>) -> MutexGuard<T>` helper in `util.rs`; map the two `expect`s to `fail(...)`/`log::error!`.

### 13. Nit — `explorer.exe` by bare name
`app/mod.rs:262` `Command::new("explorer.exe")`: CreateProcess searches the application directory first (per-user install dir is user-writable). Same trust domain, but trivially hardened: `Path::new(&std::env::var_os("WINDIR")).join("explorer.exe")`.

### 14. Nit — capabilities broader than needed
`capabilities/default.json` grants `core:default` (includes `core:tray`, `core:menu`, `core:window` operations). The SPA only needs `core:event:default` (and `core:app`/`core:path` if used). Not a security issue (no fs/shell), least-privilege only.

### 15. Nit — fixed 300 s request timeout
`client.rs:142` applies 300 s to every request including 8 MiB `put_part` (`client.rs:216`); on a < ~230 kbit/s uplink the same part times out forever (transient → backoff → same part). **Fix:** per-request `.timeout()` for parts scaled by size, or a smaller `part_size` negotiated with `init`.

### 16. Nit — `WavReader::open` skips the `WAVE` check
`wav.rs` `WavReader::open` checks `RIFF`/`data` only; `repair` checks `WAVE` too. Harmless (recorder-produced files), consistency only.

## Rule / contract conformance

- AGENTS rule 8: satisfied for crash, sleep, disk-full, device loss by design and tests (see "correct"); the two Major items are the remaining ways audio can be lost or an upload stalled without the user being told.
- Rule 3 (secrets): key never in `backend.json`, masked in IPC, not in logs, not forwarded on redirect. OK.
- `docs/desktop-ipc.md`: command set, payload casing, mono/`per_channel` option rewriting, newest-first list, warning semantics all match; one bilingual-string deviation (finding 10).
- ARCH_REVIEW §4 g (MSVC CI): still open; not a code finding.
- D49/D58: implemented as described; coverage gaps listed in finding 11.

## Merge verdict

**OK with notes.** No blocker for the current branch. Findings 1 and 2 should be fixed before the next release (both are < 20-line changes with straightforward tests); 3–11 are follow-ups.