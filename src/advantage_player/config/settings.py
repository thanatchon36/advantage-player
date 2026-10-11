"""Environment secrets (.env) and runtime config (config.yaml)."""

from __future__ import annotations

import hashlib
import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class LiveNotConfirmedError(RuntimeError):
    pass


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    binance_env: Env = Env.DEMO
    binance_demo_api_key: SecretStr = SecretStr("")
    binance_demo_api_secret: SecretStr = SecretStr("")
    binance_live_api_key: SecretStr = SecretStr("")
    binance_live_api_secret: SecretStr = SecretStr("")
    live_trading_confirmed: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_password: SecretStr = SecretStr("")

    def check_mode(self) -> None:
        """Live refuses to start unless LIVE_TRADING_CONFIRMED is exactly true."""
        if self.binance_env is Env.LIVE and not self.live_trading_confirmed:
            raise LiveNotConfirmedError(
                "BINANCE_ENV=live but LIVE_TRADING_CONFIRMED is not 'true' — refusing to start"
            )

    @property
    def api_key(self) -> str:
        k = self.binance_live_api_key if self.binance_env is Env.LIVE else self.binance_demo_api_key
        return k.get_secret_value()

    @property
    def api_secret(self) -> str:
        s = (
            self.binance_live_api_secret
            if self.binance_env is Env.LIVE
            else self.binance_demo_api_secret
        )
        return s.get_secret_value()

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def banner(self) -> str:
        return "⚠️ LIVE" if self.binance_env is Env.LIVE else "DEMO"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseCfg(_Strict):
    scan_interval_s: int = 900
    quote_asset: str = "USDT"
    min_listing_days: int = 30
    min_quote_volume_usd: float = 50_000_000
    min_depth_usd_10bps: float = 250_000
    max_tick_bps: float = 2.0
    min_move_to_cost: float = 5.0
    max_abs_funding: float = 0.0005
    anomaly_lookback_min: int = 60
    open_tables: int = 8
    watchlist: int = 12
    cluster_corr: float = 0.7
    cluster_lookback_days: int = 7
    slippage_model_bps: float = 1.0
    table_change_debounce_s: int = 1800


class RegimeCfg(_Strict):
    ema_fast: int = 20
    ema_slow: int = 50
    trend_slope_bps: float = 2.0
    high_vol_pctile: float = 90
    high_vol_lookback_days: int = 30
    allow_high_vol: bool = False


class GameCfg(BaseModel):
    """Common game fields; strategy-specific thresholds live in model_extra."""

    model_config = ConfigDict(extra="allow", frozen=True)

    enabled: bool = True
    timeframes: list[str]
    regimes: list[Literal["TREND", "RANGE", "HIGH_VOL"]]
    entry: Literal["maker", "taker"] = "taker"

    def param(self, name: str, default: float) -> float:
        extra = self.model_extra or {}
        return float(extra.get(name, default))


class LedgerCfg(_Strict):
    bootstrap_samples: int = 5000
    confidence: float = 0.95
    bh_q: float = 0.10
    shadow_min_n: int = 200
    probe_min_n: int = 100
    probe_max_slippage_ratio: float = 1.5
    suspend_slippage_ratio: float = 2.0
    suspend_fee_share: float = 0.5
    rolling_window: int = 100
    cusum_k: float = 0.25
    cusum_h: float = 5.0
    walk_forward_train_frac: float = 0.6
    research_days: int = 30
    research_interval_h: float = 24.0
    eval_interval_s: int = 900
    suspend_cooldown_h: float = 24.0


class RiskCfg(_Strict):
    kelly_fraction: float = 0.25
    max_risk_per_trade: float = 0.01
    probe_risk: float = 0.001
    dd_half_at: float = 0.05
    dd_halt_at: float = 0.10
    liq_stop_fraction: float = 0.6
    fee_buffer: float = 0.002
    max_margin_frac: float = 0.05
    max_total_open_risk: float = 0.03
    max_cluster_risk: float = 0.015
    max_net_direction_risk: float = 0.02
    max_positions: int = 4
    daily_loss_limit: float = 0.03
    sweep_trigger: float = 1.20
    auto_sweep: bool = False


class DisciplineCfg(_Strict):
    sl_confirm_timeout_s: float = 2.0
    symbol_lock_after_stop_min: int = 15
    loss_streak_pause_n: int = 4
    loss_streak_pause_min: int = 30
    kill_resume_min_hours: int = 12
    config_increase_delay_h: int = 24
    config_reject_dd: float = 0.05
    max_spread_bps: float = 5.0
    funding_blackout_min: int = 5
    max_clock_drift_ms: int = 500
    min_rate_headroom: float = 0.2
    stale_data_s: float = 5.0


class ExecutionCfg(_Strict):
    maker_timeout_s: float = 5.0
    max_taker_slippage_bps: float = 3.0
    reconcile_interval_s: int = 30
    recv_window_ms: int = Field(5000, le=60000)


class StorageCfg(_Strict):
    url: str = "sqlite:///data/advantage_player.db"
    data_dir: str = "data"


class AppConfig(_Strict):
    target_leverage: int = Field(50, ge=1, le=125)
    universe: UniverseCfg = UniverseCfg()
    regime: RegimeCfg = RegimeCfg()
    games: dict[str, GameCfg] = {}
    ledger: LedgerCfg = LedgerCfg()
    risk: RiskCfg = RiskCfg()
    discipline: DisciplineCfg = DisciplineCfg()
    execution: ExecutionCfg = ExecutionCfg()
    storage: StorageCfg = StorageCfg()

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """STORAGE_URL / DATA_DIR env vars override `storage` (Docker points it at Postgres)."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    storage = data.setdefault("storage", {})
    for env_key, field in (("STORAGE_URL", "url"), ("DATA_DIR", "data_dir")):
        if os.environ.get(env_key):
            storage[field] = os.environ[env_key]
    return AppConfig.model_validate(data)
