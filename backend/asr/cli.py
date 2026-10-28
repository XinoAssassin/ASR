"""``asr`` command-line interface. Owned by the ``cloud-provider`` agent (M2).

Cloud credentials come from the environment only: ``DASHSCOPE_API_KEY`` (required),
``DASHSCOPE_ENDPOINT`` (default ``https://dashscope.aliyuncs.com``) and ``ASR_CLOUD_MODEL``
(default ``qwen-audio-3.1-asr-flash-filetrans``).
"""

from __future__ import annotations

import os
import sys
import tempfile
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
) -> None:
    """Transcribe FILE and print (or write) the transcript."""
    from asr import writers
    from asr.providers import get_provider

    if provider is Provider.local:
        err.print(
            "[red]error:[/red] Local provider is not available until M5; use --provider cloud"
        )
        raise typer.Exit(2)

    is_url = file.startswith(("http://", "https://"))
    if not is_url and not Path(file).is_file():
        err.print(f"[red]error:[/red] not a file: {file}")
        raise typer.Exit(2)

    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        err.print("[red]error:[/red] DASHSCOPE_API_KEY is not set")
        raise typer.Exit(2)
    endpoint = os.environ.get("DASHSCOPE_ENDPOINT", "").strip() or DEFAULT_ENDPOINT
    from asr.providers.cloud import DEFAULT_MODEL

    model = os.environ.get("ASR_CLOUD_MODEL", "").strip() or DEFAULT_MODEL

    try:
        options = JobOptions(
            provider="cloud",
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
            cloud = get_provider(
                "cloud", api_key=api_key, endpoint=endpoint, model=model, work_dir=Path(work_dir)
            )
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

                transcript = cloud.transcribe(source, options, on_progress)
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


if __name__ == "__main__":
    app()
