"""Celery Beat scheduling for the polling agent.

`celery -A app.workers.tasks.celery beat --loglevel=info` runs the schedule
defined in `beat.py`. The schedule is attached to the celery app at import
time in app/workers/tasks.py so a single `Celery(...)` instance is shared
between the worker and Beat.
"""

from app.scheduling.beat import (
    BEAT_SCHEDULE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    POLL_TASK_NAME,
)

__all__ = [
    "BEAT_SCHEDULE",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "POLL_TASK_NAME",
]
