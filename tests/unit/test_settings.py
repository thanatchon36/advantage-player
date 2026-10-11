from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from advantage_player.config.settings import (
    AppConfig,
    Env,
    LiveNotConfirmedError,
    Secrets,
    load_config,
)

ROOT = Path(__file__).resolve().parents[2]


def _secrets(**kw: Any) -> Secrets:
    return Secrets(_env_file=None, **kw)


def test_live_refuses_without_confirmation() -> None:
    with pytest.raises(LiveNotConfirmedError):
        _secrets(binance_env=Env.LIVE, live_trading_confirmed=False).check_mode()


def test_live_ok_with_confirmation_and_demo_always_ok() -> None:
    _secrets(binance_env=Env.LIVE, live_trading_confirmed=True).check_mode()
    _secrets(binance_env=Env.DEMO).check_mode()


def test_keys_selected_by_env_and_banner() -> None:
    s = _secrets(
        binance_env=Env.DEMO,
        binance_demo_api_key="dk",
        binance_demo_api_secret="ds",
        binance_live_api_key="lk",
        binance_live_api_secret="ls",
    )
    assert (s.api_key, s.api_secret, s.banner) == ("dk", "ds", "DEMO")
    live = s.model_copy(update={"binance_env": Env.LIVE})
    assert (live.api_key, live.banner) == ("lk", "⚠️ LIVE")


def test_secret_not_in_repr() -> None:
    s = _secrets(binance_demo_api_secret="topsecret")
    assert "topsecret" not in repr(s)


def test_config_yaml_matches_spec_defaults() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.target_leverage == 50
    r = cfg.risk
    assert (r.kelly_fraction, r.max_risk_per_trade, r.probe_risk) == (0.25, 0.01, 0.001)
    assert (r.dd_half_at, r.dd_halt_at) == (0.05, 0.10)
    assert (r.max_total_open_risk, r.max_cluster_risk, r.max_net_direction_risk) == (
        0.03,
        0.015,
        0.02,
    )
    assert (r.max_positions, r.daily_loss_limit, r.max_margin_frac) == (4, 0.03, 0.05)
    assert set(cfg.games) == {"A_trend_pullback", "B_order_flow", "C_range_fade"}
    assert cfg == AppConfig.model_validate(cfg.canonical())


def test_config_hash_changes_with_content() -> None:
    a = AppConfig()
    b = AppConfig(target_leverage=20)
    assert a.hash != b.hash
    assert a.hash == AppConfig().hash


def test_unknown_config_key_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"max_risk_per_trade": 0.01, "typo_key": 1}})


def test_storage_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_URL", "postgresql+psycopg://ap:ap@db:5432/ap")
    monkeypatch.setenv("DATA_DIR", "/data")
    cfg = load_config("config.yaml")
    assert cfg.storage.url == "postgresql+psycopg://ap:ap@db:5432/ap"
    assert cfg.storage.data_dir == "/data"
    monkeypatch.delenv("STORAGE_URL")
    monkeypatch.delenv("DATA_DIR")
    assert load_config("config.yaml").storage.url.startswith("sqlite:///")
