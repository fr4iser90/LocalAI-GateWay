"""In-memory RPM + concurrency + daily counters (key / user / team / model)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timezone


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


@dataclass
class LimitDecision:
    allowed: bool
    reason: str = ""
    remaining_rpm: int | None = None
    retry_after: int | None = None


class RateLimiter:
    """In-memory RPM + concurrency + daily counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._inflight: dict[str, int] = defaultdict(int)
        self._daily: dict[str, tuple[date, int]] = {}

    def _rpm_check(
        self, bucket: str, rpm: int | None, now: float
    ) -> tuple[bool, int | None, int | None]:
        if rpm is None or rpm <= 0:
            return True, None, None
        q = self._windows[bucket]
        cutoff = now - 60.0
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= rpm:
            return False, 0, max(1, int(60 - (now - q[0])))
        return True, rpm - len(q) - 1, None

    def _daily_get(self, bucket: str, today: date) -> int:
        day, count = self._daily.get(bucket, (today, 0))
        if day != today:
            self._daily[bucket] = (today, 0)
            return 0
        return count

    def _daily_inc(self, bucket: str, today: date) -> int:
        count = self._daily_get(bucket, today) + 1
        self._daily[bucket] = (today, count)
        return count

    def daily_count(self, bucket: str) -> int:
        with self._lock:
            return self._daily_get(bucket, _utc_today())

    def check_and_acquire(
        self,
        *,
        key_id: int,
        team_id: int | None,
        rpm: int | None,
        concurrency: int | None,
        model: str | None = None,
        model_rpm: int | None = None,
        model_concurrency: int | None = None,
        key_daily_quota: int | None = None,
        team_daily_quota: int | None = None,
        model_daily_quota: int | None = None,
        user_id: int | None = None,
        user_rpm: int | None = None,
        user_concurrency: int | None = None,
        user_daily_quota: int | None = None,
    ) -> LimitDecision:
        now = time.time()
        today = _utc_today()
        key_bucket = f"key:{key_id}"
        team_bucket = f"team:{team_id}" if team_id else None
        user_bucket = f"user:{user_id}" if user_id else None
        model_bucket = f"key:{key_id}:model:{model}" if model else None

        with self._lock:
            if key_daily_quota and key_daily_quota > 0:
                if self._daily_get(key_bucket, today) >= key_daily_quota:
                    return LimitDecision(
                        False, "key_daily_quota_exceeded", retry_after=60
                    )
            if user_bucket and user_daily_quota and user_daily_quota > 0:
                if self._daily_get(user_bucket, today) >= user_daily_quota:
                    return LimitDecision(
                        False, "user_daily_quota_exceeded", retry_after=60
                    )
            if team_bucket and team_daily_quota and team_daily_quota > 0:
                if self._daily_get(team_bucket, today) >= team_daily_quota:
                    return LimitDecision(
                        False, "team_daily_quota_exceeded", retry_after=60
                    )
            if model_bucket and model_daily_quota and model_daily_quota > 0:
                if self._daily_get(model_bucket, today) >= model_daily_quota:
                    return LimitDecision(
                        False, "model_daily_quota_exceeded", retry_after=60
                    )

            ok, remaining, retry = self._rpm_check(key_bucket, rpm, now)
            if not ok:
                return LimitDecision(False, "rpm_exceeded", remaining, retry)

            if user_bucket:
                ok_u, rem_u, retry_u = self._rpm_check(user_bucket, user_rpm, now)
                if not ok_u:
                    return LimitDecision(False, "user_rpm_exceeded", rem_u, retry_u)
                if rem_u is not None and (remaining is None or rem_u < remaining):
                    remaining = rem_u

            if model_bucket:
                ok_m, rem_m, retry_m = self._rpm_check(model_bucket, model_rpm, now)
                if not ok_m:
                    return LimitDecision(False, "model_rpm_exceeded", rem_m, retry_m)
                if rem_m is not None and (remaining is None or rem_m < remaining):
                    remaining = rem_m

            if concurrency is not None and concurrency > 0:
                if self._inflight[key_bucket] >= concurrency:
                    return LimitDecision(False, "concurrency_exceeded", remaining, 1)
            if user_bucket and user_concurrency and user_concurrency > 0:
                if self._inflight[user_bucket] >= user_concurrency:
                    return LimitDecision(
                        False, "user_concurrency_exceeded", remaining, 1
                    )
            if model_bucket and model_concurrency and model_concurrency > 0:
                if self._inflight[model_bucket] >= model_concurrency:
                    return LimitDecision(
                        False, "model_concurrency_exceeded", remaining, 1
                    )

            if rpm is not None and rpm > 0:
                self._windows[key_bucket].append(now)
            if user_bucket and user_rpm is not None and user_rpm > 0:
                self._windows[user_bucket].append(now)
            if model_bucket and model_rpm is not None and model_rpm > 0:
                self._windows[model_bucket].append(now)
            if concurrency is not None and concurrency > 0:
                self._inflight[key_bucket] += 1
            if user_bucket and user_concurrency and user_concurrency > 0:
                self._inflight[user_bucket] += 1
            if model_bucket and model_concurrency and model_concurrency > 0:
                self._inflight[model_bucket] += 1

            self._daily_inc(key_bucket, today)
            if user_bucket:
                self._daily_inc(user_bucket, today)
            if team_bucket:
                self._daily_inc(team_bucket, today)
            if model_bucket:
                self._daily_inc(model_bucket, today)

            return LimitDecision(True, remaining_rpm=remaining)

    def release(
        self,
        key_id: int,
        model: str | None = None,
        *,
        user_id: int | None = None,
    ) -> None:
        key_bucket = f"key:{key_id}"
        model_bucket = f"key:{key_id}:model:{model}" if model else None
        user_bucket = f"user:{user_id}" if user_id else None
        with self._lock:
            if self._inflight[key_bucket] > 0:
                self._inflight[key_bucket] -= 1
            if user_bucket and self._inflight[user_bucket] > 0:
                self._inflight[user_bucket] -= 1
            if model_bucket and self._inflight[model_bucket] > 0:
                self._inflight[model_bucket] -= 1

    def quota_usage_pct(
        self,
        *,
        key_id: int,
        team_id: int | None,
        key_q: int | None,
        team_q: int | None,
        user_id: int | None = None,
        user_q: int | None = None,
    ) -> float | None:
        today = _utc_today()
        with self._lock:
            pcts = []
            if key_q and key_q > 0:
                pcts.append(100.0 * self._daily_get(f"key:{key_id}", today) / key_q)
            if user_id and user_q and user_q > 0:
                pcts.append(100.0 * self._daily_get(f"user:{user_id}", today) / user_q)
            if team_id and team_q and team_q > 0:
                pcts.append(100.0 * self._daily_get(f"team:{team_id}", today) / team_q)
            return max(pcts) if pcts else None


rate_limiter = RateLimiter()
