"""``asr`` command-line interface. Owned by the ``cloud-provider`` agent (M2).

Cloud credentials come from the environment only: ``DASHSCOPE_API_KEY`` (required),
``DASHSCOPE_ENDPOINT`` (default ``https://dashscope.aliyuncs.com``) and ``ASR_CLOUD_MODEL``
(default ``qwen-audio-3.1-asr-flash-filetrans``).
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from asr import __version__
from asr.providers.base import (
    AudioSource,
    JobCancelled,
    JobOptions,
    PreprocessOptions,
    ProgressEvent,
    ProviderError,
    Term,
    TranscriptionProvider,
)

app = typer.Typer(help="Multi-speaker ASR command-line interface.", no_args_is_help=True)
err = Console(stderr=True)

DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com"


class Provider(StrEnum):
    cloud = "cloud"
    local = "local"


class OutputFormat(StrEnum):
    txt = "txt"
    srt = "srt"
    vtt = "vtt"
    json = "json"


class Channels(StrEnum):
    auto = "auto"
    downmix = "downmix"
    left = "left"
    right = "right"
    per_channel = "per_channel"


def parse_term(spec: str) -> Term:
    """``"Kubernetes:5"`` → ``Term("Kubernetes", 5)``; without ``:<int>`` the weight is 3."""
    text, sep, weight = spec.rpartition(":")
    try:
        if sep and text.strip() and weight.strip().isdigit():
            return Term(term=text, weight=int(weight))
        return Term(term=spec)
    except ValidationError as exc:
        msg = exc.errors()[0].get("msg", "invalid term")
        raise typer.BadParameter(f"{spec!r}: {msg}") from None


@app.callback()
def main() -> None:
    """Multi-speaker ASR command-line interface."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def probe(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    stream_index: Annotated[int, typer.Option(min=0, help="Audio stream index.")] = 0,
) -> None:
    """Print the audio metadata (ProbeInfo) of FILE as JSON."""
    from asr.audio import decode

    try:
        info = decode.probe(file, stream_index)
    except (ValueError, OSError) as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from None
    typer.echo(info.model_dump_json(indent=2))


