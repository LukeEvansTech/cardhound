"""In-process scheduler: one daemon thread that starts a sweep at each cron time."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo

from croniter import croniter

log = logging.getLogger(__name__)


def next_time(expr: str, tz: str, now: dt.datetime | None = None) -> dt.datetime:
    base = now or dt.datetime.now(ZoneInfo(tz))
    return croniter(expr, base).get_next(dt.datetime)


def start(expr: str, tz: str, job) -> threading.Thread:
    if not croniter.is_valid(expr):
        raise ValueError(f"invalid CARDHOUND_SCHEDULE: {expr!r}")

    def loop() -> None:
        while True:
            at = next_time(expr, tz)
            log.info("next sweep at %s", at.isoformat())
            while (wait := (at - dt.datetime.now(ZoneInfo(tz))).total_seconds()) > 0:
                time.sleep(min(wait, 60))
            try:
                job()
            except Exception:  # pylint: disable=broad-exception-caught
                log.exception("scheduled sweep crashed")

    t = threading.Thread(target=loop, name="scheduler", daemon=True)
    t.start()
    return t
