"""Portfolio caps (SPEC §6). Pure: given open positions, how much new risk may be added.

- total open risk (Σ f) ≤ 3 %
- per correlation cluster ≤ 1.5 %
- net same-direction exposure |Σ long f − Σ short f| ≤ 2 %
- ≤ 4 concurrent positions, ≤ 1 position per symbol (no adding / pyramiding)
- daily loss ≥ 3 % of start-of-day equity ⇒ no new entries until 00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from advantage_player.config.settings import RiskCfg


@dataclass(frozen=True)
class OpenRisk:
    symbol: str
    direction: int  # +1 long, −1 short
    f: float  # risk fraction at entry (bankroll lost if the stop hits)
    cluster: int


@dataclass(frozen=True)
class CapCheck:
    allowed_f: float  # largest f that fits all caps (0 ⇒ blocked)
    reason: str  # binding constraint, "" if none bound


def cap_room(
    positions: Sequence[OpenRisk],
    symbol: str,
    direction: int,
    cluster: int,
    cfg: RiskCfg,
    daily_loss_frac: float = 0.0,
) -> CapCheck:
    if daily_loss_frac >= cfg.daily_loss_limit:
        return CapCheck(0.0, "daily_loss_limit")
    if any(p.symbol == symbol for p in positions):
        return CapCheck(0.0, "position_exists")  # no averaging down / adding / pyramiding
    if len(positions) >= cfg.max_positions:
        return CapCheck(0.0, "max_positions")
    total = sum(p.f for p in positions)
    in_cluster = sum(p.f for p in positions if cluster >= 0 and p.cluster == cluster)
    net = sum(p.f * p.direction for p in positions)
    rooms = {
        "total_open_risk": cfg.max_total_open_risk - total,
        "cluster_risk": cfg.max_cluster_risk - in_cluster,
        # adding f in `direction` moves net by direction·f; |net + d·f| ≤ cap
        "net_direction": cfg.max_net_direction_risk - net * direction,
        "max_risk_per_trade": cfg.max_risk_per_trade,
    }
    reason, room = min(rooms.items(), key=lambda kv: kv[1])
    room = max(room, 0.0)
    return CapCheck(room, reason if room < cfg.max_risk_per_trade - 1e-12 else "")
