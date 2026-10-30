# Frontend review — `frontend/` @ main `db52182`

Scope: `src/api/**`, `stores/**` (+tests), `views/**`, `components/**`, `platform/**`, `locales/**`, router, `App.vue`, `vite.config.ts`, `package.json`. Read-only; no files modified. Context read: AGENTS.md, ARCH_REVIEW §4 e/f, EXECUTION_LOG D6/D12/D43–D45/D55/D57, desktop-ipc.md.

## Summary

| # | Severity | Area | Finding | Location |
|---|---|---|---|---|
| 1 | **Major** | Autosave / lost edits | Open editor keeps a stale `draft`; the next keystroke after Replace-all / Accept-correction / 409-reload silently overwrites the server change for that segment | `components/SegmentRow.vue:46-50`, `views/TranscriptView.vue:156,329` |
| 2 | **Major** | Rendering perf | No virtualisation; `piecesFor(seg)` runs for every segment on every parent render (each active-segment change during playback, each search keystroke) and re-renders all rows | `views/TranscriptView.vue:644-649`, `:176-189` |
| 3 | Minor | Autosave / 409 | Edits of writes queued *behind* the write that got the 409 are restored to `pendingText` but not re-applied to the reloaded view → screen ≠ what "Retry save" will send | `stores/transcript.ts:302-316,340-351` |
| 4 | Minor | Autosave / lost edits | Typing into a segment while a structural op (merge) that removes it is in flight is dropped silently by `withPending` | `stores/transcript.ts:189-204`, `views/TranscriptView.vue:275-280` |
| 5 | Minor | SSE | Fatal stream error (e.g. 200 + HTML from a mis-pointed proxy) stops the stream and no polling fallback is started; the job row never updates | `stores/jobs.ts:101-121` |
| 6 | Minor | Waveform perf | Every accepted save rebuilds all regions (`clearRegions` + N × `addRegion`) | `components/WaveformPlayer.vue:156`, `views/TranscriptView.vue:78-85` |
| 7 | Minor | Audio memory / UX | Whole MP3 is fetched as one Blob before playback; no progress, Range (D12) unused; decoded buffer + Blob both retained | `stores/transcript.ts:216-234`, `api/download.ts:17-31` |
| 8 | Minor | A11y | Segment action buttons are `opacity: 0` until hover; keyboard focus lands on invisible buttons; `.segment` not focusable; drop-zone is a click-only `div`; ↑/↓ buttons have no label | `components/SegmentRow.vue:215-227`, `views/UploadView.vue:185-189`, `views/TranscriptView.vue:593-594` |
| 9 | Minor | Settings / key | API key stored plaintext in `localStorage`; retained when the backend URL is changed (sent to the new host on the next request) | `api/client.ts:19-23,190-198`, `stores/settings.ts:107-114` |
| 10 | Minor | Tests | `api/events.ts` (reconnect/back-off/fatal path) has no unit test | `api/events.ts` |
| 11 | Nit | Convention | Views/components call `@/api` directly although `endpoints.ts` says "components never talk to the backend directly"; `jobs.create()` is dead | `views/UploadView.vue:25,142`, `components/DirectoryPicker.vue:12`, `views/SettingsView.vue:26-30`, `stores/jobs.ts:156` |
| 12 | Nit | i18n | Fullwidth `：` / `（）` hardcoded in templates and `describe()` regardless of locale; `settings.local.auto` leaks an internal milestone ("见 M4 结论") | `composables/useApiErrors.ts:14,47`, `components/JobOptionsForm.vue:32`, `locales/zh-CN.ts:149` |
| 13 | Nit | Type safety | `as never` used to pass `data-testid` to a dialog button | `views/JobsView.vue:96` |
| 14 | Nit | Misc | `startsWith(r.path)` root match without separator; `toLowerCase()` index mapping in `occurrences`; `/config.js` absolute path ignores `BASE_URL`; SSE headers captured once per stream | `components/DirectoryPicker.vue:41`, `utils/highlight.ts:29`, `index.html:8`, `api/events.ts:50` |

Verdict: **OK with notes** (no Critical). #1 should be fixed before the proofreading feature is relied on; #2 before hour-long transcripts are routine.

## Correct (verified)

