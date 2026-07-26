from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass
class GroupState:
    group: str
    limit_total: int      # parsed from "150/15m" → 150
    limit_window: str     # "15m"
    remaining: int
    used_last: int
    last_updated: datetime


@dataclass
class RequestLogEntry:
    path: str
    status_code: int
    group: Optional[str]
    tokens_used: int
    timestamp: datetime


@dataclass
class LegacyErrorState:
    remaining: int        # 0–100
    reset_in_seconds: int
    last_updated: datetime


REJECTION_WINDOW_SECS = 300  # 5 minutes


class RateLimitTracker:
    def __init__(self):
        self.groups: dict[str, GroupState] = {}
        self.legacy: Optional[LegacyErrorState] = None
        self.request_log: deque[RequestLogEntry] = deque(maxlen=3000)
        self._warned_groups: set[str] = set()  # prevent DB spam when stuck in warning zone
        # Rolling record of ACTUAL rejections (429 / 420). Drives the banner's
        # critical state — proximity warnings alone stay yellow.
        self.recent_rejections: deque[datetime] = deque(maxlen=500)

    def update_from_response(self, path: str, status_code: int, headers: dict) -> list[dict]:
        """
        Called synchronously after every ESI HTTP response.
        Returns list of events to log (caller dispatches as asyncio.create_task).
        No I/O here — safe to call between await points.
        """
        now = datetime.now(timezone.utc)
        events = []

        group   = headers.get("x-ratelimit-group")
        limit_s = headers.get("x-ratelimit-limit")
        remain  = headers.get("x-ratelimit-remaining")
        used    = headers.get("x-ratelimit-used")
        err_rem = headers.get("x-esi-error-limit-remain")
        err_rst = headers.get("x-esi-error-limit-reset")

        # Token cost (per CCP docs: 2xx=2, 3xx=1, 4xx=5, 5xx=0)
        # 404 from valid endpoints typically costs 1, not 5
        if status_code < 300:
            tokens = int(used) if used else 2
        elif status_code < 400:
            tokens = int(used) if used else 1
        elif status_code < 500:
            tokens = int(used) if used else (1 if status_code == 404 else 5)
        else:
            tokens = 0

        # Update group state + detect threshold crossings
        if group and limit_s and remain is not None:
            total, window = _parse_limit(limit_s)
            new_rem = int(remain)
            if total > 0:
                pct = new_rem / total
                if pct < 0.05 and group + ":critical" not in self._warned_groups:
                    self._warned_groups.add(group + ":critical")
                    events.append({"event_type": "group_critical", "group_name": group,
                                   "path": path, "remaining": new_rem, "limit_str": limit_s})
                elif pct < 0.20 and group + ":warning" not in self._warned_groups:
                    self._warned_groups.add(group + ":warning")
                    events.append({"event_type": "group_warning", "group_name": group,
                                   "path": path, "remaining": new_rem, "limit_str": limit_s})
                if pct >= 0.20:
                    self._warned_groups.discard(group + ":critical")
                    self._warned_groups.discard(group + ":warning")
            self.groups[group] = GroupState(
                group=group, limit_total=total, limit_window=window,
                remaining=new_rem, used_last=tokens, last_updated=now,
            )

        # Legacy error state
        if err_rem is not None:
            self.legacy = LegacyErrorState(
                remaining=int(err_rem),
                reset_in_seconds=int(err_rst) if err_rst else 60,
                last_updated=now,
            )

        # Request log
        self.request_log.append(RequestLogEntry(
            path=path, status_code=status_code,
            group=group, tokens_used=tokens, timestamp=now,
        ))

        # Rolling rejection log — drives the banner's critical state.
        if status_code in (429, 420):
            self.recent_rejections.append(now)

        return events

    def throttle_delay(self) -> float:
        """Seconds to sleep before the next request. 0.0 = no delay."""
        worst = 0.0
        # Check error budget (X-Esi-Error-Limit-Remain) — if near 0, hard throttle
        if self.legacy:
            if self.legacy.remaining <= 0:
                worst = max(worst, float(self.legacy.reset_in_seconds))
            elif self.legacy.remaining < 10:
                worst = max(worst, 2.0)
            elif self.legacy.remaining < 30:
                worst = max(worst, 0.5)
        # Check per-group rate limits
        for g in self.groups.values():
            if g.limit_total == 0:
                continue
            pct = g.remaining / g.limit_total
            if pct < 0.05:
                worst = max(worst, 1.0)
            elif pct < 0.20:
                worst = max(worst, 0.2)
        return worst

    def recent_rejection_count(self, window_seconds: int = REJECTION_WINDOW_SECS) -> int:
        """Number of 429/420 responses in the trailing window. Prunes the
        rolling deque as a side effect."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        while self.recent_rejections and self.recent_rejections[0] < cutoff:
            self.recent_rejections.popleft()
        return len(self.recent_rejections)

    def overall_status(self) -> str:
        """Returns 'ok', 'warning', or 'critical'.

        Critical now requires evidence that we are *actually* being rejected
        (recent 429/420) or that the legacy error budget is fully depleted
        (next ESI error = 420). Proximity alone (remaining/limit < 5%) stays
        in the 'warning' band so the banner doesn't go red pre-emptively.
        """
        if self.recent_rejection_count() > 0:
            return "critical"
        if self.legacy and self.legacy.remaining <= 0:
            return "critical"

        warning = False
        if self.legacy and self.legacy.remaining < 30:
            warning = True
        if any(
            (g.remaining / g.limit_total) < 0.20
            for g in self.groups.values() if g.limit_total > 0
        ):
            warning = True
        return "warning" if warning else "ok"


def _parse_limit(s: str) -> tuple[int, str]:
    try:
        total, window = s.split("/", 1)
        return int(total), window
    except Exception:
        return 0, "?"


async def log_event(event_type: str, path: str, headers: dict, retry_after: int = None):
    """Async DB write for significant events. Called via asyncio.create_task()."""
    from app.db.models import AsyncSessionLocal, ESIRateLimitEvent
    group     = headers.get("x-ratelimit-group")
    if group:
        group = group[:128]  # match DB column length
    remaining = headers.get("x-ratelimit-remaining")
    limit_str = headers.get("x-ratelimit-limit")
    async with AsyncSessionLocal() as db:
        db.add(ESIRateLimitEvent(
            event_type=event_type, group_name=group, path=path,
            remaining=int(remaining) if remaining else None,
            limit_str=limit_str, retry_after=retry_after,
            occurred_at=datetime.now(timezone.utc),
        ))
        await db.commit()


# Module-level singleton
rate_limit_tracker = RateLimitTracker()
