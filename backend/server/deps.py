"""FastAPI dependencies. Frozen contract.

Everything request-scoped is read from ``app.state``, which ``server.app.create_app`` populates:
``env`` at construction, ``db`` and ``job_manager`` in the lifespan.
"""

from typing import Annotated

from fastapi import Depends, Request

from server.config import EnvSettings
from server.db import Database
from server.jobs import JobManager


def get_env(request: Request) -> EnvSettings:
    env: EnvSettings = request.app.state.env
    return env


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_job_manager(request: Request) -> JobManager:
    manager: JobManager = request.app.state.job_manager
    return manager


EnvDep = Annotated[EnvSettings, Depends(get_env)]
DbDep = Annotated[Database, Depends(get_db)]
JobManagerDep = Annotated[JobManager, Depends(get_job_manager)]
