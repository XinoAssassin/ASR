"""Note-style exports: Markdown (portable and SiYuan-flavoured) and the SiYuan kernel client.

Core library: no FastAPI / ``server`` imports (AGENTS.md rule 5). Callers pass metadata that the
core ``Transcript`` does not carry (title, source file, dates, model, summary) as ``ExportMeta``.
"""

from asr.export.markdown import ExportMeta, Flavor, Locale, render_markdown, siyuan_doc_attrs

__all__ = ["ExportMeta", "Flavor", "Locale", "render_markdown", "siyuan_doc_attrs"]
