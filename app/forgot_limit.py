from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class ForgotLimiter:
    """Rate-limit password-reset requests by IP and login identifier."""

    def __init__(self, *, per_ip: int = 5, per_login: int = 3, window_s: float = 3600.0) -> None:
        self.per_ip = per_ip
        self.per_login = per_login
        self.window_s = window_s
        self._lock = threading.Lock()
        self._ip: dict[str, deque[float]] = defaultdict(deque)
        self._login: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, q: deque[float], now: float) -> None:
        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()

    def allow(self, *, ip: str, login: str) -> tuple[bool, str]:
        now = time.time()
        ip_key = (ip or "unknown").strip() or "unknown"
        login_key = (login or "").strip().lower() or "unknown"
        with self._lock:
            iq = self._ip[ip_key]
            lq = self._login[login_key]
            self._prune(iq, now)
            self._prune(lq, now)
            if len(iq) >= self.per_ip:
                return False, "Too many reset requests from this IP. Try again later."
            if len(lq) >= self.per_login:
                return False, "Too many reset requests for this account. Try again later."
            iq.append(now)
            lq.append(now)
            return True, ""


forgot_limiter = ForgotLimiter()