- **D6 — key never in a URL.** `apiUrl()` + `authHeaders()` is the only path for SSE (`api/events.ts:49-50`), audio/export blobs (`api/download.ts:19`) and XHR parts (`api/upload.ts:88`). `exportPath`/`audioPath` carry no key; `<audio src>`/`<a href>` to the backend are not used. `openWhenHidden: true` with the documented reason (`events.ts:52-54`).
- **SSE reconnection** (`api/events.ts:56-100`): retryable vs fatal split mirrors `isRetryableStatus`; back-off resets on open; terminal status aborts the controller so `done` settles; `StreamClosedError` forces a retry on silent drops.
- **Autosave core (D45/D55/D57)**: 600 ms debounce (`transcript.ts:399-400`), single serial `editChain` (`:362`), `expected_updated_at` read at run time so own writes are included (`:340`), conflict epoch drops queued ops after a 409 (`:338, 348`), `restoreEdits` guarded by `seq` (`:302-308`), per-transcript id capture so late answers never touch another transcript (`:207-211`). Route guards (`TranscriptView.vue:481-495`), `beforeunload` tied to `hasUnsavedChanges` (`transcript.ts:170-178`), `pagehide` keepalive (`:155-157`). Tests cover 409 reload, retry, type-back-mid-save, switch A→B, reset, LLM isolation (`transcript.test.ts:144-460`).
- **UTF-16 ↔ code point**: `utf16ToCodePoint`/`codePointToUtf16` handle surrogate pairs and clamp (`utils/text.ts`); split caret converted in `splitOffset` (`components/interaction.ts:181-185`); correction offsets converted with an `indexOf` fallback when the text moved (`utils/highlight.ts:59-80`); tests with emoji + CJK exist (`text.test.ts:57-61`, `interaction.test.ts:113-121`). Replace sends `ignore_case: true` to match the client-side count.
- **Upload chunking** (`api/upload.ts`): sequential parts with exponential retry, `onSession` after init and each ack, resume from `nextPart`, 404 → start over once, target/size mismatch → new session; `complete` retried (server idempotent). Store keeps the session on error/cancel and restarts exactly STARTABLE items (`uploads.test.ts`). Remove is disabled while uploading, so the `for…of` over a replaced array cannot start a removed file.
- **Tauri gating**: `@tauri-apps/*` only in `platform/tauri.ts`, loaded via dynamic `import('./tauri')` behind `isDesktop()` (`platform/index.ts:20-25`); `grep` confirms no other import. `platform/types.ts` matches `desktop-ipc.md` field-for-field. Recorder/sidecar stores and `App.vue` guard with `available`/`isDesktop()`.
- **Type safety**: `strict` + `noUncheckedIndexedAccess` (`tsconfig.json`); zero `any` in `src/` outside the generated schema; `endpoints.ts` derives every DTO from `components['schemas']`; `ReplaceOp` widening is documented. `pnpm build` runs `vue-tsc --noEmit`.
- **i18n parity**: `en: MessageSchema = {…}` (`locales/en.ts:3`) makes missing/extra keys a compile error; no hardcoded Chinese in templates besides the locale toggle label; `fallbackLocale: 'zh-CN'`.
- **Secrets**: no `v-html`/`innerHTML`; DashScope key is write-only and masked (`SettingsView.vue:400-435`); desktop card shows only `keySet`.
- **Blob lifecycle**: `saveBlob` revokes after 60 s (`download.ts:46`); store nulls `audio` and aborts the fetch on `reset()`/`load()` (`transcript.ts:487-501`); `WaveformPlayer` calls `destroy()` on unmount (`:148-152`). wavesurfer's own object URL from `loadBlob` is revoked by the library on reload/destroy in v7+ — not verifiable here (no `node_modules`); worth a one-line check against the pinned 8.x.

## Findings

