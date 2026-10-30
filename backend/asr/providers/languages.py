"""Language-hint normalisation shared by every provider (web-free, model-free).

``JobOptions.language_hints`` means the same thing whichever provider runs the job: ``zh-CN`` /
``zh_Hans`` / ``Chinese`` are ``zh``, ``en-US`` is ``en``, and ``auto`` or a code no engine knows
is no hint at all.
"""

from __future__ import annotations

from collections.abc import Sequence

# Codes Qwen3-ASR can be forced to (transformers' ``LANGUAGE_CODE_TO_NAME`` of qwen3_asr); the
# DashScope qwen-audio ASR models accept the same family of codes as ``language_hints``.
KNOWN_LANGUAGES: frozenset[str] = frozenset(
    {
        "ar",
        "yue",
        "zh",
        "cs",
        "da",
        "nl",
        "en",
        "fil",
        "fi",
        "fr",
        "de",
        "el",
        "hi",
        "hu",
        "id",
        "it",
        "ja",
        "ko",
        "mk",
        "ms",
        "fa",
        "pl",
        "pt",
        "ro",
        "ru",
        "es",
        "sv",
        "th",
        "tr",
        "vi",
    }
)

_ALIASES = {
    "chinese": "zh",
    "mandarin": "zh",
    "cmn": "zh",
    "english": "en",
    "cantonese": "yue",
    "zh-yue": "yue",
    "japanese": "ja",
    "korean": "ko",
}


def normalize_language(hint: str) -> str | None:
    """``zh-CN`` / ``zh_Hans`` -> ``zh``, ``en-US`` -> ``en``; ``auto`` or unknown -> ``None``."""
    h = hint.strip().lower().replace("_", "-")
    if not h or h == "auto":
        return None
    if h in _ALIASES:
        return _ALIASES[h]
    if h in KNOWN_LANGUAGES:
        return h
    primary = h.split("-", 1)[0]
    primary = _ALIASES.get(primary, primary)
    return primary if primary in KNOWN_LANGUAGES else None


def normalize_hints(hints: Sequence[str]) -> list[str]:
    """Normalised hints in order, duplicates and non-hints (``auto``, unknown) removed."""
    return list(dict.fromkeys(c for h in hints if (c := normalize_language(h))))
