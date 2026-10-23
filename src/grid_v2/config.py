"""
src/grid_v2/config.py — Carga y validación de configuración (config.yaml + .env).

Las claves API se leen SOLO del entorno (.env) y nunca se imprimen.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

load_dotenv()


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


@dataclass
class ExchangeConfig:
    tld: str = "eu"
    testnet: bool = False
    recv_window: int = 5000
    rate_limit_per_second: int = 8
    rest_poll_interval_seconds: int = 15
    max_clock_ahead_ms: int = 800        # Bybit rechaza timestamps > server_time + 1000 ms
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)


@dataclass
class StrategyConfig:
    base_currency: str = "EUR"
    initial_capital_eur: Decimal = Decimal("50")
    pairs: List[str] = field(default_factory=list)
    num_levels: int = 4
    step_min_bps: Decimal = Decimal("25")
    k_fee_multiplier: Decimal = Decimal("2.5")       # step >= k * 2 * maker_fee_bps (k >= 2.5)
    min_expected_net_bps: Decimal = Decimal("5")
    step_mode: str = "fixed"                          # "fixed" | "adaptive" (volatilidad realizada)
    vol_lookback_minutes: int = 240
    vol_horizon_minutes: int = 60
    vol_multiplier: Decimal = Decimal("1.0")
    step_max_bps: Decimal = Decimal("400")
    recenter_buffer_levels: Decimal = Decimal("1.0")  # cancela compras más allá de (niveles + buffer) pasos

    def __post_init__(self) -> None:
        if self.k_fee_multiplier < Decimal("2.5"):
            raise ValueError("k_fee_multiplier debe ser >= 2.5")
        if self.num_levels < 1:
            raise ValueError("num_levels debe ser >= 1")
        if self.step_mode not in ("fixed", "adaptive"):
            raise ValueError("step_mode debe ser 'fixed' o 'adaptive'")


@dataclass
class RiskConfig:
    max_inv_pct_pair: Decimal = Decimal("0.30")
    max_inv_pct_global: Decimal = Decimal("0.70")
    slope_threshold_bps: Decimal = Decimal("35")
    slope_cooldown_seconds: int = 900
    order_size_factor: Decimal = Decimal("1.0")
    max_drawdown_pct: Decimal = Decimal("15")
    emergency_liquidate: bool = False
    trend_filter_enabled: bool = True
    cancel_buys_on_pause: bool = True


@dataclass
class BacktestConfig:
    days: int = 90
    interval: str = "1"
    fill_through_ticks: int = 1      # el precio debe atravesar la orden N ticks (cola conservadora)
    in_sample_fraction: float = 0.5  # walk-forward: primera mitad elige, segunda mitad valida


@dataclass
class BotConfig:
    exchange: ExchangeConfig
    strategy: StrategyConfig
    risk: RiskConfig
    backtest: BacktestConfig
    db_path: str = "data/grid_state.db"
    paper_db_path: str = "data/grid_paper.db"
    log_level: str = "INFO"
    log_file: str = "logs/grid_trading.log"


def load_config(config_path: str = "config.yaml") -> BotConfig:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Archivo de configuración no encontrado: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        data: Dict[str, Any] = yaml.safe_load(f) or {}

    ex = data.get("exchange", {})
    st = data.get("strategy", {})
    rk = data.get("risk", {})
    bt = data.get("backtest", {})

    exchange = ExchangeConfig(
        tld=ex.get("tld", "eu"),
        testnet=bool(ex.get("testnet", False)),
        recv_window=int(ex.get("recv_window", 5000)),
        rate_limit_per_second=int(ex.get("rate_limit_per_second", 8)),
        rest_poll_interval_seconds=int(ex.get("rest_poll_interval_seconds", 15)),
        max_clock_ahead_ms=int(ex.get("max_clock_ahead_ms", 800)),
        api_key=os.getenv("BYBIT_API_KEY", ""),
        api_secret=os.getenv("BYBIT_API_SECRET", ""),
    )
    strategy = StrategyConfig(
        base_currency=st.get("base_currency", "EUR"),
        initial_capital_eur=_d(st.get("initial_capital_eur", 50)),
        pairs=list(st.get("pairs", [])),
        num_levels=int(st.get("num_levels", 4)),
        step_min_bps=_d(st.get("step_min_bps", 25)),
        k_fee_multiplier=_d(st.get("k_fee_multiplier", 2.5)),
        min_expected_net_bps=_d(st.get("min_expected_net_bps", 5)),
        step_mode=st.get("step_mode", "fixed"),
        vol_lookback_minutes=int(st.get("vol_lookback_minutes", 240)),
        vol_horizon_minutes=int(st.get("vol_horizon_minutes", 60)),
        vol_multiplier=_d(st.get("vol_multiplier", 1.0)),
        step_max_bps=_d(st.get("step_max_bps", 400)),
        recenter_buffer_levels=_d(st.get("recenter_buffer_levels", 1.0)),
    )
    risk = RiskConfig(
        max_inv_pct_pair=_d(rk.get("max_inv_pct_pair", 0.30)),
        max_inv_pct_global=_d(rk.get("max_inv_pct_global", 0.70)),
        slope_threshold_bps=_d(rk.get("slope_threshold_bps", 35)),
        slope_cooldown_seconds=int(rk.get("slope_cooldown_seconds", 900)),
        order_size_factor=_d(rk.get("order_size_factor", 1.0)),
        max_drawdown_pct=_d(rk.get("max_drawdown_pct", 15)),
        emergency_liquidate=bool(rk.get("emergency_liquidate", False)),
        trend_filter_enabled=bool(rk.get("trend_filter_enabled", True)),
        cancel_buys_on_pause=bool(rk.get("cancel_buys_on_pause", True)),
    )
    backtest = BacktestConfig(
        days=int(bt.get("days", 90)),
        interval=str(bt.get("interval", "1")),
        fill_through_ticks=int(bt.get("fill_through_ticks", 1)),
        in_sample_fraction=float(bt.get("in_sample_fraction", 0.5)),
    )
    return BotConfig(
        exchange=exchange, strategy=strategy, risk=risk, backtest=backtest,
        db_path=data.get("persistence", {}).get("db_path", "data/grid_state.db"),
        paper_db_path=data.get("persistence", {}).get("paper_db_path", "data/grid_paper.db"),
        log_level=data.get("logging", {}).get("level", "INFO"),
        log_file=data.get("logging", {}).get("log_file", "logs/grid_trading.log"),
    )
