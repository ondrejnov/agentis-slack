from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class EventDeduper:
    ttl_seconds: int = 600
    seen: dict[str, float] = field(default_factory=dict)

    def seen_before(self, event_id: str) -> bool:
        now = time.monotonic()
        expired = [key for key, deadline in self.seen.items() if deadline <= now]
        for key in expired:
            self.seen.pop(key, None)
        if event_id in self.seen:
            return True
        self.seen[event_id] = now + self.ttl_seconds
        return False


@dataclass
class GlobalRateLimiter:
    max_events: int
    window_seconds: int
    timestamps: deque[float] = field(default_factory=deque)

    def allow(self) -> bool:
        now = time.monotonic()
        while self.timestamps and self.timestamps[0] <= now - self.window_seconds:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.max_events:
            return False
        self.timestamps.append(now)
        return True


def should_ignore_event(event: dict, *, bot_user_id: str | None = None) -> bool:
    if event.get("subtype"):
        return True
    if event.get("bot_id"):
        return True
    if bot_user_id and event.get("user") == bot_user_id:
        return True
    return False
