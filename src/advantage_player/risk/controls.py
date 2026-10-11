"""Human controls (Telegram / dashboard). Rule 9: they can only REDUCE risk.

There is deliberately no method that increases risk. `lower()` refuses any change the
pre-commitment classifier deems risk-increasing; `set_risk_scale()` only goes down.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from advantage_player.config.changes import apply_leaves, flatten, is_increase
from advantage_player.config.settings import AppConfig


class ControlTarget(Protocol):
    cfg: AppConfig
    risk_scale: float
    overrides: dict[str, Any]

    def pause(self) -> None: ...
    def resume(self) -> str: ...
    async def close(self, symbol: str | None) -> str: ...
    async def kill(self, reason: str) -> None: ...
    def apply_effective(self, cfg: AppConfig, note: str) -> None: ...


class Controls:
    def __init__(self, target: ControlTarget, audit: Callable[[str], Awaitable[None] | None]):
        self.t = target
        self.audit = audit

    async def _log(self, msg: str) -> None:
        r = self.audit(msg)
        if r is not None:
            await r

    async def pause(self) -> str:
        self.t.pause()
        await self._log("control: pause")
        return "paused (no new entries; open positions keep their SL/TP)"

    async def resume(self) -> str:
        res = self.t.resume()
        await self._log(f"control: resume → {res}")
        return res

    async def close(self, symbol: str | None) -> str:
        res = await self.t.close(symbol)
        await self._log(f"control: close {symbol or 'all'} → {res}")
        return res

    async def kill(self) -> str:
        await self.t.kill("manual /kill")
        return "KILLED: orders cancelled, positions flattened, trading halted"

    async def set_risk_scale(self, scale: float) -> str:
        if not 0 <= scale <= self.t.risk_scale:
            return f"refused: risk scale can only be lowered (current {self.t.risk_scale:g})"
        self.t.risk_scale = scale
        await self._log(f"control: risk scale → {scale:g}")
        return f"risk scale set to {scale:g}"

    async def lower(self, path: str, value: Any) -> str:
        cur = flatten(self.t.cfg.canonical())
        if path not in cur:
            return f"refused: unknown setting {path}"
        old = cur[path]
        try:
            value = type(old)(value)
        except (TypeError, ValueError):
            return f"refused: bad value {value!r}"
        if is_increase(path, old, value):
            return f"refused: {path} {old} → {value} would increase risk"
        try:
            new = apply_leaves(self.t.cfg, {path: value})
        except Exception as e:
            return f"refused: invalid ({e})"
        self.t.overrides[path] = value
        self.t.apply_effective(new, f"control lower {path}={value}")
        await self._log(f"control: lower {path} {old} → {value}")
        return f"{path}: {old} → {value}"

    def view(self) -> dict[str, Any]:
        r = self.t.cfg.risk
        return {
            "risk_scale": self.t.risk_scale,
            "kelly_fraction": r.kelly_fraction,
            "max_risk_per_trade": r.max_risk_per_trade,
            "probe_risk": r.probe_risk,
            "max_total_open_risk": r.max_total_open_risk,
            "max_positions": r.max_positions,
            "target_leverage": self.t.cfg.target_leverage,
            "overrides": dict(self.t.overrides),
        }
