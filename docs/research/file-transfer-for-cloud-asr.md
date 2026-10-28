# Research: Making local audio reachable by URL for DashScope `qwen-audio-3.1-asr-flash-filetrans`

Context: PLAN.md §4.4 (cloud provider). The filetrans API accepts only `input.file_urls` (HTTP/HTTPS, one URL per request); it never accepts upload bytes. Files: up to 2 GB, typically 10 min – 2 h. Backend may sit behind NAT (home/office) or in Docker on a server.

Intended destination per task: `docs/research/file-transfer-for-cloud-asr.md` (this run wrote only to the authoritative output path).

## Summary

Use **DashScope's own temporary upload (`/api/v1/uploads` → `oss://` URL + `X-DashScope-OssResourceResolve: enable`) as the default** for single-user/dev use: it is free, same-region, needs no extra account, and works with the filetrans REST endpoint on both `dashscope.aliyuncs.com` and `maas.qianwenaiapi.com`. Its hard caps are 1 GB per upload (interface limit; model allows 2 GB) and an explicit "do not use in production" label. Use **Alibaba Cloud OSS (Beijing) + presigned GET URL + 1-day lifecycle rule as the production fallback** — it is the vendor-recommended path, same region as the model, and costs on the order of ¥0.1–1 per 2-hour file. Cloudflare R2 is attractive on price (zero egress) but has no evidence of reliable reachability from Alibaba Cloud Beijing, so it is a third choice. Exposing the backend via tunnels is not recommended for this workload (ngrok free: 1 GB/month out; Cloudflare Tunnel: large-file policy on Free plan; Tailscale Funnel: beta + unspecified bandwidth caps).

## Findings

### 1. DashScope temporary file upload (option a)

