"""Queue + job-status store, both backed by Redis.

RQ owns the work queue; a separate Redis hash per job holds the user-facing
status (state, logs, timestamps) that the API reads. Keeping our own hash —
rather than reading RQ internals — gives the API a stable shape to return.
"""
import time

import redis
from rq import Queue

from .config import settings

_redis = redis.Redis.from_url(settings.redis_url)

# default_timeout must exceed the build timeout so RQ doesn't reap a job that
# is still legitimately building.
queue = Queue("builds", connection=_redis, default_timeout=settings.build_timeout + 120)


def _now() -> str:
    return f"{time.time():.0f}"


class Store:
    """Thin wrapper over a Redis hash per job: key `job:<id>`."""

    @staticmethod
    def _key(job_id: str) -> str:
        return f"job:{job_id}"

    def create(self, job_id: str, **fields) -> None:
        fields.setdefault("status", "queued")
        fields.setdefault("created_at", _now())
        self._write(job_id, fields)
        _redis.expire(self._key(job_id), settings.job_ttl)

    def update(self, job_id: str, **fields) -> None:
        self._write(job_id, fields)

    def get(self, job_id: str):
        data = _redis.hgetall(self._key(job_id))
        if not data:
            return None
        return {k.decode(): v.decode() for k, v in data.items()}

    def _write(self, job_id: str, fields: dict) -> None:
        _redis.hset(self._key(job_id), mapping={k: str(v) for k, v in fields.items()})


store = Store()
