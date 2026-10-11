from __future__ import annotations

import pytest

from advantage_player.sim.fills import Costs, SimTrade
from advantage_player.strategies.base import Signal, Target

C = Costs(maker_fee=0.0002, taker_fee=0.0004, slippage_bps=1.0, spread_bps=2.0)


def _sig(**kw: object) -> Signal:
    base: dict[str, object] = dict(
        side="BUY",
        entry_type="market",
        entry_ref=100.0,
        stop=99.0,
        targets=(Target(2.0, 1.0),),
        tag="t",
    )
    base.update(kw)
    return Signal(**base)  # type: ignore[arg-type]


def st(t: SimTrade) -> str:
    """Read state without mypy literal narrowing across mutations."""
    return t.state


def _open_market(sig: Signal, ts: int = 1000) -> SimTrade:
    t = SimTrade("g", "X", sig, 0, C)
    t.try_enter(100, 100, ts, None, None, ref=100.0)
    return t


def test_market_entry_pays_half_spread_plus_slippage_and_r_math() -> None:
    t = _open_market(_sig())
    assert t.entry_px == pytest.approx(100 * 1.0001 * 1.0001)
    t.update(101.9, 102.5, 2000, first=101.9)
    assert st(t) == "closed" and t.exit_reason == "target"
    r = t.result()
    pnl = 102 * 0.9999 - t.entry_px
    fees = t.entry_px * 0.0004 + 102 * 0.9999 * 0.0004
    assert r.r_net == pytest.approx(pnl - fees)  # risk = 1.0
    assert r.r_gross == pytest.approx(2.0)  # at intended prices, before costs
    assert r.fee_r == pytest.approx(fees)
    assert r.slippage_r == pytest.approx((t.entry_px - 100) + 102 * 0.0001)


def test_market_entry_uses_live_book_when_available() -> None:
    t = SimTrade("g", "X", _sig(), 0, C)
    t.try_enter(100, 100, 10, bid=99.9, ask=100.1, ref=100)
    assert t.entry_px == pytest.approx(100.1 * 1.0001)


def test_maker_entry_needs_trade_through_and_times_out() -> None:
    sig = _sig(entry_type="maker")
    t = SimTrade("g", "X", sig, 0, C)
    t.try_enter(100.0, 100.5, 100, None, None, 100)  # touches 100 but not through
    assert st(t) == "pending"
    t.try_enter(99.99, 100.2, 200, None, None, 100)
    assert st(t) == "open" and t.entry_px == 100.0 and t.entry_fee == 0.0002
    t2 = SimTrade("g", "X", sig, 0, C)
    t2.try_enter(99.0, 99.5, C.maker_timeout_ms + 1, None, None, 100)
    assert st(t2) == "cancelled"


def test_stop_wins_when_bar_spans_stop_and_target() -> None:
    t = _open_market(_sig())
    t.update(98.5, 102.5, 2000, first=100.0)
    assert t.exit_reason == "stop"
    assert t.legs[-1].price == pytest.approx(99.0 * 0.9999)  # stop level, not bar low


def test_gap_through_stop_fills_at_gap_price() -> None:
    t = _open_market(_sig())
    t.update(97.0, 97.0, 2000, first=97.0)
    assert t.legs[-1].price == pytest.approx(97.0 * 0.9999)
    assert t.result().r_net < -3


def test_short_side_symmetry() -> None:
    sig = _sig(side="SELL", stop=101.0)
    t = _open_market(sig)
    assert t.entry_px == pytest.approx(100 * 0.9999 * 0.9999)
    t.update(97.5, 98.1, 2000, first=98.1)
    assert t.exit_reason == "target"
    assert t.result().r_gross == pytest.approx(2.0)


def test_tp1_partial_then_breakeven_stop() -> None:
    sig = _sig(
        targets=(Target(1.5, 0.5), Target(2.5, 0.5)), move_stop_to_be_after_first_target=True
    )
    t = _open_market(sig)
    t.update(100.5, 101.6, 2000, first=100.5)
    assert t.remaining == pytest.approx(0.5)
    assert t.stop == pytest.approx(t.entry_px)  # moved to BE
    t.update(t.entry_px - 0.5, 100.5, 3000, first=100.5)
    assert t.exit_reason == "be_stop"
    r = t.result()
    assert r.r_gross > 0.7  # 0.5 × 1.5R locked in


def test_trailing_only_moves_toward_profit() -> None:
    sig = _sig(targets=(Target(1.0, 0.5), Target(10.0, 0.5)), trail_atr=1.0, atr=1.0)
    t = _open_market(sig)
    t.update(100.5, 101.2, 2000, first=100.5)  # TP1 hit, extreme 101.2 → trail 100.2
    assert t.stop == pytest.approx(100.2)
    t.update(100.4, 100.9, 3000, first=100.9)  # lower high must not lower the stop
    assert t.stop == pytest.approx(100.2)
    t.update(102.0, 103.0, 4000, first=102.0)
    assert t.stop == pytest.approx(102.0)


def test_time_stop() -> None:
    t = _open_market(_sig(time_stop_bars=2))
    t.on_bar_close(100.5, 2000)
    assert st(t) == "open"
    t.on_bar_close(100.5, 3000)
    assert st(t) == "closed" and t.exit_reason == "time"


def test_funding_long_pays_short_receives() -> None:
    t = _open_market(_sig())
    t.on_funding(0.001, 100.0)
    t.force_exit(100.0, 5000)
    assert t.result().funding_r == pytest.approx(0.1)
    s = _open_market(_sig(side="SELL", stop=101.0))
    s.on_funding(0.001, 100.0)
    assert s.funding_paid == pytest.approx(-0.1)


def test_mae_mfe_tracking() -> None:
    t = _open_market(_sig(targets=()))
    t.update(99.5, 100.8, 2000, first=100.0)
    assert t.mae == pytest.approx(t.entry_px - 99.5)
    assert t.mfe == pytest.approx(100.8 - t.entry_px)
