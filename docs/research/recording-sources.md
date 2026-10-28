# Research: Recording sources — iPhone, Apple Watch, Windows "what you hear"

Intended location in the repo: `docs/research/recording-sources.md` (written to the run's artifact path per task instructions; no repo file was modified).
Context: PLAN.md §4.3–4.5 (decode → 16 kHz mono, channel policy, decode-only preprocessing, 1 GB DashScope temp-upload cap).

## Summary

All three sources are already decodable by the bundled PyAV path (AAC/ALAC in M4A, WAV float32, MP4/MKV from meeting
tools). The only pipeline-relevant differences are (1) **channel semantics** — iPhone 16/17 Voice Memos and iOS call
recordings arrive as 2-channel files that are *correlated*, whereas an OBS/Voicemeeter mic+loopback capture can be
*independent* per channel — and (2) **file size** — only lossless/WAV captures exceed the 1 GB cloud cap within a
single meeting (48 kHz float32 stereo WAV hits 1 GB after ~43 min). Meeting-tool native recordings (Zoom/Teams/
Tencent/Feishu) are better sources than loopback capture when available because they are mixed server-side at
consistent levels and can provide per-participant tracks or speaker-attributed transcripts.

## A. iPhone Voice Memos and call recordings

### Format table

| Source | Container / codec | Sample rate | Bitrate | Channels | Size / hour | Evidence |
|---|---|---|---|---|---|---|
| Voice Memos "Compressed" (default) | M4A / AAC-LC, CBR | 48 kHz (recent iOS; older iOS 44.1 kHz) | 64 kbps | mono (built-in mic, pre-iPhone 16) | ≈ 29 MB | cBlog measurement [1]; SysFrontier repair guide: "recent iOS Voice Memos 48000 Hz, older 44100 Hz" [2]; iTunes report "AAC 44.1 kHz 64 kbps mono" (2010) [3] |
| Voice Memos "Lossless" | M4A / ALAC | 48 kHz | VBR, ≈ 400–550 kbps mono speech (inference from 768 kbps raw × ~55–70 % ALAC ratio) | mono / stereo | ≈ 180–250 MB mono, ×2 stereo | cBlog [1]; Apple Mac guide confirms the Compressed/Lossless setting [4] |
| Voice Memos on iPhone 16/17 (Spatial Audio mode) | M4A / AAC or ALAC | 48 kHz | per channel as above | **exported as 2-ch stereo** | ×2 | Apple: supported iPhones "automatically record in Spatial Audio"; Export: "Spatial Audio becomes stereo" when exported to Files [5][6] |
| iOS 18.1+ Phone/FaceTime call recording | M4A / AAC-LC | 44.1 kHz | nominal 256 kbps (measured 202 kbps) | **2 channels (L R)** | ≈ 90–115 MB | mediainfo dump by R. Littles [7]; Apple: saved to Notes "Call Recordings" folder, transcript identifies speakers, "Save Audio Files" exports [8] |
| Carrier voicemail | M4A / AAC-LC | 44.1 kHz | 128 kbps nominal | 2 ch | ≈ 45 MB | [7] |
| Typical 3rd-party call recorders (e.g. Rev Call Recorder) | MP3 | 22.05 kHz | 32 kbps CBR | mono | ≈ 14 MB | [7] (note: telephony-grade, ~8 kHz bandwidth) |

### Behaviour that matters for ASR

1. **Claim:** Voice Memos default is AAC-LC 64 kbps CBR mono at 48 kHz; Lossless is ALAC at 48 kHz. **Sources:** [1][2][3]. **Support:** direct evidence (independent measurements, 2010/2019/2024). **Confidence:** medium-high (no Apple spec sheet; the 44.1→48 kHz change is inferred from dated measurements).
2. **Claim:** "Enhance Recording" (iOS 14+, not 17) is a *playback* option that "reduces background noise and room reverberation"; it is not baked into the file, and exporting/sharing yields the unenhanced audio. **Sources:** Apple/9to5Mac/AppleInsider feature description [9][10]; Apple playback-options note "does not change the recording itself" [11]; user report that exports lose the enhancement [12]. **Support:** direct evidence for the playback-only nature; Apple's Export page says sharing options can "retain any layers or recording effects" on current iOS, so an *enhanced* export is possible if the user opts in [6]. **Confidence:** medium. **Implication:** by default our pipeline receives raw mic audio (good — matches the decode-only policy). Tell users *not* to export with effects.
3. **Claim:** Control Center mic modes (**Voice Isolation** / **Wide Spectrum**) apply to Voice Memos recordings and are baked in at capture time. **Sources:** Apple guide "Filter out background sounds" [5]. **Support:** direct. **Confidence:** high. **Implication:** Voice Isolation is a non-linear suppressor aimed at the *nearest* voice; for multi-speaker meetings it can attenuate far speakers and hurt diarization. Recommend "Standard" (or Wide Spectrum) for round-table recordings. (Researcher inference on the multi-speaker effect; no benchmark found.)
4. **Claim:** iPhone 16/17 record Spatial Audio by default and the exported M4A is stereo; older models record mono unless "Stereo Recording" is on. **Sources:** [5][6]. **Support:** direct. **Confidence:** high. **Implication:** the 2-ch correlation heuristic in [0b] must classify these as *correlated* (multi-mic array of one device) → downmix. Layered recordings (iPhone 16 Pro, iOS 18.2+) are flattened on export [6].
5. **Claim:** iOS 18.1+ call recordings are 2-channel AAC at 44.1 kHz; **whether each party sits on its own channel is not documented** — the one mediainfo dump shows "L R" only. **Sources:** [7][8]. **Support:** direct for format; missing for per-party layout. **Confidence:** low on channel semantics. **Implication:** let the heuristic decide at runtime and expose the `channels: per-channel` override; Apple's own Notes transcript (speaker-identified, zh supported) is a useful cross-check for the user.
6. **AAC pitfalls (inference, low impact):** AAC-LC at 64 kbps applies an encoder low-pass well above 8 kHz; since the pipeline resamples to 16 kHz (Nyquist 8 kHz) the codec band-limit is irrelevant. 22.05 kHz/32 kbps MP3 call recorders *are* band-limited to telephony quality — expect lower CER, nothing to fix in preprocessing. iOS applies AGC/noise suppression on the mic path; no public spec, so treat all iPhone audio as "already processed" and never stack another denoiser by default (consistent with PLAN §4.5).

### Pipeline handling and user guidance (A)

- Accept `.m4a` (AAC and ALAC), `.caf`, `.mp3`. PyAV decodes ALAC natively; no special path.
- Channel policy: 1-ch → as is; 2-ch from Voice Memos → correlated → average (or take L). Call recordings → heuristic; show the detected policy in the job UI.
- Resample 48/44.1 kHz → 16 kHz in the decode pass; no upsampling case.
- Cloud cap: Compressed memos never hit 1 GB (≈ 34 h). Lossless mono exceeds 1 GB at ≈ 4–5 h; Lossless stereo/Spatial at ≈ 2–2.5 h → transcode to 16 kHz mono FLAC (≈ 60–110 MB/h for speech; inference) or MP3 64 kbps before temp upload. Trigger: `size > policy.max_file_size_mb`.
- Tell the user: keep **Compressed** (64 kbps is already above what a 16 kHz ASR front-end can use); mic mode **Standard** (not Voice Isolation) for multi-speaker; do not export with "effects"; place the phone centrally, screen up; disable Low Power Mode for long sessions.

## B. Apple Watch Voice Memos (watchOS 9–26)

| Property | Value | Evidence |
|---|---|---|
| Container / codec | M4A / AAC-LC | HowToGeek analysis of a watch memo: "mono M4A … variable bitrate of 65 kbps … sample rate of 48 kHz" [13] |
| Sample rate / bitrate / channels | 48 kHz, ≈ 65 kbps VBR, mono | [13] |
| Size / hour | ≈ 29 MB | arithmetic from [13] |
| Quality setting | none on the watch; iPhone's Compressed/Lossless setting is not documented to apply to watch recordings | Apple watch guide lists no settings [14] (absence of evidence) |
| Sync | automatic via iCloud to iPhone/iPad/Mac, shown under "Apple Watch Recordings"; one-way (phone memos do not sync to the watch) | Apple [14][15] |
| Max duration | **disputed** — Apple documents no limit [14]; users report a 20-min auto-stop (2025) [16] and older 1-min/30-min stops tied to watchOS versions [17] | contradiction recorded below |
| Mic | near-field beamformed MEMS array with voice-oriented processing; Apple's "Audio Intelligence" (Series 11/S11) does *not* record audio [18] | Apple [18]; capability claims otherwise are vendor/blog opinion |

7. **Claim:** Watch memos are the same class of file as iPhone Compressed memos (AAC mono ~64 kbps, 48 kHz), so they need no special decode path. **Sources:** [13]. **Support:** direct (single measurement). **Confidence:** medium.
8. **Realistic expectations (researcher inference):** the wrist mic is optimised for the wearer; other speakers across a table arrive 10–20 dB lower and reverberant. Expect usable ASR for the wearer, degraded CER and weaker CAM++ embeddings for distant speakers, and more missed turns at overlap. Neither upsampling (already 48 kHz) nor a denoiser helps in principle; the PLAN's cited studies show single-channel enhancement raises WER for modern ASR. The linear **peak-safety gain** [0d] is the right default; offer `dereverb: wpe` (linear) as the only toggle worth benchmarking for watch audio.
9. **Operational guidance:** enable Do Not Disturb, keep the watch on the table facing speakers, and split long sessions (do not rely on >20-min single takes until verified on the user's watchOS). If the 20-min cap is real, instruct the user to upload all parts; the UI could offer "concatenate parts in order" later.

## C. Windows 11 "what you hear" capture

### Capture methods

| Method | Format delivered | Notes | Evidence |
|---|---|---|---|
| WASAPI loopback (Audacity "Windows WASAPI" + "(loopback)" device, OBS "Desktop Audio") | render endpoint mix format: IEEE float32, commonly 48 kHz stereo | fully digital; captures *all* apps incl. system sounds; stream only delivers data while something plays — Audacity pauses, NAudio pads silence | Microsoft [19]; Audacity manual [20]; NAudio docs [21]; StackOverflow (44.1 kHz on some machines) [22] |
| "Stereo Mix" | driver-dependent, typically 16/24-bit 48 kHz | often absent/disabled on modern drivers; cannot capture USB/BT/HDMI outputs; analog-style path | Audacity manual [20] |
| Process loopback (`ActivateAudioInterfaceAsync` + `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`) | same as loopback, but restricted to one process tree (or everything except it); not tied to an endpoint | requires Windows 10 build 20348+ / Windows 11; used by OBS "Application Audio Capture" and the win-capture-audio plugin | Microsoft sample [23]; bozbez/win-capture-audio [24] |
| OBS multi-track recording | MKV / Hybrid MP4 (OBS 30.2+) with up to 6 AAC tracks | mic on track 1, desktop/app audio on track 2 → separable | OBS KB [25][26] |
| Windows Sound Recorder app | mic only; no loopback option (unverified — not checked against Microsoft docs this run) | — | missing evidence |

10. **Claim:** Loopback audio is delivered in the engine mix format (float32, usually 48 kHz stereo) and only while audio is playing. **Sources:** [19][20][21]. **Support:** direct. **Confidence:** high. **Implication:** silence gaps in Audacity loopback recordings are *removed*, which breaks timestamps vs. wall clock — prefer OBS (which pads) or NAudio-style paced mixers.
11. **Claim:** Mic and loopback run on independent clocks and drift; correct handling is wall-clock pacing or adaptive resampling, and WASAPI packet timestamps are unreliable on some drivers. **Sources:** NAudio docs [21]. **Support:** direct. **Confidence:** high. **Implication:** if the user supplies a 2-channel "mic L / loopback R" file produced by a proper recorder (OBS, Voicemeeter, NAudio multiplex), channels are already time-aligned; if they supply two *separate* files, we should not try to align them in v1 — document as unsupported.
12. **Resampling artifacts (inference):** 48 kHz → 16 kHz with PyAV's `AudioResampler` (swresample, sinc) is transparent for speech; the only real artifact source is the *meeting codec* on the remote side (Opus/SILK/Satin at 16–24 kHz effective bandwidth, with the sender's noise suppression already applied). Nothing to do locally.
13. **Mixed vs. separate channels (inference, follows PLAN [0b]):** a loopback-only capture is one acoustic "room" (all remote participants) → normal diarization. A mic+loopback capture with the two sources on separate channels is the ideal case: run per-channel pipelines, label L = local, R = remote, and run diarization only on the remote channel (it may still contain several people). A *summed* mono capture works but relies on diarization for everything and local/remote level mismatch is common → peak-safety gain helps; offer `loudness: ebu_r128` as an opt-in.

### Are the meeting tools' own recordings better? (C′)

| Tool | Local recording | Per-speaker tracks | Transcript | Evidence |
|---|---|---|---|---|
| Zoom | MP4 video + `audio_only.m4a` (AAC) | yes — "Record a separate audio file for each participant" (local), one M4A per speaking participant | cloud only | Zoom support: computer recordings "can include … separate audio tracks for each participant" [27]; Zoom blog [28] |
| Microsoft Teams | cloud MP4 (H.264/AAC) in OneDrive/SharePoint; no local file | no | yes — speaker-attributed, downloadable as .docx/.vtt; Graph API | Microsoft support [29][30] |
| Tencent Meeting 腾讯会议 | local: MP4 + optional separate `.m4a` ("同时录制音频文件"); raw `wemta/wemtv` | no (local); cloud transcript attributes speakers by account and can re-diarize a shared device | cloud "录制转写" with 逐字稿, export txt/srt/vtt; also accepts uploads ≤ 4 GB / 8 h | Tencent Cloud docs [31][32][33] |
| Feishu 飞书 | local MP4; cloud → 妙记 (Minutes) | no | yes (妙记, speaker-labelled) | Feishu article [34] (marketing-grade source; medium confidence) |

14. **Claim:** Native recordings are preferable to loopback when the user is the host: Zoom's per-participant M4As give clean single-speaker tracks (diarization unnecessary; just merge by timestamp), and Teams/Tencent/Feishu transcripts give speaker names that our output can be reconciled against. **Support:** interpretation of [27]–[34]. **Confidence:** medium-high. Loopback remains the fallback when the user is not host or the platform forbids recording.

### Pipeline handling and user guidance (C)

- Accept `.wav` (PCM/float), `.flac`, `.mkv`, `.mp4`, `.m4a`, `.opus/.ogg`, `.webm`; probe stream count and pick the audio stream with the user-selectable track index (OBS multi-track needs `stream_index`).
- Channel policy: 2-ch loopback → correlated → average. 2-ch "mic L / remote R" → independent → per-channel, skip diarization on L. Users who record this way must say so; keep the explicit `channels` override and show the auto-detected result.
- Cloud cap: WAV 48 kHz float32 stereo = 1.38 GB/h (≈ 43 min to 1 GB); 16-bit stereo = 0.69 GB/h; 24-bit stereo = 1.04 GB/h. OBS AAC 160 kbps ≈ 72 MB/h; Zoom/Tencent M4A tens of MB/h. **Always transcode WAV/FLAC before temp upload** (16 kHz mono FLAC, or two mono FLACs for per-channel jobs since the cloud `channel_id=[0,1]` path needs both channels — keep a 16 kHz *stereo* FLAC in that case, ≈ 120–220 MB/h inference).
- Tell the user: use OBS (Desktop Audio = WASAPI loopback or Application Audio Capture for the meeting app only; Mic on track 1, desktop on track 2; MKV or Hybrid MP4, 48 kHz, AAC ≥ 128 kbps) rather than Audacity loopback (gaps) or Stereo Mix (missing/analog); disable system sounds; if the user is host, prefer the platform's native recording with per-participant audio (Zoom) or transcript export (Teams/Tencent/Feishu).

## Decision table

| Source | Expected file | Channel policy | Resample | Transcode for cloud? | Toggle to suggest |
|---|---|---|---|---|---|
| iPhone Voice Memos Compressed (≤ iPhone 15) | M4A AAC 64k mono 48k | mono | 48→16 k | no (29 MB/h) | none; mic mode "Standard" |
| iPhone 16/17 Voice Memos (Spatial/Stereo) | M4A 2-ch | correlated → downmix | 48→16 k | Lossless only (>1 GB after ~2 h) | none |
| iPhone Voice Memos Lossless | M4A ALAC 48k | as above | 48→16 k | yes when > 1 GB (~4–5 h mono) | none |
| iOS 18+ call recording | M4A AAC 256k 2-ch 44.1k | heuristic; user override | 44.1→16 k | no | `channels: per-channel` if parties are split (verify on first file) |
| 3rd-party call recorder | MP3 32k mono 22.05k | mono | 22.05→16 k | no | none (quality ceiling is the source) |
| Apple Watch Voice Memos | M4A AAC ~65k mono 48k | mono | 48→16 k | no | peak-safety gain (default); optional `dereverb: wpe` after M4 benchmark |
| Windows loopback only (Audacity/OBS) | WAV float32 48k stereo or MKV/MP4 AAC | correlated → downmix | 48→16 k | WAV: yes (1 GB at ~43 min) | none; warn about Audacity gaps |
| Windows mic + loopback, separate channels/tracks | 2-ch WAV or multi-track MKV | independent → per-channel, no diarization on mic ch | 48→16 k | WAV: yes | `channels: per-channel`; `loudness: ebu_r128` opt-in |
| Windows mic + loopback summed | mono/stereo | downmix | 48→16 k | WAV: yes | peak-safety gain; diarization carries all speakers |
| Zoom local with per-participant M4A | N × M4A AAC | one mono file per speaker | →16 k | no | future: "multi-file, one speaker each" job type |
| Teams / Tencent / Feishu cloud | MP4/M4A + speaker-labelled transcript | downmix | →16 k | MP4 video: extract audio first | future: import platform transcript as speaker hints |

## Upload UI recommendations

- Accepted extensions: `m4a, mp3, wav, flac, aac, caf, ogg, opus, webm, mp4, m4v, mov, mkv`. Reject nothing by codec; let PyAV probe and show codec/rate/channels/duration before the job starts.
- Show the detected **channel policy** (mono / stereo-downmix / per-channel) with a one-click override; default per-channel only when the correlation heuristic says independent.
- Size hint: "Cloud provider: files over the temp-upload limit (read from policy, ~1 GB) are transcoded to 16 kHz FLAC before upload." Show the estimated post-transcode size.
- Source hints (collapsible, Chinese UI copy): iPhone — "保持默认'压缩'音质；多人会议请在控制中心选'标准'麦克风模式；导出时不要勾选保留效果"; Apple Watch — "每段不超过 20 分钟，开启勿扰"; Windows — "推荐 OBS：麦克风=音轨1、桌面/应用音频=音轨2，MKV 或 Hybrid MP4；避免 Audacity loopback 的静音跳过；主持人优先用会议软件自带录制/转写".
- For OBS multi-track files, expose an audio-track selector (stream index) and the option "treat each track as a separate channel".

## Contradictions

- Apple Watch max recording length: Apple documents none [14]; a 2025 Reddit thread reports a hard 20-min cap [16]; 2019–2021 Apple Community threads report 1-min stops after specific watchOS updates [17]. Unresolved; treat 20 min as the practical planning figure.
- Voice Memos sample rate: 44.1 kHz (2010–2017 reports [3]) vs 48 kHz (2019+ [1][2]). Resolved as a version change, but not confirmed by Apple.
- iOS call recording channel semantics: AI-generated search summaries say "mono mixed"; the only measured file is 2-ch [7]. Measured evidence wins; per-party separation unknown.

## Missing evidence

- No Apple specification for Voice Memos bitrate, sample rate, or ALAC bit depth; all numbers are third-party measurements.
- Whether iPhone's Compressed/Lossless setting affects Apple Watch recordings.
- Whether iOS call recordings put each party on its own channel (needs one test file through the correlation heuristic).
- Windows 11 24H2-specific loopback behaviour: nothing authoritative found beyond the API requiring build 20348+; OBS forum has a 24H2 tag but no verified regression.
- Zoom per-participant M4A codec parameters; Teams/Tencent AAC bitrates (sizes above are estimates).
- Windows Sound Recorder loopback capability (not checked).
- No published CER/DER benchmarks for Apple Watch or loopback audio with Qwen3-ASR / CAM++ — needs M4 bake-off clips from each source.

## Sources

- Kept: [1] cBlog Voice Memos codec measurement (https://yaritakunai.hatenablog.com/entry/voice-memos-quality) — AAC 64 kbps CBR 48 kHz / ALAC 48 kHz
- Kept: [2] SysFrontier m4a repair guide (https://sysfrontier.com/en/2024/01/15/methods-to-repair-m4a-recording-files/) — 48 kHz recent vs 44.1 kHz older
- Kept: [3] Apple Community 2010 thread (https://discussions.apple.com/thread/2546598) — AAC 44.1 kHz 64 kbps mono (historical)
- Kept: [4] Apple Voice Memos Mac settings (https://support.apple.com/guide/voice-memos/change-voice-memos-settings-vm0ba246ff3e/mac) — Compressed/Lossless option
- Kept: [5] Apple "Make a recording in Voice Memos" (https://support.apple.com/guide/iphone/make-a-recording-iph4d2a39a3b/ios) — Spatial default, Recording Mode, Voice Isolation/Wide Spectrum
- Kept: [6] Apple "Export a recording to Files" (https://support.apple.com/guide/iphone/iph831c37815/ios) — exports .m4a; Spatial → stereo; effects optional on share
- Kept: [7] R. Littles, iOS call recording mediainfo (https://raleighlittles.github.io/blog/comparison-of-iphone-call-recording-methods/) — measured formats
- Kept: [8] Apple "Record and transcribe a call" (https://support.apple.com/guide/iphone/record-and-transcribe-a-call-iph57c6590e9/ios) — Notes folder, speaker-identified transcript, export
- Kept: [9] 9to5Mac iOS 14 Voice Memos (https://9to5mac.com/2020/06/30/voice-memos-ios-14/); [10] AppleInsider (https://appleinsider.com/articles/20/06/30/voice-memos-gains-enhanced-recording-smart-folders-in-ios-14-and-macos-big-sur) — Apple's "background noise and room reverberation" wording
- Kept: [11] Apple "Play it back" playback-options note (https://support.apple.com/guide/ipod-touch/play-it-back-iphebbd067ce/ios); [12] AppleVis report exports lose enhancement (https://applevis.com/forum/ios-ipados/voice-memos-app-does-not-export-enhanced-audio)
- Kept: [13] How-To Geek Apple Watch memo analysis (https://www.howtogeek.com/why-i-use-my-apple-watch-as-a-microphone-for-videos-and-how/) — mono, 65 kbps VBR, 48 kHz
- Kept: [14] Apple Watch Voice Memos guide (https://support.apple.com/guide/watch/voice-memos-apd441786282/watchos); [15] iCloud sync guide (https://support.apple.com/guide/iphone/keep-recordings-up-to-date-iph38b91c7af/ios)
- Kept: [16] Reddit r/Voicenotesai 20-min cap (https://www.reddit.com/r/Voicenotesai/comments/1jzi62q/); [17] Apple Community watch stop reports (https://discussions.apple.com/thread/250632820)
- Kept: [18] Apple Audio Intelligence on Apple Watch (https://support.apple.com/en-us/148354) — no audio recorded
- Kept: [19] Microsoft Loopback Recording (https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording); [23] Application loopback sample (https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/) — build 20348+
- Kept: [20] Audacity "Recording Computer Playback on Windows" (https://manual.audacityteam.org/man/tutorial_recording_computer_playback_on_windows.html) — WASAPI loopback vs Stereo Mix, pause on silence
- Kept: [21] NAudio MixMicrophoneAndSystemAudio (https://github.com/naudio/NAudio/blob/main/Docs/MixMicrophoneAndSystemAudio.md) — float32 48 kHz, drift, gaps, multiplex to L/R
- Kept: [22] StackOverflow loopback format (https://stackoverflow.com/questions/36139556/) — 44.1 kHz float on some machines
- Kept: [24] bozbez/win-capture-audio (https://github.com/bozbez/win-capture-audio); [25] OBS multi-track guide (https://obsproject.com/kb/multiple-audio-track-recording-guide); [26] OBS Hybrid MP4 (https://obsproject.com/kb/hybrid-mp4)
- Kept: [27] Zoom "Starting a computer recording" (https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0076922); [28] Zoom podcast blog (https://www.zoom.com/en/blog/using-zoom-for-podcasts/)
- Kept: [29] Teams recordings (https://support.microsoft.com/en-us/teams/meetings/play-share-and-download-meeting-recordings-in-microsoft-teams); [30] Teams transcripts (https://support.microsoft.com/en-us/teams/meetings/start-stop-and-download-live-transcripts-in-microsoft-teams-meetings)
- Kept: [31] Tencent Meeting local recording (https://cloud.tencent.com/document/product/1095/53664); [32] 录制转写 (https://meeting.tencent.com/support/topic/1866/index.html); [33] upload limits (https://meeting.tencent.com/support/topic/2062/index.html)
- Kept: [34] Feishu 妙记 article (https://www.feishu.cn/content/article/7602953897327070434) — vendor marketing, used only for feature existence
- Rejected/deprioritized: AI-generated search summaries (Gemini) — internally inconsistent numbers (e.g. 22.05–44.1 kHz, "~128 kbps" watch memos); xconvert/tinrec/blackboxrecorder SEO pages — no measurements; Hill 2017 thesis (UC Denver) — valid but pre-dates the 48 kHz change.

## Next steps

1. Record one 2-minute clip from each source (iPhone 16/17 Spatial memo, iOS call recording, Apple Watch, OBS mic+loopback 2-track) and run them through the [0a]–[0b] probe to validate the correlation heuristic thresholds — especially the call-recording channel layout.
2. Add these clips to the M4 bake-off so CER/term-recall per source is known before writing UI copy about expected quality.
3. Verify the Apple Watch 20-minute cap on the user's watchOS version before documenting it as a hard limit.
