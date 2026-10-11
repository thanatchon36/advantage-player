from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from advantage_player.exchange.models import LeverageBrackets, SymbolInfo, round_to_step

BTC = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "contractType": "PERPETUAL",
    "quoteAsset": "USDT",
    "onboardDate": 1569398400000,
    "deliveryDate": 4133404802000,
    "filters": [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "261.10",
            "tickSize": "0.10",
            "maxPrice": "809484",
        },
        {"filterType": "LOT_SIZE", "maxQty": "1000", "minQty": "0.0001", "stepSize": "0.0001"},
        {"stepSize": "0.001", "maxQty": "120", "minQty": "0.001", "filterType": "MARKET_LOT_SIZE"},
        {"filterType": "MIN_NOTIONAL", "notional": "50"},
    ],
}

BRACKETS = {
    "symbol": "SOLUSDT",
    "brackets": [
        {
            "bracket": 2,
            "initialLeverage": 25,
            "notionalCap": 250000,
            "notionalFloor": 50000,
            "maintMarginRatio": 0.01,
            "cum": 250.0,
        },
        {
            "bracket": 1,
            "initialLeverage": 50,
            "notionalCap": 50000,
            "notionalFloor": 0,
            "maintMarginRatio": 0.005,
            "cum": 0.0,
        },
        {
            "bracket": 3,
            "initialLeverage": 10,
            "notionalCap": 1000000,
            "notionalFloor": 250000,
            "maintMarginRatio": 0.025,
            "cum": 4000.0,
        },
    ],
}


def test_symbol_parse() -> None:
    si = SymbolInfo.from_exchange_info(BTC)
    assert si.tick_size == Decimal("0.10")
    assert si.step_size == Decimal("0.0001")
    assert si.market_step_size == Decimal("0.001")
    assert si.min_notional == Decimal("50")


def test_rounding_modes() -> None:
    si = SymbolInfo.from_exchange_info(BTC)
    assert si.round_price("60000.07") == Decimal("60000.1")
    assert si.round_price("60000.07", "down") == Decimal("60000.0")
    assert si.round_price("60000.01", "up") == Decimal("60000.1")
    assert si.round_qty("0.12349") == Decimal("0.1234")
    assert si.round_qty("0.12349", market=True) == Decimal("0.123")
    assert si.round_qty("5000") == Decimal("1000")  # capped at maxQty


def test_min_notional() -> None:
    si = SymbolInfo.from_exchange_info(BTC)
    assert not si.meets_min_notional(Decimal("0.0008"), Decimal("60000"))  # $48
    assert si.meets_min_notional(Decimal("0.0009"), Decimal("60000"))  # $54


@given(
    v=st.decimals(min_value="0.00001", max_value="1000000", places=8, allow_nan=False),
    step=st.sampled_from([Decimal("0.001"), Decimal("0.1"), Decimal("1"), Decimal("0.0001")]),
)
def test_round_down_never_exceeds_and_is_multiple(v: Decimal, step: Decimal) -> None:
    r = round_to_step(v, step, "down")
    assert r <= v
    assert v - r < step
    assert (r / step) == (r / step).to_integral_value()


def test_bracket_lookup_falls_with_notional() -> None:
    b = LeverageBrackets.from_api(BRACKETS)
    assert b.max_leverage(1_000) == 50
    assert b.max_leverage(50_000) == 25
    assert b.max_leverage(300_000) == 10
    assert b.mmr(100_000) == 0.01
    with pytest.raises(ValueError):
        b.max_leverage(2_000_000)
