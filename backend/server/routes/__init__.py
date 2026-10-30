"""API routers, one module per PLAN §5 area."""

from fastapi import APIRouter

from server.routes import (
    audio,
    fs,
    health,
    jobs,
    settings,
    siyuan,
    system,
    term_sets,
    transcripts,
    uploads,
)

ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    uploads.router,
    fs.router,
    audio.router,
    jobs.router,
    transcripts.router,
    term_sets.router,
    settings.router,
    siyuan.router,
    system.router,
)
