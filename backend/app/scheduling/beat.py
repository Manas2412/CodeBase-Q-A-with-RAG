"""Celery Beat schedule.

Beat is the scheduler process. It periodically enqueues
`poll_all_projects_task` onto the same Redis broker the worker drains.

Schedule
========

Default: every 300 seconds (5 minutes). Configurable via
`POLL_INTERVAL_SECONDS` env so ops can tune for a noisy / quiet day
without a code change.

`expires` is set to half the interval (min 5s). If a tick goes stale —
because the worker was down, slow, or restarted — the queued task drops
on the floor rather than running late and racing the next tick. The
"half" comes from the observation that two consecutive polls running
concurrently against the same project both pass the `status='done'`
idempotency guard (neither has finished yet) and double-spend on
Bedrock. Half the interval guarantees at most one tick in flight.

Originally `expires` was `interval - 30` which gave ~2 ticks of slack at
fast cadences (e.g. 15s interval → 30s expires → 2 ticks queued).
A worker restart in mid-cycle dequeued the backlog and ran the same
poll concurrently, billing Bedrock twice. Half-interval kills the burst.
"""

from __future__ import annotations

import os

#: Fully-qualified celery task name. Kept as a constant so Beat config
#: and worker registration agree.
POLL_TASK_NAME: str = "app.workers.tasks.poll_all_projects_task"

#: Default cadence in seconds. 5 min is the Phase 1 cadence (Plan v3.3 §4.2).
DEFAULT_POLL_INTERVAL_SECONDS: int = int(
    os.getenv("POLL_INTERVAL_SECONDS", "300")
)

#: Hard floor on the expires window — anything shorter than this risks
#: dropping a task purely because of normal Redis round-trip jitter.
MIN_EXPIRES_SECONDS: int = 5


def _expires_for(interval: int) -> int:
    """Half the interval, min 5s. Keeps at most one tick in flight."""
    return max(interval // 2, MIN_EXPIRES_SECONDS)


BEAT_SCHEDULE: dict = {
    "poll-all-projects": {
        "task": POLL_TASK_NAME,
        "schedule": DEFAULT_POLL_INTERVAL_SECONDS,
        # Tight expires → stale ticks die before the next one fires.
        # Prevents the "Beat catch-up burst" failure mode where N stale
        # polls dequeue at once after a worker restart.
        "options": {"expires": _expires_for(DEFAULT_POLL_INTERVAL_SECONDS)},
    },
}