1. **Claim:** Flow is `GET /api/v1/uploads?action=getPolicy&model=<model>` → OSS PostObject form upload to `data.upload_host` → URL = `oss://` + key. URL is valid **48 h**; file is bound to the **same model name and same Alibaba main account**; file cannot be listed, modified, or downloaded afterwards. **Sources:** [Bailian: 上传本地文件获取临时URL](https://help.aliyun.com/zh/model-studio/get-temporary-file-url). **Support:** direct evidence. **Confidence:** high.
2. **Claim:** Interface cap is **1 GB per file** ("接口上传文件大小不得超过1GB"); `data.max_file_size_mb` in the policy response is the effective per-model cap and should be read at runtime. **Sources:** same doc. **Support:** direct evidence. **Confidence:** high. *Researcher inference:* a 2 GB WAV must be transcoded (e.g. to 16 kHz mono FLAC/MP3) before temp upload; a 2 h 16 kHz mono WAV is ~230 MB, 2 h 128 kbps MP3 ~115 MB, so re-encoding keeps nearly all real inputs under 1 GB.
3. **Claim:** The doc says "请勿用于生产环境" twice (48 h expiry; `getPolicy` limited to **100 QPS** per account+model, not expandable) and recommends OSS for production. **Sources:** same doc; [filetrans HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api). **Support:** direct evidence. **Confidence:** high. *Interpretation:* for a single-tenant tool submitting a few jobs per hour, the 100 QPS cap is irrelevant; the real risk is "no SLA / may change".
4. **Claim:** Works with this model: the filetrans HTTP API page states `file_urls` accepts `oss://` temporary URLs **via REST only**; the DashScope SDK "does not support `oss://` temporary URLs" and "SDK does not support configuring request headers". Header `X-DashScope-OssResourceResolve: enable` is required ("不推荐该方式" is the doc's wording). **Sources:** [filetrans HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api). **Support:** direct evidence. **Confidence:** high. *Implication:* implement the cloud provider with httpx against REST (as PLAN.md already does), not the `dashscope` SDK's `Transcription` class.
5. **Claim:** Qianwen AI Platform has the identical feature at `https://maas.qianwenaiapi.com/api/v1/uploads` (same getPolicy/PostObject/`oss://` flow, 48 h, 100 QPS, model/account binding, same header). **Sources:** [platform.qianwenai.com: 上传文件获取临时 URL](https://platform.qianwenai.com/docs/api-reference/more/upload-file-get-temporary-url). **Support:** direct evidence. **Confidence:** high. Note: that page does not print the 1 GB figure; it defers to `max_file_size_mb`. The Qianwen model page lists ¥0.8/M input, ¥2.7/M output, 600 RPM ([qianwenai model page](https://www.qianwenai.com/models/qwen-audio-3.1-asr-flash-filetrans)).
6. **Claim:** Model limits: single file ≤ 2 GB and ≤ 12 h; **with `diarization_enabled` the doc recommends ≤ 2 h** or the task "may fail or time out". Result JSON `transcription_url` is valid 24 h. Sub-task error `FILE_DOWNLOAD_FAILED` exists (URL unreachable). **Sources:** [非实时语音识别](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide); [HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api). **Support:** direct evidence. **Confidence:** high.
7. **Security:** the temp object is `x-oss-object-acl: private`, non-overwritable, not downloadable by anyone (including you), and only resolvable by DashScope for your account — the strongest default privacy posture of all options. **Support:** direct evidence (policy fields). **Confidence:** high.
8. **Claim:** Bailian also publishes the workspace-scoped domain `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com` for submit/poll; `parameters` must be present (even `{}`) for this model or the task fails. **Sources:** [HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api). **Support:** direct evidence. **Confidence:** high.

### 2. Cloudflare R2

9. **Claim:** Free tier per month: 10 GB-month Standard storage, 1 M Class A, 10 M Class B ops; egress always free; beyond that $0.015/GB-month, $4.50/M Class A, $0.36/M Class B; `DeleteObject`/`AbortMultipartUpload` are free; `UploadPart`/`CompleteMultipartUpload` are Class A. Usage is rounded up to the next GB-month / million ops. **Sources:** [R2 Pricing](https://developers.cloudflare.com/r2/pricing/). **Support:** direct evidence. **Confidence:** high. *Inference:* with a 1-day lifecycle rule, even 2 GB/day of audio averages well under 10 GB-month → effectively ¥0.
10. **Claim:** R2 is S3-compatible (boto3 with `endpoint_url=https://<account>.r2.cloudflarestorage.com`), supports presigned GET/PUT URLs and lifecycle rules (expire after N days, abort incomplete multipart). **Sources:** [R2 Pricing](https://developers.cloudflare.com/r2/pricing/) (lifecycle ops listed); vendor summaries. **Support:** interpretation (pricing page lists the operations; presigned-URL/lifecycle docs not fetched this run). **Confidence:** medium-high.
11. **Claim (decision-critical, unverified):** Reachability of `*.r2.cloudflarestorage.com` / `*.r2.dev` from mainland China is widely reported as slow/unstable/blocked; custom domains help partially. **No evidence specific to Alibaba Cloud's DashScope fetcher in Beijing was found.** **Sources:** community/blog reports only (e.g. [tanglu.me](https://blog.tanglu.me/cloudflare-R2-configure/), Cloudflare community threads). **Support:** interpretation. **Confidence:** low. *Researcher inference:* DashScope downloads from its Beijing region; cross-border fetch of a 100 MB–2 GB object risks `FILE_DOWNLOAD_FAILED`. Must be empirically tested before relying on R2. Public bucket vs presigned: use presigned GET (private bucket); a public `r2.dev` URL is both GFW-exposed and unauthenticated.

### 3. Alibaba Cloud OSS (Beijing)

12. **Claim:** Billing structure: public outbound traffic (`NetworkOut`) is charged; inbound and intra-region internal-endpoint traffic are free; requests billed separately. Unit prices commonly cited: Standard LRS ¥0.12/GB-month; public egress ¥0.50/GB busy (08–24) and ¥0.25/GB off-peak; ¥0.01 per 10k requests. **Sources:** [OSS 流量费用](https://help.aliyun.com/zh/oss/traffic-fees) (structure, direct); unit prices from [aliyun price page](https://www.aliyun.com/price/detail/oss) and secondary summaries (not parsed this run). **Support:** structure = direct; unit prices = interpretation. **Confidence:** medium. *Inference:* a 2-hour job costs ≈ ¥0.06 (115 MB MP3) to ≈ ¥1 (2 GB WAV) in egress plus negligible storage if deleted within a day. The `source_check` call returned no passages, so unit prices were not machine-validated.
13. **Claim:** Presigned (signed) URLs: V4 signature via SDK up to 7 days; STS-derived up to 12 h; cannot be revoked early except by rotating the key. Lifecycle rules can delete by prefix after N days and clean incomplete multipart parts; rules take effect within ~24–48 h. **Sources:** [下载使用预签名URL](https://www.alibabacloud.com/help/zh/oss/developer-reference/download-using-a-presigned-url); [生命周期规则](https://help.aliyun.com/zh/oss/user-guide/configure-lifecycle-rules-to-manage-object-versions). **Support:** interpretation (from search summaries citing these docs). **Confidence:** medium.
14. **Claim:** Alibaba's own docs recommend OSS as *the* production path for filetrans ("文件托管：将音频文件上传至阿里云 OSS … 避免使用本地文件上传"). **Sources:** [非实时语音识别 → 生产环境建议](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide). **Support:** direct evidence. **Confidence:** high. *Inference:* same-region fetch (Beijing→Beijing) is the lowest-latency, lowest-failure path; cheaper than R2 only in the sense that R2 is $0 but may not work — OSS costs cents per job and is reliable.
15. **Security:** keep bucket private; use short-lived presigned GET (≤ a few hours — the task is async but typically completes in minutes); a dedicated RAM user with `oss:PutObject/GetObject/DeleteObject` on one bucket prefix; delete the object when the job reaches a terminal state and keep a 1-day lifecycle rule as backstop. **Support:** researcher recommendation.

### 4. Exposing the backend directly (signed URL + tunnel)

16. **Claim:** ngrok Free: **1 GB/month data transfer out**, 20k HTTP requests/month, 3 endpoints, one `*.ngrok-free.app` dev domain, interstitial on browser traffic (bypassable with a header; not applied to programmatic clients). **Sources:** [ngrok Free Plan Limits](https://ngrok.com/docs/pricing-limits/free-plan-limits). **Support:** direct evidence. **Confidence:** high. *Inference:* unusable — one 2 h job can exceed the monthly quota; Hobbyist (5 GB) is still too small.
17. **Claim:** Cloudflare Tunnel (named tunnel, free, requires a domain on Cloudflare; quick `trycloudflare.com` tunnels need no account but are ephemeral): public-hostname traffic on Free/Pro/Business plans is subject to Cloudflare's large-file/video policy, and Cloudflare "may redirect your content or take other actions" if an app serves "a disproportionate amount of large files". Proxy read timeout is 125 s (time to first byte). **Sources:** [Delivering Videos with Cloudflare](https://developers.cloudflare.com/fundamentals/reference/policies-compliances/delivering-videos-with-cloudflare/); [Connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/). **Support:** direct evidence. **Confidence:** high. The widely cited 100 MB *request-body* limit applies to uploads through Cloudflare, not to the GET downloads DashScope would perform (interpretation; community sources). *Inference:* works technically for occasional audio downloads but is ToS-grey and depends on home uplink speed plus cross-border Cloudflare↔Alibaba path.
18. **Claim:** Tailscale Funnel: free on all plans, beta, only `*.ts.net` names, ports 443/8443/10000, TLS only, "subject to non-configurable bandwidth limits", relayed through Funnel servers (your IP hidden). **Sources:** [Tailscale Funnel](https://tailscale.com/kb/1223/funnel). **Support:** direct evidence. **Confidence:** high. *Inference:* bandwidth cap unspecified → risky for multi-hundred-MB downloads from Beijing; also a cross-border relay.
19. **Common pros/cons of all tunnels:** no storage account, zero copies of the audio leave your machine except to DashScope; but they (a) require the backend to be up and reachable for the whole fetch, (b) are bottlenecked by home uplink (a 500 MB file at 20 Mbps ≈ 3.5 min; Alibaba's fetch timeout is undocumented), (c) traverse GFW/cross-border paths, (d) widen attack surface (must implement HMAC-signed, single-use, expiring download URLs; never expose `/api/audio/{id}` as-is), and (e) Docker-on-server deployments usually already have a public IP, in which case a plain HTTPS reverse proxy with a signed URL is simpler than any tunnel. **Support:** researcher inference.

### 5. Other object stores (brief)

20. **Backblaze B2:** $6/TB-month, first 10 GB free, egress free up to 3× stored volume then $0.01/GB, S3-compatible with presigned URLs. **Sources:** [Backblaze pricing](https://www.backblaze.com/cloud-storage/pricing). **Support:** interpretation (search summary). **Confidence:** medium. US/EU regions only → same cross-border concern as R2.
21. **AWS S3:** no permanent free tier for this pattern; egress ~$0.09/GB; a Beijing/Ningxia region exists but needs a separate China account + ICP. **Support:** researcher background knowledge (not verified this run). **Confidence:** low-medium.
22. **Tencent COS (Beijing):** Standard ~¥0.118/GB-month, public egress ~¥0.5–0.6/GB, new-user 50 GB/6-month storage credit; presigned URLs supported. **Sources:** [Tencent COS billing](https://www.tencentcloud.com/zh/document/product/436/6240). **Support:** interpretation. **Confidence:** medium. In-China, so reachability is fine, but no advantage over OSS for an Alibaba-hosted model.
23. **MinIO self-hosted:** free software, S3-compatible, presigned URLs — but it still has to be reachable from Beijing, so it inherits all of §4's exposure problems; only sensible if you already run a public VPS in China.

## Recommended default and fallback

- **Default (dev / single user / files ≤ 1 GB after transcode):** DashScope temp upload (REST). Implement: read `max_file_size_mb` from getPolicy; if source > cap, transcode to 16 kHz mono (FLAC or 64–128 kbps MP3/AAC) before upload; submit via REST with `X-DashScope-OssResourceResolve: enable` and non-empty `parameters`; same `model` string for upload and submit; same endpoint family (Bailian key ↔ `dashscope.aliyuncs.com`; Qianwen key ↔ `maas.qianwenaiapi.com`).
- **Fallback / production:** user-supplied OSS bucket in `oss-cn-beijing` (`oss2` multipart upload → presigned GET, e.g. 6 h → delete on terminal job state; 1-day lifecycle rule as backstop). Expose in Settings as `file_transfer: temp_upload | oss | public_url` as already planned.
- **Not recommended:** ngrok (quota), Tailscale Funnel / Cloudflare Tunnel (bandwidth/ToS/cross-border), R2/B2 (unverified reachability from Beijing — test before offering as an option).

### Decision table

| Option | Cost (2 h job) | Size cap | Reach from DashScope BJ | Setup | Security | Verdict |
|---|---|---|---|---|---|---|
| DashScope temp upload | ¥0 | 1 GB/file (model: 2 GB) | Same region, native | None (API key only) | Private, model+account bound, 48 h | **Default** |
| Alibaba OSS Beijing | ≈¥0.06–1 egress + ~¥0 storage | 5 GB single PUT / 48.8 TB multipart | Same region | Bucket + RAM user | Presigned ≤7 d, lifecycle delete | **Production fallback** |
| Cloudflare R2 | ¥0 (free tier) | 5 TB | Unverified; China access reports poor | CF account, S3 keys | Presigned, lifecycle | Test first; 3rd choice |
| Backblaze B2 | ¥0 (<10 GB) | — | Overseas only | B2 account | Presigned | Not for China |
| Tencent COS BJ | ≈ OSS | — | In China, cross-cloud | Tencent account | Presigned | No advantage over OSS |
| Cloudflare Tunnel | ¥0 | n/a (uplink-bound) | Cross-border via CF edge | Domain on CF | Needs own signed URLs; ToS risk | Not recommended |
| ngrok free | ¥0 | 1 GB/**month** | Cross-border | Account | Interstitial, quota | Unusable |
| Tailscale Funnel | ¥0 | n/a, unspecified bw cap | Cross-border relay | Tailscale | TLS, `*.ts.net` | Not recommended |
| MinIO self-hosted | VPS cost | — | Depends on VPS location | Most effort | DIY | Only with existing CN VPS |

## Contradictions

- Temp-upload size: one search summary said 2 GB; the official Bailian doc says 1 GB interface cap (and per-model `max_file_size_mb`). Resolved in favour of the official doc; the 2 GB figure is the *model* input limit.
- Temp URL with SDK: search summaries show `Transcription.async_call(file_urls=[oss_url])`; the official HTTP API page says the SDK does **not** support `oss://` temp URLs. Follow the official page: use REST.
- Cloudflare "100 MB limit": community sources apply it to tunnels generally; it is a request-body (upload) limit and does not constrain DashScope's GET downloads. Not independently verified against the Cloudflare cache-limits page.

## Missing evidence

- No test or documentation on whether DashScope's Beijing fetcher can download from R2/B2 (cross-border). Needs an empirical probe (`FILE_DOWNLOAD_FAILED` is the expected failure mode).
- DashScope's download timeout / minimum throughput for `file_urls` is undocumented; matters for tunnel options.
- OSS unit prices (¥0.12 / ¥0.50 / ¥0.25) and presigned-URL max validity (7 d) were read from search summaries citing official pages, not from parsed primary text; `source_check` returned no passages.
- Whether the Qianwen endpoint's temp upload enforces the same 1 GB cap (its doc only references `max_file_size_mb`).

## Sources

- Kept: Bailian 上传本地文件获取临时URL (https://help.aliyun.com/zh/model-studio/get-temporary-file-url) — authoritative limits for option 1.
- Kept: Bailian filetrans HTTP API (https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api) — `oss://` REST-only, header, diarization ≤2 h, error codes, workspace domain.
- Kept: Bailian 非实时语音识别 (https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide) — 2 GB/12 h, production advice.
- Kept: Qianwen AI 上传文件获取临时 URL (https://platform.qianwenai.com/docs/api-reference/more/upload-file-get-temporary-url) — confirms feature on `maas.qianwenaiapi.com`.
- Kept: Cloudflare R2 Pricing (https://developers.cloudflare.com/r2/pricing/) — free tier and op classes.
- Kept: Cloudflare video/large-file policy (https://developers.cloudflare.com/fundamentals/reference/policies-compliances/delivering-videos-with-cloudflare/) — Tunnel public hostnames covered by policy.
- Kept: Cloudflare connection limits (https://developers.cloudflare.com/fundamentals/reference/connection-limits/) — 125 s proxy read timeout.
- Kept: ngrok Free Plan Limits (https://ngrok.com/docs/pricing-limits/free-plan-limits) — 1 GB/month.
- Kept: Tailscale Funnel (https://tailscale.com/kb/1223/funnel) — beta, bandwidth limits, ports.
- Kept: OSS 流量费用 (https://help.aliyun.com/zh/oss/traffic-fees) — egress billing structure.
- Rejected/deprioritized: SEO pricing aggregators (filebase, nubbo, egresscost, aliyunbaike) — redundant with official pages; ai-tldr/llmlearner model mirrors — non-authoritative; unsourced "R2 from China" AI summary — kept only as low-confidence signal.

## Next steps

1. Empirical probe: upload a 200 MB test file to R2 (presigned) and OSS, submit both to filetrans, compare success/latency. This resolves the only decision-relevant gap.
2. Confirm `max_file_size_mb` returned for `qwen-audio-3.1-asr-flash-filetrans` on both endpoints with a real key.
