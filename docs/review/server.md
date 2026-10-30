# Server layer review — `main` @ `db52182`

Scope: `backend/server/**` (app, auth, body_limit, config, db, deps, jobs, storage, schemas, logging_setup, cli, routes/*) and the server tests listed in the task. I read it against AGENTS.md, ARCH_REVIEW §4 b/c/d/f and EXECUTION_LOG D1–D28 and D43–D58. Review only: nothing was edited and no tests were run.

## Summary

| # | Sev | Area | Finding | Location |
|---|---|---|---|---|
| M1 | **Major** | Uploads / disk | Chunked `complete` needs about 2× the file size free in `ASR_DATA_DIR` (3× across volumes). The init check covers only 1× + 256 MB, nothing is reserved across the 16 sessions, and ENOSPC comes back as a bare 500 | `storage.py:387`, `:470-481` |
| m1 | Minor | Uploads / idempotency | A crash during `complete` leaves `complete.lock` behind, so every retry gets 409 until the 24 h TTL | `storage.py:460-463` |
| m2 | Minor | SSE | The subscriber queue is registered outside the generator, so its `finally` never runs if the stream never starts | `routes/jobs.py:184-216` |
| m3 | Minor | Auth (open mode) | The loopback-peer check (D54) is bypassed by any same-host TCP forwarder or reverse proxy that sends no proxy headers, and by UDS peers. `Host: localhost` is trivially spoofed | `auth.py:63-67`, `:104-129`, `cli.py:80` |
| m4 | Minor | Secrets / logs | The redaction filter is installed only by `asr serve`. The documented `uvicorn server.app:app` run has none, and `jobs._redact` covers only the DashScope key | `cli.py:71`, `jobs.py:361`, `app.py:138` |
| m5 | Minor | Threads / cost | LLM suggestions and summary block the shared anyio pool (40 threads) for minutes. There is no single-flight guard per transcript, and a disconnect does not stop the paid call | `routes/transcripts.py:179`, `:248` |
| m6 | Minor | Delete / Windows | `delete_audio=true` on a running or playing job: Windows `unlink` raises PermissionError, giving a 500 after the job row is already deleted, and the upload is orphaned | `routes/jobs.py:248-286` |
| m7 | Minor | Body cap | `/api/uploads` is exempt by prefix, so the JSON routes `init` and `register` have no 2 MB cap | `body_limit.py:16`, `:29-30` |
| m8 | Minor | Tests | No Windows path tests (case, drive-relative, UNC, `\\?\`, junctions). Symlink tests error rather than skip on Windows without Developer Mode. Several race paths are untested | `tests/test_storage.py`, etc. |
| n1 | Nit | Errors | `llm-suggestions` maps the allow-list error to 502; `summary` maps the same error to 422 | `routes/transcripts.py:181` vs `:205-210` |
| n2 | Nit | Duplication | `_inside_roots` re-implements the root containment check that `storage.py` already has | `routes/jobs.py:240-245` |
| n3 | Nit | Settings | `local.diarize_model` is accepted without validation, stored and returned, but never used | `routes/settings.py:79-80` |
| n4 | Nit | Jobs | A cancel that lands between `_settle` and the `succeeded` write returns 200 "running", then the job succeeds | `jobs.py:209-231`, `:547-575` |
| n5 | Nit | D13 | `work/<job_id>` is never cleaned after a crash. On stop it is deleted while the provider thread may still be writing | `jobs.py:136-158`, `:493-500` |
| n6 | Nit | §4f | `SettingsOut` doesn't show whether the DashScope key comes from env or the override, or which fields are env-only | `routes/settings.py:37-56` |
| n7 | Nit | Auth | The header is decoded as latin-1 and re-encoded as UTF-8, so a non-ASCII `ASR_API_KEY` can never match, and nothing reports this | `auth.py:36-40`, `:80-84` |

There are no Critical findings. Merge verdict: **OK with notes**. Fix M1 before the desktop uploader or any real 1–2 GB use.

## Correct (verified)

- **Path containment (rule 2).** `storage._norm` compares realpath + `normcase` and checks containment with `commonpath`. `ValueError` (different drive, UNC vs drive) is treated as outside (`storage.py:131-150`). I traced the Windows cases: `C:foo` (pathlib drive replacement, then outside the root), `\\server\share` (commonpath ValueError), `\\?\` / `\\.\NUL` (prefix kept, so outside), 8.3 short names and junctions (realpath expands and follows them), and case variants (normcase). All of them fail closed. `..` is also rejected on both separators. `resolve_target_dir` re-checks after `mkdir`. Stored paths are re-validated before read (D27, `storage.py:153-166`) by both `jobs._execute` and `audio.get_audio`.
- **Upload file creation.** `create_unique_file` uses `O_EXCL|O_NOFOLLOW`. The cross-device copy checks the inode (`storage.py:278-317`). `safe_filename` handles Windows reserved names, ADS colons, and leading/trailing dots.
- **Auth.** The middleware wraps everything. The key is checked in constant time and only read from the header. Exemptions match exact paths only. Open mode enforces peer, Host and Origin checks before the exemption check (`auth.py:131-139`). CORS is the outer layer, uses `allow_credentials=False`, rejects `*` (`config.py:146-155`), and 401/413 responses carry CORS headers. `asr serve` refuses a non-loopback bind without a key (`cli.py:57-69`).
- **Secrets.** 422 responses drop `input`/`ctx` (`app.py:110-115`, D26). `ConfigError` never chains the pydantic input (`config.py:213-230`). The key is masked to its last 4 characters. `test-cloud` returns the exception type only. Core `ProviderError` messages are built from type names and status codes, never response bodies (`asr/providers/cloud/client.py:134-176`, `asr/correct/llm.py:162-203`).
- **No SSRF on the backend (D3).** `file_url` is validated twice (`schemas.py:182-191`, `routes/jobs.py:35-46`) and only handed to DashScope (`dashscope_filetrans.py:343-351`). The endpoint allow-list is enforced when settings are written (`routes/settings.py:85-93`) and again where the key is used.
- **JobManager races.** The cancel flag is registered before the atomic `queued→running` claim (`jobs.py:327-344`). Duplicate queue entries are dropped after the await. `_settle` releases the run before the terminal status becomes visible, so retry-after-cancel works (tested in `test_jobs.py:266`). An insert after a delete is handled through `IntegrityError` (`jobs.py:556-563`). SSE queues are bounded and drop the oldest snapshot, never the end marker.
- **Optimistic concurrency (D55).** Writes use a conditional `UPDATE … WHERE updated_at=?`, and versions move by at least 2 ms, so the 1 ms tolerance for JS `Date` round-trips can't match a neighbouring version (`db.py:31`, `:443-479`, `routes/transcripts.py:66-96`). Callers that don't send a version get up to 5 recompute retries, so their writes are never lost. The LLM paths merge into the current row.
- **Migrations (D56).** `BEGIN IMMEDIATE`, each step keyed on the presence of its column, rollback on any `BaseException`. Tests cover a half-applied v1 database and a failing step (`test_server_review.py:529-563`). v1→v4 just adds three columns.
- **D13 recovery.** `running` jobs become failed "interrupted by restart" and `queued` jobs are re-enqueued (`jobs.py:136-147`; `test_jobs.py:370`).
- **Single-process limit (§4d).** `workers=1` is forced (`cli.py:76`) and documented in `DEPLOYMENT.md:34`.
- **Chunked complete.** Idempotent in-process: one per-session `asyncio.Lock`, results cached for 1 h, concurrent duplicates wait for the first call (`routes/uploads.py:347-409`; tested).

## Details

### M1 (Major): chunked complete underestimates disk use; ENOSPC gives a 500
`create_session` checks `free >= size + 256 MB` once, at init, for both the data dir and the target (`storage.py:385-388`). `complete_session` then writes `assembled.tmp` into the session dir while all the parts are still there (`storage.py:470-478`), so `ASR_DATA_DIR` peaks at 2× `size`. When the upload root is on another volume (common on Windows: data on C:, media on D:), `move_into` copies across before `rmtree`, so the target needs another 1×. Nothing reserves space for other sessions, so up to 16 sessions of 2 GiB each pass the check on the same free space. The multipart route (`routes/uploads.py:232`) has no free-space check at all. A full disk raises a plain `OSError`, which is not a `StorageError`, so the client gets a 500. A partial `assembled.tmp` stays behind until the next attempt, and a full data disk also breaks SQLite commits for every job.
**Fix:** assemble in place so the peak is about `size + PART_SIZE`: rename part 0 to `assembled.tmp`, then append each later part and unlink it. Also:
- re-check free space at `complete` (data dir, plus the target when `st_dev` differs);
- map `errno.ENOSPC` to `InsufficientStorageError` (507) and unlink `assembled.tmp` in the `finally`;
- subtract the declared sizes of active sessions from the init check;
- apply the same check to the multipart route.
Add a test with `disk_usage` monkeypatched to exactly `size + margin`.

### m1 (Minor): a stale `complete.lock` blocks completion for 24 h
The lock file is created with `O_EXCL` and removed only in `finally` (`storage.py:459-488`). If the process dies mid-assembly (crash, OOM, a Docker kill after the 30 s grace period), every later `complete` returns 409 "retry in a moment" until `SESSION_TTL_S` (24 h). The desktop retry queue (D49/D58) will just keep retrying. Within one process, `_Completions.lock` already serialises calls, and D46 guarantees one process.
**Fix:** on startup, delete `upload-sessions/*/complete.lock`. Alternatively, write the pid and a timestamp into the lock and treat it as stale after N minutes. Add a test for this.

### m2 (Minor): SSE subscriber leak when the stream never starts
`manager.subscribe` runs in the handler (`routes/jobs.py:184`), but `unsubscribe` lives only in the generator's `finally` (`:210-211`). Python never runs the `finally` of an async generator that was never advanced, even through `aclose()`. So if the response is torn down before the first `__anext__` (client gone before `http.response.start` completes, or an error during response start), the queue stays in `_subscribers[job_id]` until the job is deleted or the server stops. It then gets a snapshot on every publish, up to 64 items. Each reconnect loop adds one more. The tests only check unsubscribe on terminal status and on delete (`test_routes_jobs.py:396-430`).
**Fix:** do the 404 check with `get_state` in the handler, then move `subscribe()` and a second `get_state()` to the top of `stream()`, inside the `try`. Add a test that the subscriber count returns to 0 after a client disconnect while the job is still running.

### m3 (Minor): open mode trusts any loopback TCP peer
With an empty `ASR_API_KEY`, the only network gate is "the peer is loopback" (`auth.py:105`), plus a `Host` check that the client controls. These setups all present a loopback peer for remote clients:
- nginx with only `proxy_pass http://127.0.0.1:8000` (the default upstream `Host` is `$proxy_host` = `127.0.0.1:8000`, which passes);
- `netsh interface portproxy` (the usual Windows/WSL LAN recipe), `socat`, `ssh -R`;
- `uvicorn --uds` behind a proxy (`client is None` is treated as loopback, `auth.py:65`).

In each case a LAN client gets the whole API. `--forwarded-allow-ips` helps only when the proxy sends `X-Forwarded-For` and the operator passes the flag. `DEPLOYMENT.md` §6.1 does say to keep the key set behind a proxy, so this is a misconfiguration risk, not a default hole.
**Fix:** in open mode, refuse requests (403) that carry `Forwarded`, `X-Forwarded-For`, `X-Real-IP` or `Via` unless uvicorn's proxy handling is on. Treat `client is None` as non-loopback unless `allow_network` is set. In `DEPLOYMENT.md` §5, state that TCP forwarders defeat the peer check. Add tests with an XFF header and with `client=None`.

### m4 (Minor): log redaction depends on the launch command
`SecretRedactingFilter` is attached only by `configure_logging`, which only `asr serve` calls (`cli.py:71`). AGENTS.md "Commands" and the `app.py` docstring document `uv run uvicorn server.app:app --reload`. That path builds the app at import (`app.py:138`) with uvicorn's default handlers and no filter. `server.*`/`asr.*` warnings then go to the last-resort handler without redaction. The unexpected-error traceback in `jobs.py:360-361` passes through `_redact(trace, secrets)`, which removes only the DashScope key, not signed result/OSS URLs or bearer tokens. The design relies on the handler filter for those (D47).
**Fix:** in `create_app` or the lifespan, attach `SecretRedactingFilter` idempotently to the root and `uvicorn*` handlers (or call `configure_logging` when no `_asr_handler` exists). In `jobs.py:361`, use `asr.providers.cloud.security.redact(...)` together with `_redact`.

### m5 (Minor): LLM calls hold request threads (ARCH §4c)
`llm_suggestions` (`httpx` timeout 120 s, possibly several batches) and `create_summary` (180 s) run through `run_in_threadpool` (`routes/transcripts.py:179`, `:248`). That uses anyio's default limiter of 40 threads, which is shared with every `run_in_threadpool` call on the hot upload path (`fh.write` per chunk in `routes/uploads.py:330`, probe, storage ops, the audio transcode). Problems:
- nothing stops two clicks or two tabs from making two paid calls for the same transcript;
- a client that disconnects, or a proxy that times out at 60 s, does not cancel the call;
- local LLM inference (planned) would make this minutes per call.

**Fix:** short term, a dedicated `anyio.CapacityLimiter(2)` for LLM calls, plus a per-transcript in-flight set that answers 409 "already running". Longer term, move these onto the job queue as job kinds, as ARCH §4c says.

### m6 (Minor): `DELETE /api/jobs/{id}?delete_audio=true` is not Windows-safe
`manager.delete` only sets the cancel flag. The provider thread keeps the file open until its next progress call, and `/api/audio` may be serving the MP3 cache through `FileResponse`. `_remove_audio_files` then calls `unlink` (`routes/jobs.py:251-260`). On Windows that raises `PermissionError`, which is not caught, so the client gets a 500. By then the job row is already deleted (`:276`), the upload row is never deleted (`:283`), and no route exists to delete an upload. The same applies to `os.replace` onto a cache MP3 that is being served (`asr/audio/transcode.py:104`).
**Fix:** catch `OSError` around each unlink, log it, and skip deleting the row so a later cleanup can retry. For running jobs, wait (bounded) for the run's `_release` before removing files. Add a Windows-marked test with the file held open.

### m7 (Minor): JSON upload routes have no body cap
`EXEMPT_PREFIXES = ("/api/uploads",)` (`body_limit.py:16`) also exempts `POST /api/uploads/init` and `/register`. These are pydantic JSON bodies, which FastAPI reads fully into memory before the `max_length` checks run. Only clients with the key, or local clients in open mode, can reach this, but it bypasses the D56 cap.
**Fix:** exempt only `POST /api/uploads` (exact path) and `PUT /api/uploads/*/parts/*` (pattern). Extend `test_upload_routes_keep_their_own_limits` to cover `init` and `register`.

### m8 (Minor): test gaps
- **Windows (the dev machine):** no tests for a root given in different case, `C:foo`, a path on another drive, a UNC path, a `\\?\D:\…` prefix, or a junction escape (`_winapi.CreateJunction` needs no privilege). All `symlink_to` tests (`test_storage.py:83`, `:93`, `:147`, `:233`, `:364`; `test_security.py:108`; `test_routes_fs.py:76`) raise WinError 1314 without Developer Mode instead of skipping. Add a `can_symlink` fixture that skips, plus junction variants.
- **Untested paths:** SSE disconnect cleanup (m2), stale lock (m1), complete when disk space is low (M1), open mode with forwarded headers or a UDS peer (m3), the LLM error status mapping (n1), `delete_audio` while the job runs (m6), and work-dir cleanup at start (n5).

### n1 (Nit): inconsistent LLM error mapping
`llm_suggestions` returns 422 only for "not configured" (`routes/transcripts.py:181`). A disallowed endpoint gives 502 there but 422 from `_llm_error` in the summary route. **Fix:** use `_llm_error` in both.

### n2 (Nit): duplicate containment logic
`routes/jobs._inside_roots` uses `Path.is_relative_to` (`:240-245`), while `storage.py` uses `commonpath` + `normcase`. Both are correct today, but there are now two implementations of rule 2. **Fix:** `storage.containing_root(path, roots) is not None and real != root`.

### n3 (Nit): dead setting
`local.diarize_model` is written without validation (`routes/settings.py:79-80`) and is never read by the pipeline (a grep finds only schema, config and settings uses). **Fix:** validate it against the registry (`kind == "diarize"`) or remove it from the API.

### n4 (Nit): cancel accepted after it can no longer take effect
Once `_settle` (`jobs.py:547`) has removed the run from `_running`, `cancel()` finds neither a queued job nor a running state and returns the DB row (`running`) with 200 (`jobs.py:221-231`). The job then succeeds. **Fix:** return 409 "job is finishing" when the status is `running` but no state is registered, or document the window.

### n5 (Nit): work-dir hygiene
After a crash, `start()` marks jobs interrupted but leaves `data/work/<id>` in place (`jobs.py:143-151`). A retry reuses the directory with `exist_ok`. On `stop()`, `CancelledError` skips every `except` in `_run`, so `_release` calls `rmtree` while the provider thread is still writing there (`jobs.py:369-373`, `:493-500`). **Fix:** `rmtree(data_dir/"work")` in `start()` before any worker starts (nothing can be running then).

### n6 (Nit): settings vs env split not visible (ARCH §4f)
`SettingsOut` has `api_key_set`/`api_key_masked` but not where the key comes from. After `cloud.api_key=""` clears the override, an env key still shows `api_key_set: true`, which looks like the clear failed. CORS, hosts, roots and the API key are not marked read-only. **Fix:** add `cloud.api_key_source: "env" | "settings" | null` and an `env_only` list. Optionally log a startup warning when `provider_default == "cloud"` and no DashScope key is configured.

### n7 (Nit): non-ASCII API key can never match
`_header` decodes the header as latin-1, and `api_key_matches` re-encodes the value as UTF-8 (`auth.py:36-40`, `:80-84`). For a non-ASCII `ASR_API_KEY` the bytes differ, so every request gets 401. **Fix:** require a printable-ASCII `ASR_API_KEY` in `EnvSettings` (a clear `ConfigError`), or compare the raw header bytes.