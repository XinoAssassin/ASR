# Sample meeting clip

`meeting-sample.ogg` is the clip the first-run setup guide uploads for "try a sample meeting".

It is **fully synthetic**: text-to-speech, no recording of real people, written for this project.
There is no third-party audio in it and no licence issue for redistribution.

- Duration 39.3 s, Ogg Opus, mono, 48 kHz, ~48 kbit/s (~224 KB).
- Two speakers: a female voice (host) and a male voice, Mandarin with English technical terms
  (API, Kubernetes, Jenkins, GitHub Actions, release notes, review, Grafana).
- Voices: Kokoro v1.1-zh (`hexgrad/Kokoro-82M-v1.1-zh`, Apache-2.0), speaker ids 3 (zf_001) and
  60 (zm_*), int8 build from sherpa-onnx (`kokoro-int8-multi-lang-v1_1`), run offline. Windows TTS
  was the first choice, but the dev machine has no zh-CN voice installed.
- Converted from the 24 kHz WAV with PyAV (bundled FFmpeg, libopus); no host ffmpeg.

Script (0.7 s pause between turns):

| Speaker | Line |
|---|---|
| A | 大家好，我们开始今天的周会。先看一下 API 网关的进度。 |
| B | 好的。上周我们把 Kubernetes 集群升级完了，接口延迟降低了大概百分之二十。 |
| A | 很好。那 Jenkins 流水线超时的问题解决了吗？ |
| B | 还没有。GitHub Actions 偶尔会超时，我打算周五之前加上缓存。 |
| A | 可以。另外，下个版本的 release notes 谁来写？ |
| B | 我来写吧，周三之前发给大家 review。顺便把 Grafana 的监控面板也更新一下。 |
| A | 好，那就这么定了。谢谢大家。 |

The local pipeline (Qwen3-ASR 0.6B + CAM++, CPU) transcribes it with two speakers and all terms
except "GitHub Actions" → "GitHub Action" (a term set entry fixes that).