@app.command()
def transcribe(
    file: Annotated[str, typer.Argument(help="Audio file path, or a public http(s) URL.")],
    provider: Annotated[Provider, typer.Option(help="Transcription provider.")] = Provider.cloud,
    num_speakers: Annotated[
        int | None, typer.Option(min=1, max=20, help="Expected speakers (default: auto).")
    ] = None,
    terms: Annotated[
        list[str] | None,
        typer.Option("--terms", "--term", help="Hotword as term[:weight]; repeatable."),
    ] = None,
    language: Annotated[
        list[str] | None, typer.Option(help="Language hint; repeatable (default: zh, en).")
    ] = None,
    context: Annotated[str | None, typer.Option(help="Background text (≤ 2000 chars).")] = None,
    channels: Annotated[Channels, typer.Option(help="Channel handling.")] = Channels.auto,
    keep_dialect: Annotated[bool, typer.Option(help="Keep dialect wording.")] = False,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f")] = OutputFormat.txt,
    output: Annotated[Path | None, typer.Option("--output", "-o", dir_okay=False)] = None,
    asr_model: Annotated[
        str | None,
        typer.Option(help="Local ASR model key (default: M4 choice for this hardware)."),
    ] = None,
    device: Annotated[str, typer.Option(help="Local inference device: cpu or cuda.")] = "cpu",
) -> None:
    """Transcribe FILE and print (or write) the transcript."""
    from asr import writers

    is_url = file.startswith(("http://", "https://"))
    if not is_url and not Path(file).is_file():
        err.print(f"[red]error:[/red] not a file: {file}")
        raise typer.Exit(2)

    if provider is Provider.local:
        build = _local_builder(is_url, asr_model, device)
    else:
        build = _cloud_builder()

    try:
        options = JobOptions(
            provider=provider.value,
            num_speakers=num_speakers,
            terms=[parse_term(t) for t in terms or []],
            language_hints=language if language else ["zh", "en"],
            context=context,
            preprocess=PreprocessOptions(channels=channels.value),
        )
        options.cloud.keep_dialect = keep_dialect
    except ValidationError as exc:
        err.print(f"[red]error:[/red] invalid options: {exc.errors()[0].get('msg')}")
        raise typer.Exit(2) from None
    source = AudioSource(url=file) if is_url else AudioSource(path=Path(file))

    with tempfile.TemporaryDirectory(prefix="asr-cli-") as work_dir:
        try:
            chosen = build(Path(work_dir))
            with Progress(
                TextColumn("{task.description:<14}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=err,
                transient=True,
            ) as bar:
                task = bar.add_task("queued", total=1.0)

                def on_progress(event: ProgressEvent) -> None:
                    bar.update(task, description=event.stage, completed=event.progress)
                    if event.external_id:
                        bar.console.print(f"DashScope task: {event.external_id}")

                transcript = chosen.transcribe(source, options, on_progress)
        except ProviderError as exc:
            err.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(1) from None
        except (JobCancelled, KeyboardInterrupt):
            err.print("[yellow]cancelled[/yellow]")
            raise typer.Exit(130) from None

    for warning in transcript.warnings:
        err.print(f"[yellow]warning:[/yellow] {warning}")
    rendered = writers.write(transcript, fmt.value)
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
        err.print(
            f"Wrote {output} ({len(transcript.segments)} segments, "
            f"{len(transcript.speakers)} speakers)"
        )


def _cloud_builder() -> Callable[[Path], TranscriptionProvider]:
    from asr.providers import get_provider
    from asr.providers.cloud import DEFAULT_MODEL

    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        err.print("[red]error:[/red] DASHSCOPE_API_KEY is not set")
        raise typer.Exit(2)
    endpoint = os.environ.get("DASHSCOPE_ENDPOINT", "").strip() or DEFAULT_ENDPOINT
    model = os.environ.get("ASR_CLOUD_MODEL", "").strip() or DEFAULT_MODEL

    def build(work_dir: Path) -> TranscriptionProvider:
        return get_provider(
            "cloud", api_key=api_key, endpoint=endpoint, model=model, work_dir=work_dir
        )

    return build


def _local_builder(
    is_url: bool, asr_model: str | None, device: str
) -> Callable[[Path], TranscriptionProvider]:
    from asr.providers import get_provider
    from asr.providers.local import models

    if is_url:
        err.print("[red]error:[/red] the local provider needs a file, not a URL")
        raise typer.Exit(2)
    if device not in ("cpu", "cuda"):
        err.print("[red]error:[/red] --device must be cpu or cuda")
        raise typer.Exit(2)
    if not models.local_stack_installed():
        err.print(
            "[red]error:[/red] the local model stack is not installed "
            "(uv sync --extra cpu --extra local)"
        )
        raise typer.Exit(2)
    from asr.providers.local.pipeline import default_asr_model

    cache = _model_cache_dir()
    chosen = asr_model or default_asr_model(device)
    try:
        missing = models.missing_models(cache, chosen)
    except KeyError as exc:
        err.print(f"[red]error:[/red] {exc.args[0]}")
        raise typer.Exit(2) from None
    if missing:
        err.print(
            f"[red]error:[/red] models not downloaded: {', '.join(missing)}\n"
            f"run: asr models download {' '.join(missing)}"
        )
        raise typer.Exit(2)
    err.print(f"local ASR model: {chosen} ({device})")

    def build(work_dir: Path) -> TranscriptionProvider:
        del work_dir
        return get_provider("local", cache_dir=cache, asr_model=chosen, device=device)

    return build


# ------------------------------------------------------------------------------ local models
models_app = typer.Typer(help="Local model cache (explicit downloads only).", no_args_is_help=True)
app.add_typer(models_app, name="models")


def _model_cache_dir() -> Path:
    return Path(os.environ.get("ASR_MODEL_CACHE_DIR", "").strip() or "./models").resolve()


@models_app.command("list")
def models_list() -> None:
    """Show known models and whether they are in ASR_MODEL_CACHE_DIR."""
    from asr.providers.local import models

    cache = _model_cache_dir()
    for key, spec in models.MODELS.items():
        state = "downloaded" if models.is_downloaded(cache, key) else "-"
        typer.echo(f"{key:18} {spec.kind:6} {state:10} {spec.hub}:{spec.repo_id}")
    err.print(f"cache: {cache}")


@models_app.command("download")
def models_download(
    keys: Annotated[list[str], typer.Argument(help="Model keys from `asr models list`.")],
    mirror: Annotated[
        bool,
        typer.Option("--mirror", help="Fetch Hugging Face models from their ModelScope mirror."),
    ] = False,
) -> None:
    """Download models into ASR_MODEL_CACHE_DIR (the only way models are fetched)."""
    from asr.providers.local import models

    cache = _model_cache_dir()
    for key in keys:
        try:
            models.spec(key)
        except KeyError as exc:
            err.print(f"[red]error:[/red] {exc.args[0]}")
            raise typer.Exit(2) from None
    for key in keys:
        if models.is_downloaded(cache, key):
            err.print(f"{key}: already downloaded")
            continue
        err.print(f"{key}: downloading {models.spec(key).repo_id} ...")
        path = models.download(cache, key, mirror=mirror and models.spec(key).mirror is not None)
        err.print(f"{key}: {path}")


if __name__ == "__main__":
    app()
