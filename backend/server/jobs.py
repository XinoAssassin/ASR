"""Job queue and runner. Owned by ``backend-jobs`` (M1).

Providers are synchronous and run in worker threads; local jobs use a single worker, cloud jobs
run concurrently (PLAN §4.7). Jobs left ``running`` at startup are marked failed (D13).
"""

from server.config import EnvSettings
from server.db import Database


class JobManager:
    def __init__(self, *, db: Database, env: EnvSettings) -> None:
        self.db = db
        self.env = env

    async def start(self) -> None:
        """Called once from the app lifespan after the database is open."""

    async def stop(self) -> None:
        """Called once from the app lifespan before the database closes."""
