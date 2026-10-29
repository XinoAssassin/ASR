"""FastAPI dependencies. Frozen contract.

Everything request-scoped is read from ``app.state``, which ``server.app.create_app`` populates:
``env`` and ``model_downloads`` at construction, ``db`` and ``job_manager`` in the lifespan.
"""

from typing import Annotated

from fastapi import Depends, Request

from server.config import EnvSettings
from server.db import Database
from server.jobs import JobManager
from server.model_downloads import ModelDownloads


def get_env(request: Request) -> EnvSettings:
    env: EnvSettings = request.app.state.env
    return env


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_job_manager(request: Request) -> JobManager:
    manager: JobManager = request.app.state.job_manager
    return manager


def get_model_downloads(request: Request) -> ModelDownloads:
    downloads: ModelDownloads = request.app.state.model_downloads
    return downloads


EnvDep = Annotated[EnvSettings, Depends(get_env)]
ModelDownloadsDep = Annotated[ModelDownloads, Depends(get_model_downloads)]
DbDep = Annotated[Database, Depends(get_db)]
JobManagerDep = Annotated[JobManager, Depends(get_job_manager)]
