"""``asr`` command-line interface. Owned by the ``cloud-provider`` agent (M2)."""

import typer

from asr import __version__

app = typer.Typer(help="Multi-speaker ASR command-line interface.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Multi-speaker ASR command-line interface."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
