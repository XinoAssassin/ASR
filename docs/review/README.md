# Code review — `main` @ `db52182` (2026-10-30)

Architecture-level review: `../ARCH_REVIEW.md`. Four read-only code reviews by subagents, each on a disjoint area. None found a Critical. Reviewers could not run tests or linters (no shell); the supervisor should run `uv run pytest`, `pnpm test && pnpm typecheck`, and `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test` in WSL before fixing.

| Report | Critical | Major | Minor | Nit |
|---|---|---|---|---|
| [core-and-providers.md](core-and-providers.md) | 0 | 1 | 7 | 6 |
| [server.md](server.md) | 0 | 1 | 8 | 7 |
| [frontend.md](frontend.md) | 0 | 2 | 7 | 4 |
| [desktop.md](desktop.md) | 0 | 2 | 9 | 5 |

## Majors (fix first)

| # | Area | Finding | Location |
|---|---|---|---|
| C-M1 | Local merge (D35) | Text outside token spans is silently dropped at segment boundaries (leading `《“$`, trailing `++`, `℃`, emoji); sentence ends after symbols are missed → transcript text loss | `backend/asr/providers/local/merge.py:231-239, 311-312` |
| S-M1 | Chunked upload / disk | `complete` needs ~2× file size free (3× across volumes); init checks only 1× + 256 MB, nothing reserved across the 16 sessions; ENOSPC → bare 500 | `backend/server/storage.py:387, 470-481` |
| F-1 | Autosave / lost edits | Open editor keeps a stale `draft`; next keystroke after Replace-all / Accept-correction / 409-reload overwrites the server change for that segment | `frontend/src/components/SegmentRow.vue:46-50`, `views/TranscriptView.vue:156,329` |
| F-2 | Rendering | No virtualisation; `piecesFor` runs for every segment on every parent render → hour-long transcripts lag during playback and search | `frontend/src/views/TranscriptView.vue:644-649, 176-189` |
| D-1 | Sidecar | App-started backend inherits only the user env; default `ASR_CORS_ORIGINS` lacks `http://tauri.localhost` → the SPA cannot call its own sidecar | `desktop/src-tauri/src/sidecar.rs:185-200` |
| D-2 | Rule 8 | A recorder thread that panics or exits without setting `fatal` is never detected; UI stays "recording" while nothing is written | `desktop/src-tauri/src/recorder/mod.rs:170-175`, `app/mod.rs:424` |

## Cross-cutting themes

1. **Cloud `per_channel` diverges from local (D36).** Cloud never diarizes channel 0 (`core m1`), so desktop recordings sent to the cloud lose remote-speaker separation silently. Either implement the D36 rule on the cloud path (transcode ch 0 to mono, `diarization_enabled` on, fixed speaker for ch 1) or warn in the UI when provider=cloud and policy=per_channel.
2. **Long-running LLM calls in request handlers** (`server m5`, ARCH_REVIEW §4-c): block the 40-thread anyio pool, no single-flight per transcript, disconnect does not stop the paid call. Move to job kinds on the existing queue.
3. **Open (no-key) mode is weaker than D54 claims** (`server m3`): same-host forwarders and proxies without proxy headers bypass the loopback-peer check; `Host: localhost` is spoofable. Document the limit or require a key whenever a proxy is in front.
4. **Secret redaction depends on the entry point** (`server m4`): only `asr serve` installs the filter; the documented `uvicorn server.app:app` run has none. Install it in `app.py` lifespan.
5. **Crash-window duplicates**: desktop finalize (`desktop 4`) and server `complete.lock` (`server m1`) both leave state that causes duplicate jobs or permanent 409s after a crash at the wrong instant.
6. **Windows test coverage** (`server m8`, `desktop 11`): no Windows path-shape tests; symlink tests error without Developer Mode; device-switch and auto-stop-on-fatal untested.
7. **Error-mapping inconsistencies** (`core n4`, `server n1`): allow-list and 403 errors mapped differently across routes.

## Suggested order

1. C-M1, F-1, D-2 — data-loss class (transcript text, user edits, recorded audio).
2. D-1, S-M1 — blockers for the desktop sidecar and large uploads.
3. Theme 1 (cloud per_channel) and theme 2 (LLM on the queue) — architectural, already in ARCH_REVIEW §5.
4. F-2, then the Minors by area.
5. Re-run the full test matrix in WSL; add Windows path tests and the D58 device-switch test.
