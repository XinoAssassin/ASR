"""``asr`` console script: the core CLI (``asr.cli``) plus ``asr serve``.

``serve`` lives here, not in ``asr.cli``, because the core library stays Web-free (AGENTS.md
rule 5); this module extends the core Typer app instead.
"""

from __future__ import annotations

from typing import Annotated

import typer

from asr.cli import STARTUP_HOOKS, app, err

__all__ = ["app"]


def load_dotenv_files() -> None:
    """Give every CLI command the same ``.env`` files as ``asr serve`` (repo root, then
    ``backend/``; real environment variables win), so e.g. ``asr models download`` and the
    server agree on ``ASR_MODEL_CACHE_DIR``. Values are never printed."""
    from server.config import load_env_files_into_environ

    load_env_files_into_environ()


if load_dotenv_files not in STARTUP_HOOKS:
    STARTUP_HOOKS.append(load_dotenv_files)


@app.command()
def serve(
    host: Annotated[
        str, typer.Option(help="Bind address; 0.0.0.0 for LAN access (requires ASR_API_KEY).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    forwarded_allow_ips: Annotated[
        str | None,
        typer.Option(help="Reverse-proxy addresses trusted for X-Forwarded-* (comma-separated)."),
    ] = None,
    reload: Annotated[bool, typer.Option(help="Reload on code changes (development).")] = False,
) -> None:
    """Run the HTTP API (one process: the job queue lives in it)."""
    import uvicorn
    from rich.markup import escape

    from server.auth import is_loopback_address
    from server.config import ConfigError, get_env
    from server.logging_setup import configure_logging

    try:
        env = get_env()
    except ConfigError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from None
    if not is_loopback_address(host) and not env.api_key_required:
        if not env.allow_unauthenticated_network:
            err.print(
                f"[red]error:[/red] refusing to listen on {escape(host)} with an empty "
                "ASR_API_KEY: every client on the network would control the API. Set "
                "ASR_API_KEY, bind 127.0.0.1, or - only when the port is published on loopback "
                "alone (e.g. docker-compose's 127.0.0.1:8000:8000) - set "
                "ASR_ALLOW_UNAUTHENTICATED_NETWORK=1."
            )
            raise typer.Exit(2)
        err.print(
            "[yellow]warning:[/yellow] ASR_API_KEY is empty and "
            "ASR_ALLOW_UNAUTHENTICATED_NETWORK is set: any client that reaches this port "
            "controls the API. Publish it on loopback only, or set ASR_API_KEY."
        )
    configure_logging(env.log_level, env.log_format, env.log_file)
    uvicorn.run(
        "server.app:app",
        host=host,
        port=port,
        reload=reload,
        workers=1,
        log_config=None,  # propagate uvicorn's loggers to configure_logging's handlers
        proxy_headers=forwarded_allow_ips is not None,
        forwarded_allow_ips=forwarded_allow_ips,
        timeout_graceful_shutdown=30,
    )
