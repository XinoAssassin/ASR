"""Multi-speaker ASR core library.

Web-free by design (AGENTS.md rule 5): nothing under ``asr`` may import FastAPI or ``server``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("asr")
except PackageNotFoundError:  # running from a source tree without an installed distribution
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