### 1. Major — Stale editor draft overwrites server-side changes
`SegmentRow.vue:46-50` only syncs `draft` from `props.segment.text` when `!props.editing`. `TranscriptView.replaceAll` (`:156-168`) and `review()` (`:329-331`) do **not** close the editor (unlike `structural()` at `:278` and `revertAll()` at `:320`). Sequence: user double-clicks segment S and types; runs Replace-all (or accepts a correction in S, or a 409 reload brings another client's text for S); the PATCH succeeds and `accept()` updates `segments[S].text`; the open `NInput` still shows the pre-replace `draft`; the next keystroke emits `draft + key` → `editText` → autosave PATCHes the stale text back. The replacement/correction/remote edit for S is silently undone and the correction's `offset` no longer matches. No toast, no conflict.
**Fix (smallest):** in `replaceAll` and `review` set `editingId.value = null` before `applyOps` (as `structural` does). More robust: in `SegmentRow` track `lastEmitted` and in the `segment.text` watcher do `if (!props.editing || text !== lastEmitted) draft.value = text`.

### 2. Major — No virtualisation; per-render `piecesFor` for every segment
`TranscriptView.vue:644-649` renders every `SegmentRow` (each with `NDropdown`, 3–4 `NButton`s) inside a `max-height: 65vh` scroller, and computes `:pieces="piecesFor(seg)"` in the template. `piecesFor` (`:176-189`) runs `termMarks` (terms × `indexOf`), `correctionMarks`, `searchMarks` and `pieces()` (`marks.filter` per cut) per segment. The parent re-renders on every `activeId` change during playback (`:93-94`, every segment boundary), every search keystroke, `editingId`, `structuralPending`, `saveState`. Each call yields a fresh `pieces` array, so all N rows re-render too. For a 2 h meeting (~2–3 k segments, dozens of terms) this is thousands of string scans and a full list diff several times per second while playing. `segmentTexts` (`:112`) also rebuilds an N-entry record on every transcript replacement.
**Fix:** (a) memoise pieces per segment: a `computed(() => new Map(segments.map(s => [s.id, piecesFor(s)])))` keyed on text/terms/corrections, with the search marks layered in a cheaper second pass; (b) use a virtual list (`NVirtualList` from naive-ui or `vue-virtual-scroller`) with `scrollTo` switched to the list's `scrollTo({ key })`; (c) `defineOptions({ inheritAttrs:false })` + `v-memo="[seg.text, active, editing, busy, piecesKey]"` on the row as an interim.

### 3. Minor — Edits restored after a 409 are not re-displayed
`enqueueWrite` (`transcript.ts:340-351`): the write that receives the 409 restores its edits (`:344`) *before* `reloadAfterConflict` → `accept(fresh, true)` → `withPending` (`:313-314, 210`). Writes already queued behind it fail with the synthetic 409 (`:338`) *after* the reload and call `restoreEdits` (`:344`) — their text returns to `pendingText` but `transcript.value` already shows the server's text for those segments. `hasUnsavedChanges` is true, the banner says "unsaved text kept", yet the screen shows the remote version; "Retry save" then overwrites what the user is looking at. Reachable with a slow backend (two autosaves queued) plus a concurrent editor. Not covered by `transcript.test.ts:247-257` (ops only, no text).
**Fix:** after `restoreEdits(edits)` when `isCurrent(id)`, re-apply: `if (transcript.value) transcript.value = withPending(transcript.value)`; add a test with two queued `editText` batches and a 409 on the first.

### 4. Minor — Typing into a segment removed by an in-flight merge is dropped
`structural()` (`TranscriptView.vue:275-280`) clears `editingId` but nothing prevents `startEdit` on another row while `structuralPending` is true (`busy` only disables split/merge buttons). If the user starts typing into S2 while `merge S1` (which absorbs S2) is in flight, the response lacks S2 and `withPending` (`transcript.ts:193-196`) deletes the pending entry silently.
**Fix:** either `if (structuralPending.value) return` in `startEdit`, or have `withPending` surface dropped entries (set `saveError`/a toast via a `droppedEdits` ref).

### 5. Minor — Fatal SSE error leaves the job without updates
`jobs.ts:112` `onError: () => stopStream(id)` removes the stream; `overflow` (`:117`) is computed only inside `syncLive`, so no poll timer is started for that job. A reverse proxy answering `200 text/html` (SPA fallback on a wrong backend URL) or a 404 for a just-deleted job turns into a frozen row until manual refresh — and the refresh re-opens the same failing stream.
**Fix:** on fatal error record `failed.add(id)` and treat `live.some(id => !streams.has(id))` (already true) by starting the poll timer from the `onError` callback too (`if (!pollTimer) pollTimer = setInterval(...)`); clear `failed` on terminal.

### 6. Minor — Regions rebuilt on every save
`regions` (`TranscriptView.vue:78-85`) is a new array whenever `store.transcript` is replaced (every accepted write); `WaveformPlayer.vue:156` then `clearRegions()` and re-adds N DOM regions. Text-only edits do not change any region.
**Fix:** compare a cheap key (`regions.map(r => r.id+r.start+r.end+r.color).join()`) before re-rendering, or diff by id and only add/remove/update changed regions.

### 7. Minor — Audio: full Blob download, Range unused, two copies in memory
`loadAudio` (`transcript.ts:216-234`) fetches the whole MP3 via `fetchBlob` → `response.blob()` (`download.ts:30`) before `WaveformPlayer` gets anything; there is no progress (just a spinner) and D12's Range support is unused because the header-auth constraint forbids `<audio src>`. The Blob is kept in the store for the page lifetime while wavesurfer holds the decoded `AudioBuffer`. For a 2 h file: ~60–100 MB Blob + decoded PCM (hundreds of MB depending on wavesurfer's decode `sampleRate`).
**Fix (incremental):** show download progress via `response.body` reader; release `audio.value` after `loadBlob` resolves (wavesurfer owns the object URL); for long files pass backend-generated `peaks` + `duration` to `WaveSurfer.create` so no client-side decode is needed (backend addition).

