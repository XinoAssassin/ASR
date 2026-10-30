# SiYuan (思源笔记) export

*Research for the Markdown / SiYuan export, 2026-10-30. Sources: the SiYuan API docs and the kernel
source on `master`, plus the Lute Markdown engine; read on the web. No SiYuan instance was installed
or run. Tests mock the kernel with respx (`backend/tests/test_siyuan.py`).*

## Kernel API facts

| Topic | Finding | Source |
|---|---|---|
| Endpoint, method | `http://127.0.0.1:6806`; `POST` with a JSON body; reply `{"code": 0, "msg": "", "data": …}`, non-zero `code` on failure | [API.md][api] |
| Auth | `Authorization: Token <token>` (kernel also accepts `token`, `Bearer` prefixes and `?token=`); token under Settings → About → API token | [API.md][api], [session.go][session] `CheckAuth` |
| Auth failure | HTTP 401 with `{"code": -1, "msg": "Auth failed…"}` | [session.go][session] |
| Loopback without lock-screen password | Kernel lets loopback requests through **without checking the token** (unless `Sec-Fetch-Site` is cross-site). So `POST /api/siyuan/test` can only prove the token when SiYuan has an access authorization code, or is reached over the network | [session.go][session] |
| Non-loopback access | Refused unless SiYuan has an access authorization code (lock-screen password); with one, the token header works | [session.go][session] |
| `lsNotebooks` | `data.notebooks[]`: `id`, `name`, `icon`, `sort`, `closed` | [API.md][api] |
| `createDocWithMd` | `{notebook, path, markdown}` (+ optional `tags`, `parentID`, `id`) → `data` = new doc id (plain string). `path` is the human-readable hpath, `/` separates levels, so a literal `/` in a title must become `／` (U+FF0F) | [API.md][api], [api/filetree.go][ft], [apicontract][contract] |
| Duplicate hpath | Always creates a **new** document; the existing one is not overwritten. Maintainers confirmed it is intended (duplicate titles are allowed) | [API.md][api], [siyuan#18273][i18273] |
| Missing parents | Documented as created automatically, but 3.8.9 regressed to `document metadata is not ready` when a parent is missing. Newer kernels refuse ambiguous parents (`pathAmbiguous`) | [siyuan#21082][i21082], [siyuan#20892][i20892] |
| `getIDsByHPath` | `{path, notebook}` → ids; `[]` when nothing matches; keep the leading `/` | [API.md][api] |
| `setBlockAttrs` | `{id, attrs}`; custom attributes need the `custom-` prefix; `data` null | [API.md][api] |
| `system/version` | `data` = version string | [API.md][api] |
| CORS | Kernel answers every request with `Access-Control-Allow-Origin: *`, allows the `Authorization` header and answers preflights with 204. `Access-Control-Allow-Private-Network: false`, so a page served from a public origin cannot call a LAN/loopback kernel in Chromium | [serve.go][serve] `corsMiddleware` |

### How `createDocWithMd` parses Markdown

The handler calls `model.CreateWithMarkdown`, which uses `util.NewLute()` and
`luteEngine.Md2BlockDOM` ([model/file.go][file], [import_footnotes.go][fn]). That engine
([util/lute.go][lute]) has:

- `SetKramdownIAL(true)`: block IAL lines such as `{: custom-asr-speaker="张三"}` directly after a
  block become that block's attributes (Lute `Md2BlockDOMTree` parses with `KramdownBlockIAL`,
  [lute/protyle.go][protyle]). Kept, so each turn carries its speaker and times.
- `SetYamlFrontMatter(false)`: front matter is **not** recognised; `---` would become thematic
  breaks with the YAML as plain paragraphs. So the push drops front matter and sets the metadata
  as `custom-asr-*` document attributes instead.
- `SetDataTask(true)`, GFM task lists: `- [ ] task` becomes a SiYuan task list.
- Inline syntax enabled by default: `#tag#`, `$math$`, `==mark==`, `^sup^`, `~sub~`, `*`/`_`,
  `((block ref))`, `{{embed}}`. The escaper covers all of them.
- `KeepEscaped`: backslash escapes are kept as escapes (shown as the plain character).
- A single newline inside a paragraph is kept as a line break, so a turn is one paragraph:
  name line + text line.

SiYuan's **file** import (Import → Markdown) is different: it uses `NewStdLute` with
`SetYamlFrontMatter(true)` and maps `title`, `date`, `lastmod`, `tags` to the document and other
keys to `custom-<key>` attributes, keeping the YAML as a code block ([model/import.go][import],
[import_front_matter_test.go][fmtest]). So the plain `format=md` download already imports cleanly.

## Decisions

1. **Server-side push.** The backend calls the kernel. The SiYuan token is stored in the settings
   table and only ever leaves the backend in the `Authorization` header to the configured
   endpoint (AGENTS rule 3). The browser could call the kernel itself (CORS allows it), but then
   the token would live in the browser. Consequence: SiYuan must be reachable **from the backend
   host**. That means the same machine (`http://127.0.0.1:6806`), or for Docker
   `http://host.docker.internal:6806` with a SiYuan access authorization code set, or SiYuan's
   network serving on the LAN with an access authorization code.
2. **Token binding.** Changing `siyuan.endpoint` without sending a token in the same request
   clears the stored token, so a saved token never goes to a host it was not saved for. Requests
   use `follow_redirects=False` and treat a 3xx as an error. Responses are capped at 8 MiB.
   Timeouts: 5 s connect, 30 s overall. Error messages carry no token (redacted defensively).
3. **No overwrite.** Each push creates a new document. If the target hpath exists, the push uses
   `<path> (2)`, `(3)`, … (checked with `getIDsByHPath`). Missing parents are created first as
   empty documents. That works around the 3.8.9 regression, and the explicit check avoids silent
   duplicates.
4. **Attributes.** The document gets `custom-asr-title`, `-date`, `-duration`, `-source`,
   `-provider`, `-model`, `-language`, `-speakers` and `-transcript-id`, plus `tags` via
   `createDocWithMd`. Each turn gets `custom-asr-speaker`, `-speaker-id`, `-start` and `-end`
   (seconds) through IAL, so SiYuan SQL can query for example every turn of one speaker. A failed
   `setBlockAttrs` after the document exists is returned as a warning, not an error, because a
   retry would create a second copy.
5. **No `format=siyuan` file.** SiYuan's own Markdown import reads the `md` front matter (title,
   tags, custom attributes), so a separate file variant would add nothing. The SiYuan-specific
   output (no front matter or H1, per-turn IAL) is only used by the push.

