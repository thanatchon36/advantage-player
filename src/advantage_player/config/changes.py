"""Config pre-commitment (SPEC §7.8).

Risk-INCREASING changes are queued and apply only after 24 h, and are rejected while drawdown
> 5 % (checked when queued and again when due). Risk-DECREASING changes apply immediately.
Every leaf has a known risk direction; anything unknown is treated as risk-increasing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from advantage_player.config.settings import AppConfig

HOUR_MS = 3_600_000

# +1: a higher value is riskier; −1: a lower value is riskier; 0: no risk impact.
DIRECTION: dict[str, int] = {
    "target_leverage": 1,
    "risk.kelly_fraction": 1,
    "risk.max_risk_per_trade": 1,
    "risk.probe_risk": 1,
    "risk.dd_half_at": 1,
    "risk.dd_halt_at": 1,
    "risk.liq_stop_fraction": 1,
    "risk.fee_buffer": -1,
    "risk.max_margin_frac": 1,
    "risk.max_total_open_risk": 1,
    "risk.max_cluster_risk": 1,
    "risk.max_net_direction_risk": 1,
    "risk.max_positions": 1,
    "risk.daily_loss_limit": 1,
    "risk.sweep_trigger": 1,
    "risk.auto_sweep": 1,  # needs an extra API permission → treat as increase
    "universe.min_quote_volume_usd": -1,
    "universe.min_depth_usd_10bps": -1,
    "universe.max_tick_bps": 1,
    "universe.min_move_to_cost": -1,
    "universe.max_abs_funding": 1,
    "universe.anomaly_lookback_min": -1,
    "universe.open_tables": 1,
    "universe.min_listing_days": -1,
    "universe.slippage_model_bps": -1,
    "universe.cluster_corr": 1,
    "universe.watchlist": 0,
    "universe.scan_interval_s": 0,
    "universe.table_change_debounce_s": 0,
    "regime.allow_high_vol": 1,
    "regime.high_vol_pctile": 1,
    "ledger.confidence": -1,
    "ledger.bh_q": 1,
    "ledger.shadow_min_n": -1,
    "ledger.probe_min_n": -1,
    "ledger.probe_max_slippage_ratio": 1,
    "ledger.suspend_slippage_ratio": 1,
    "ledger.suspend_fee_share": 1,
    "ledger.rolling_window": 1,
    "ledger.cusum_k": 1,
    "ledger.cusum_h": 1,
    "ledger.suspend_cooldown_h": -1,
    "ledger.bootstrap_samples": 0,
    "ledger.eval_interval_s": 0,
    "ledger.research_interval_h": 0,
    "discipline.sl_confirm_timeout_s": 1,
    "discipline.symbol_lock_after_stop_min": -1,
    "discipline.loss_streak_pause_n": 1,
    "discipline.loss_streak_pause_min": -1,
    "discipline.kill_resume_min_hours": -1,
    "discipline.config_increase_delay_h": -1,
    "discipline.config_reject_dd": 1,
    "discipline.max_spread_bps": 1,
    "discipline.funding_blackout_min": -1,
    "discipline.max_clock_drift_ms": 1,
    "discipline.min_rate_headroom": -1,
    "discipline.stale_data_s": 1,
    "execution.max_taker_slippage_bps": 1,
    "execution.reconcile_interval_s": 1,
    "execution.recv_window_ms": 1,
    "storage.url": 0,
    "storage.data_dir": 0,
}


def flatten(d: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def unflatten(flat: Mapping[str, Any]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path, v in flat.items():
        cur = root
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return root


def _direction(path: str) -> int | None:
    if path in DIRECTION:
        return DIRECTION[path]
    if path.startswith("games.") and path.endswith(".enabled"):
        return 1
    return None  # unknown


def is_increase(path: str, old: Any, new: Any) -> bool:
    """True if moving `path` from old → new increases risk (unknown ⇒ True)."""
    if old is None:  # new key (e.g. a new game) enables something
        return True
    if new is None:  # removed key → disabling something
        return False
    if path.endswith((".timeframes", ".regimes")):
        return bool(set(new) - set(old))  # adding a TF / regime enables more games
    d = _direction(path)
    if d is None:
        return True
    if d == 0:
        return False
    if isinstance(old, bool) or isinstance(new, bool):
        return bool(new) > bool(old) if d > 0 else bool(new) < bool(old)
    try:
        return (float(new) - float(old)) * d > 0
    except (TypeError, ValueError):
        return True


@dataclass(frozen=True)
class Diff:
    decreases: dict[str, Any]
    increases: dict[str, Any]


def diff(old: AppConfig, new: AppConfig) -> Diff:
    a, b = flatten(old.canonical()), flatten(new.canonical())
    dec: dict[str, Any] = {}
    inc: dict[str, Any] = {}
    for path in sorted(set(a) | set(b)):
        if a.get(path) == b.get(path):
            continue
        (inc if is_increase(path, a.get(path), b.get(path)) else dec)[path] = b.get(path)
    return Diff(dec, inc)


def apply_leaves(cfg: AppConfig, leaves: Mapping[str, Any]) -> AppConfig:
    flat = flatten(cfg.canonical())
    for k, v in leaves.items():
        if v is None:
            flat.pop(k, None)
        else:
            flat[k] = v
    return AppConfig.model_validate(unflatten(flat))


@dataclass
class Pending:
    path: str
    value: Any
    queued_ms: int
    apply_at_ms: int


@dataclass(frozen=True)
class ChangeEvent:
    kind: str  # applied | queued | rejected | cancelled
    paths: dict[str, Any]
    reason: str
    config_hash: str


class ConfigManager:
    """Holds the effective config; turns desired (on-disk) configs into gated changes."""

    def __init__(self, effective: AppConfig, pending: list[Pending] | None = None) -> None:
        self.effective = effective
        self.pending: dict[str, Pending] = {p.path: p for p in pending or []}

    def propose(self, desired: AppConfig, now_ms: int, drawdown: float) -> list[ChangeEvent]:
        d = self.effective.discipline
        out: list[ChangeEvent] = []
        ch = diff(self.effective, desired)
        if ch.decreases:
            self.effective = apply_leaves(self.effective, ch.decreases)
            out.append(
                ChangeEvent(
                    "applied", ch.decreases, "risk-decreasing → immediate", self.effective.hash
                )
            )
        # pending increases no longer desired are cancelled
        gone = {p: v.value for p, v in self.pending.items() if p not in ch.increases}
        for p in gone:
            del self.pending[p]
        if gone:
            out.append(
                ChangeEvent("cancelled", gone, "no longer in config file", self.effective.hash)
            )
        new_inc = {
            p: v
            for p, v in ch.increases.items()
            if p not in self.pending or self.pending[p].value != v
        }
        if new_inc:
            if drawdown > d.config_reject_dd:
                out.append(
                    ChangeEvent(
                        "rejected",
                        new_inc,
                        f"drawdown {drawdown:.1%} > {d.config_reject_dd:.0%}",
                        self.effective.hash,
                    )
                )
            else:
                due = now_ms + int(d.config_increase_delay_h * HOUR_MS)
                for p, v in new_inc.items():
                    self.pending[p] = Pending(p, v, now_ms, due)
                out.append(
                    ChangeEvent(
                        "queued",
                        new_inc,
                        f"risk-increasing → applies after {d.config_increase_delay_h:g} h",
                        self.effective.hash,
                    )
                )
        return out

    def tick(self, now_ms: int, drawdown: float) -> list[ChangeEvent]:
        due = {p: v for p, v in self.pending.items() if v.apply_at_ms <= now_ms}
        if not due:
            return []
        for p in due:
            del self.pending[p]
        leaves = {p: v.value for p, v in due.items()}
        if drawdown > self.effective.discipline.config_reject_dd:
            return [
                ChangeEvent(
                    "rejected",
                    leaves,
                    f"drawdown {drawdown:.1%} at apply time",
                    self.effective.hash,
                )
            ]
        self.effective = apply_leaves(self.effective, leaves)
        return [ChangeEvent("applied", leaves, "24 h pre-commitment elapsed", self.effective.hash)]
