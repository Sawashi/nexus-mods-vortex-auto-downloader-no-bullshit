"""Keeps the tool under Nexus's request limits and remembers its usage between runs.

Nexus limits requests to 20,000 per 24 hours and, once that is used up, 500 per hour. To be safe this tool treats
both as hard limits that apply at the same time, stops at 90 % of each, and counts its own API calls as well as
the requests the Nexus pages make in the tool's browser tab. Windows are rolling (the real daily quota resets at
00:00 GMT, so rolling is the stricter reading).
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .util import write_json

HOUR = 3600.0
DAY = 86400.0
OFFICIAL_HOURLY = 500
OFFICIAL_DAILY = 20_000
SAFE_HOURLY = 450  # 90 % of the official limits
SAFE_DAILY = 18_000


class RequestLimitReached(Exception):
    """A request limit is used up and waiting for it would take too long."""


@dataclass(frozen=True)
class Usage:
    hour_used: int
    hour_limit: int
    day_used: int
    day_limit: int


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = (seconds + 30) // 60
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


class RequestBudget:
    def __init__(
        self,
        path: Path | None = None,
        *,
        hourly: int = SAFE_HOURLY,
        daily: int = SAFE_DAILY,
        clock: Callable[[], float] = time.time,
    ):
        # Never allow more than Nexus itself does, whatever the settings file says.
        self.hourly = max(1, min(int(hourly), OFFICIAL_HOURLY))
        self.daily = max(1, min(int(daily), OFFICIAL_DAILY))
        self._path = path
        self._clock = clock
        self._entries: deque[list[float]] = deque()  # [timestamp, count], oldest first
        self._lock = threading.Lock()
        self._dirty = False
        self._flushed_at = 0.0
        self._load()

    # -- counting ------------------------------------------------------------------------------

    def record(self, count: int = 1) -> None:
        now = self._clock()
        with self._lock:
            if self._entries and int(self._entries[-1][0]) == int(now):
                self._entries[-1][1] += count
            else:
                self._entries.append([now, count])
            self._prune(now)
            self._dirty = True
        if now - self._flushed_at >= 2:
            self.flush()

    def usage(self) -> Usage:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return Usage(self._used(HOUR, now), self.hourly, self._used(DAY, now), self.daily)

    def _prune(self, now: float) -> None:
        while self._entries and self._entries[0][0] <= now - DAY:
            self._entries.popleft()

    def _used(self, window: float, now: float) -> int:
        return int(sum(count for stamp, count in self._entries if stamp > now - window))

    # -- waiting -------------------------------------------------------------------------------

    def wait_time(self, cost: int = 1) -> tuple[float, str]:
        """Seconds until `cost` more requests fit under both limits (0: right now), and the limit in the way."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            longest, limiting = 0.0, ""
            for name, window, limit in (("hourly", HOUR, self.hourly), ("daily", DAY, self.daily)):
                excess = self._used(window, now) + cost - limit
                if excess <= 0:
                    continue
                freed = 0
                for stamp, count in self._entries:
                    if stamp <= now - window:
                        continue
                    freed += count
                    if freed >= excess:  # these requests have aged out of the window by then
                        delay = stamp + window - now
                        break
                else:
                    delay = window  # `cost` alone exceeds the limit: wait out the whole window
                if delay > longest:
                    longest, limiting = delay, name
            return max(0.0, longest), limiting

    def wait_for(
        self,
        cost: int,
        sleep: Callable[[float], None],
        on_wait: Callable[[float, str], None] | None = None,
        max_wait: float = HOUR + 300,
    ) -> None:
        """Block (in short, interruptible sleeps) until `cost` requests fit; give up if that takes too long."""
        while True:
            delay, limiting = self.wait_time(cost)
            if delay <= 0:
                return
            if delay > max_wait:
                raise RequestLimitReached(
                    f"the {limiting} request budget ({self.daily if limiting == 'daily' else self.hourly:,} "
                    f"per {'24 hours' if limiting == 'daily' else 'hour'}) is used up and frees up again in about "
                    f"{format_duration(delay)}. Run the tool again later: finished files are remembered."
                )
            if on_wait:
                on_wait(delay, limiting)
            sleep(min(delay + 0.5, 5.0))

    # -- persistence ---------------------------------------------------------------------------

    def flush(self) -> None:
        with self._lock:
            if not self._path or not self._dirty:
                return
            snapshot = [list(entry) for entry in self._entries]
            self._dirty = False
        self._flushed_at = self._clock()
        try:
            write_json(self._path, {"entries": snapshot})
        except OSError:
            pass

    def _load(self) -> None:
        if not self._path:
            return
        try:
            entries = json.loads(self._path.read_text(encoding="utf-8"))["entries"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        now = self._clock()
        for entry in entries if isinstance(entries, list) else []:
            try:
                stamp, count = float(entry[0]), int(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            if stamp > now - DAY and count > 0:
                self._entries.append([stamp, count])
        self._entries = deque(sorted(self._entries))
