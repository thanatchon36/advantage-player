"""Tracks request-weight and order-count usage from Binance response headers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_HDR = re.compile(r"^x-mbx-(used-weight|order-count)-(\d+)([smhd])$", re.IGNORECASE)
_UNIT = {"s": "SECOND", "m": "MINUTE", "h": "HOUR", "d": "DAY"}


@dataclass
class RateLimiter:
    # (kind, interval_key) -> limit, e.g. ("REQUEST_WEIGHT", "1MINUTE") -> 6000
    limits: dict[tuple[str, str], int] = field(
        default_factory=lambda: {
            ("REQUEST_WEIGHT", "1MINUTE"): 2400,
            ("ORDERS", "1MINUTE"): 1200,
            ("ORDERS", "10SECOND"): 300,
        }
    )
    used: dict[tuple[str, str], int] = field(default_factory=dict)

    def set_limits(self, rate_limits: list[Mapping[str, object]]) -> None:
        """Load limits from exchangeInfo['rateLimits']."""
        for rl in rate_limits:
            key = (str(rl["rateLimitType"]), f"{rl['intervalNum']}{rl['interval']}")
            self.limits[key] = int(str(rl["limit"]))

    def update(self, headers: Mapping[str, str]) -> None:
        for name, value in headers.items():
            m = _HDR.match(name)
            if not m:
                continue
            kind = "REQUEST_WEIGHT" if m.group(1).lower() == "used-weight" else "ORDERS"
            key = (kind, f"{m.group(2)}{_UNIT[m.group(3).lower()]}")
            self.used[key] = int(value)

    def headroom(self) -> float:
        """Smallest remaining fraction across all tracked limits (1.0 = untouched)."""
        fracs = [
            1 - self.used[k] / lim for k, lim in self.limits.items() if k in self.used and lim > 0
        ]
        return min(fracs, default=1.0)
