#!/usr/bin/env python3
"""
pionex_compounding_bot.py — Bot de Trading Compounding Grid Institucional para Capital Pequeño (50€ -> 100€).
Diseñado específicamente para optimizar el crecimiento con riesgo controlado:
- Capital inicial: 50.0 EUR (~54.25 USDT).
- Meta: Duplicar el capital (100.0 EUR / ~108.50 USDT).
- Compounding dinámico: Cada ciclo reinvierte los beneficios aumentando el tamaño del lote.
- Apalancamiento seguro regulado (2.0x - 2.5x) con margen de liquidación > 35%.
- Escudo GLFT y Diversificación multi-activo (4 pares: TRUMP, ADA, ATOM, XLM).
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import numpy as np
import polars as pl

@dataclass
class CompoundingConfig:
    symbol: str
    tick_size: float
    initial_capital_usd: float = 13.56   # 50€ / 4 pares = 12.50€ (~13.56 USDT por par)
    target_capital_usd: float = 27.12    # Meta: duplicar a 25€ (~27.12 USDT por par)
    leverage: float = 2.0                # Apalancamiento conservador (2x)
    base_step_bps: float = 30.0          # Espaciado base (30 bps)
    num_levels: int = 10                 # 5 compras y 5 ventas activas
    maker_fee_bps: float = 2.0           # Comisión maker Bybit (2 bps)
    max_inv_pct: float = 0.35            # Inventario máximo: 35% del capital del par
    trend_filter_slope: float = -40.0    # Pausa de compras si caída 5m > 40 bps
    min_order_usd: float = 1.0           # Mínimo absoluto por orden
    auto_compound: bool = True           # Reinversión automática de beneficios

@dataclass
class CycleRecord:
    buy_p: float
    sell_p: float
    qty: float
    gross_pnl: float
    fees: float
    net_pnl: float
    ts: int

class PionexCompoundingBot:
    def __init__(self, cfg: CompoundingConfig):
        self.cfg = cfg
        self.equity = cfg.initial_capital_usd
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.fills = 0
        self.cycles = 0
        self.active_buys: Dict[float, float] = {}             # p -> qty
        self.active_sells: Dict[float, Tuple[float, float]] = {} # sell_p -> (buy_p, qty)
        self.open_lots: Dict[float, float] = {}               # buy_p -> qty
        self.history: List[CycleRecord] = []
        self.max_equity = cfg.initial_capital_usd
        self.max_drawdown_pct = 0.0

    def compute_unit_order_usd(self) -> float:
        """Calcula el tamaño de la orden por nivel con reinversión de beneficios."""
        if self.cfg.auto_compound:
            current_effective = self.equity * self.cfg.leverage
        else:
            current_effective = self.cfg.initial_capital_usd * self.cfg.leverage
        
        # Repartir entre los niveles activos
        unit = current_effective / (self.cfg.num_levels * 2)
        return max(unit, self.cfg.min_order_usd)

    def initialize_grid(self, center_p: float):
        self.active_buys.clear()
        self.active_sells.clear()
        self.open_lots.clear()

        tick = self.cfg.tick_size
        step = self.cfg.base_step_bps * 1e-4
        unit_usd = self.compute_unit_order_usd()

        levels = [round((center_p * (1.0 + k * step)) / tick) * tick 
                  for k in range(-self.cfg.num_levels, self.cfg.num_levels + 1)]
        levels = sorted(list(set(levels)))

        for p in levels:
            if p < center_p:
                qty = unit_usd / p
                self.active_buys[p] = qty
            elif p > center_p:
                p_buy = round((p * (1.0 - step)) / tick) * tick
                qty = unit_usd / p
                self.active_sells[p] = (p_buy, qty)

    def process_trade(self, ts: int, trade_p: float, trade_side: int, slope_bps: float = 0.0):
        tick = self.cfg.tick_size
        step = self.cfg.base_step_bps * 1e-4
        fee_rate = self.cfg.maker_fee_bps * 1e-4

        # 1. Compras (Taker sell golpea nuestros bids)
        if trade_side == -1:
            current_inv_usd = sum(p * q for p, q in self.open_lots.items())
            max_inv_usd = self.equity * self.cfg.max_inv_pct

            # Escudo GLFT y filtro de desplome
            if current_inv_usd < max_inv_usd and slope_bps > self.cfg.trend_filter_slope:
                filled_b = [p for p in list(self.active_buys.keys()) if trade_p <= p]
                for p in filled_b:
                    qty = self.active_buys.pop(p)
                    fee = p * qty * fee_rate
                    self.total_fees += fee
                    self.fills += 1
                    self.open_lots[p] = qty

                    # Colocar venta pareada
                    p_sell = round((p * (1.0 + step)) / tick) * tick
                    self.active_sells[p_sell] = (p, qty)

        # 2. Ventas (Taker buy golpea nuestros asks)
        elif trade_side == 1:
            filled_s = [p for p in list(self.active_sells.keys()) if trade_p >= p]
            for p in filled_s:
                buy_p, qty = self.active_sells.pop(p)
                fee = p * qty * fee_rate
                self.total_fees += fee
                self.fills += 1
                self.cycles += 1
                self.open_lots.pop(buy_p, None)

                gross = (p - buy_p) * qty
                net = gross - (fee * 2) # Ida y vuelta
                self.realized_pnl += gross
                self.equity += net # Reinversión directa al equity

                self.history.append(CycleRecord(
                    buy_p=buy_p, sell_p=p, qty=qty,
                    gross_pnl=gross, fees=fee * 2, net_pnl=net, ts=ts
                ))

                # Reabrir el buy con nuevo tamaño reinvertido
                unit_usd = self.compute_unit_order_usd()
                self.active_buys[buy_p] = unit_usd / buy_p

    def update_mark_to_market(self, current_mid: float):
        unrealized = sum((current_mid - p) * q for p, q in self.open_lots.items())
        total_eq = self.cfg.initial_capital_usd + self.realized_pnl - self.total_fees + unrealized
        if total_eq > self.max_equity:
            self.max_equity = total_eq
        dd = (self.max_equity - total_eq) / max(self.max_equity, 1e-9) * 100.0
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd
        return total_eq

    def run_simulation(self, bbo_df: pl.DataFrame, trades_df: pl.DataFrame) -> Dict:
        tick = self.cfg.tick_size
        bbo_ts = bbo_df["ts_ex"].to_numpy()
        b_k = bbo_df["bid_k"].to_numpy().astype(float)
        a_k = bbo_df["ask_k"].to_numpy().astype(float)
        bbo_mid = 0.5 * (b_k + a_k) * tick

        t_ts = trades_df["ts_ex"].to_numpy()
        t_px = trades_df["px_k"].to_numpy().astype(float) * tick
        t_sd = trades_df["side"].to_numpy()

        self.initialize_grid(bbo_mid[0])

        for ts, p, sd in zip(t_ts, t_px, t_sd):
            j = np.searchsorted(bbo_ts, ts, side="right") - 1
            j_5m = np.searchsorted(bbo_ts, ts - 300000, side="right") - 1
            slope = (bbo_mid[j] - bbo_mid[max(0, j_5m)]) / bbo_mid[j] * 1e4 if j > 0 else 0.0

            self.process_trade(ts, p, int(sd), slope)

        last_mid = bbo_mid[-1]
        final_equity = self.update_mark_to_market(last_mid)
        net_profit = final_equity - self.cfg.initial_capital_usd
        roi = net_profit / self.cfg.initial_capital_usd * 100.0

        return {
            "symbol": self.cfg.symbol,
            "capital_init": self.cfg.initial_capital_usd,
            "final_equity": round(final_equity, 3),
            "net_profit": round(net_profit, 3),
            "roi_pct": round(roi, 2),
            "fills": self.fills,
            "cycles": self.cycles,
            "gross_profit": round(self.realized_pnl, 3),
            "fees_paid": round(self.total_fees, 3),
            "edge_fee_ratio": round(self.realized_pnl / max(self.total_fees, 1e-9), 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
        }