### 8. Minor — Keyboard accessibility
`.actions { opacity: 0 }` shown only on `:hover/.active/.editing` (`SegmentRow.vue:215-227`): Tab focus reaches the hidden `NButton`s with no visible focus. `.segment` has no `tabindex`/role, so the only keyboard route is the global Enter on the *active* segment. The upload drop-zone is a `div` with `@click` (`UploadView.vue:185-189`), no `role="button"`/`tabindex`/key handler. Search ↑/↓ buttons (`TranscriptView.vue:593-594`) have no accessible name. Shortcuts are documented in `proofread.shortcuts`, which is good.
**Fix:** add `.segment:focus-within .actions { opacity: 1 }`; `role="button" tabindex="0" @keydown.enter/.space` on the drop-zone; `:aria-label` (new i18n keys) on the step buttons.

### 9. Minor — API key in `localStorage`, kept across backend-URL changes
`STORAGE_KEYS.apiKey` (`client.ts:19-23`) is plaintext `localStorage`: readable by any script on the origin (no XSS vector found in-app; third-party scripts: only same-origin `config.js`). `setBackendUrl` (`settings.ts:107-114`) keeps the key, so after a user points the SPA at a new host the old key is sent there on the next request. Clearing works (save empty → `removeItem`, `client.ts:196-198`) but there is no explicit "clear" button.
**Fix:** prefer `sessionStorage` or at least offer a "remember key" toggle; when the backend URL changes to a different origin, clear the key (or prompt); add a "Clear" button next to the key field.

### 10. Minor — `events.ts` untested
`client.test.ts`, `upload.test.ts` exist; nothing exercises `watchJob` (retry back-off, fatal vs retryable `onopen`, `StreamClosedError`, abort on terminal). This is the component the Jobs page depends on under proxies.
**Fix:** vitest with a mocked `fetchEventSource` driving `onopen/onmessage/onclose/onerror`; assert `onError` only for non-retryable statuses and that `done` resolves after a terminal job.

### 11–14. Nits
- **Convention drift**: `endpoints.ts:3` says stores call these and components never hit the backend, but `UploadView.vue:142` (`createJob`), `DirectoryPicker.vue:12` (`listDirs/listRoots`), `SettingsView.vue:26-30` (`probeHealth`, `listLocalModels`, `downloadLocalModel`) do; `jobs.create()` (`stores/jobs.ts:156`) is unused. Either relax the comment or route through stores.
- **i18n punctuation**: `useApiErrors.ts:14,47`, `TranscriptView.vue:548,551`, `JobOptionsForm.vue:32` hardcode `：`/`（）` for the English locale; `zh-CN.ts:149` "见 M4 结论" references an internal milestone in user-facing copy.
- **`as never`** (`JobsView.vue:96`) to smuggle `data-testid` into `positiveButtonProps` — use `ButtonProps & Record<\`data-${string}\`, string>` or move the testid to the dialog.
- `DirectoryPicker.vue:41` `startsWith(r.path)` matches `/data/uploads2` under `/data/uploads`; add a separator check. `highlight.ts:29` maps lower-cased indices back onto the original string (length can differ for e.g. `İ`); use `localeCompare`-free `toLowerCase` on both only when lengths match, or search with a case-insensitive RegExp over the original. `index.html:8` `/config.js` ignores `BASE_URL` (router uses it); use `%BASE_URL%config.js`. `events.ts:50` captures `authHeaders()` once; a key change mid-stream reconnects with the old key until the stream is recreated.

## Commands for the supervisor to run
- `cd frontend && pnpm typecheck && pnpm test` — confirm the i18n schema and store tests still pass on this revision.
- `pnpm build` and inspect `dist/assets` — verify `@tauri-apps` lands only in the lazy `tauri-*.js` chunk, not the entry.
- Check the pinned `wavesurfer.js` 8.x `Player.setSrc/revokeSrc` for the object-URL revoke claimed in "Correct".