## Markdown template (`format=md`)

YAML front matter with every string double-quoted (JSON escapes, which are valid YAML). Then
`# title`, a one-line `>` summary (date · duration · speakers · provider/model), `## 摘要` (only
with a summary; a stale summary is flagged), a `## 发言人` table (turns, talk time, share), and
`## 文字稿` with one paragraph per segment, or per turn with `merge_same_speaker`. Each paragraph
is `**name** \`hh:mm:ss\`` then a backslash hard break and the escaped text. `## 备注` lists the
warnings. `locale=en` switches headings and labels. The escaping rules are in the module
docstring of `backend/asr/export/markdown.py`.

[api]: https://github.com/siyuan-note/siyuan/blob/master/docs/API.md
[session]: https://github.com/siyuan-note/siyuan/blob/master/kernel/model/session.go
[ft]: https://github.com/siyuan-note/siyuan/blob/master/kernel/api/filetree.go
[contract]: https://github.com/siyuan-note/siyuan/blob/master/kernel/apicontract/filetree_remaining.go
[file]: https://github.com/siyuan-note/siyuan/blob/master/kernel/model/file.go
[fn]: https://github.com/siyuan-note/siyuan/blob/master/kernel/model/import_footnotes.go
[lute]: https://github.com/siyuan-note/siyuan/blob/master/kernel/util/lute.go
[protyle]: https://github.com/88250/lute/blob/master/protyle.go
[serve]: https://github.com/siyuan-note/siyuan/blob/master/kernel/server/serve.go
[import]: https://github.com/siyuan-note/siyuan/blob/master/kernel/model/import.go
[fmtest]: https://github.com/siyuan-note/siyuan/blob/master/kernel/model/import_front_matter_test.go
[i18273]: https://github.com/siyuan-note/siyuan/issues/18273
[i21082]: https://github.com/siyuan-note/siyuan/issues/21082
[i20892]: https://github.com/siyuan-note/siyuan/issues/20892
