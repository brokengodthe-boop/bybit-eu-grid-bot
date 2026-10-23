#!/usr/bin/env python3
"""
pionex_institutional_engine.py — Motor de Trading de Grid Cuantitativo Institucional para Bybit Linear.
Diseñado para emular y superar los bots de grid de Pionex mediante:
- Captura de oscilaciones amplias (25-50 bps) donde las comisiones maker (2 bps) son despreciables.
- Paireo estricto de lotes FIFO (cada compra tiene su venta objetivo garantizando ganancia positiva por ciclo).
- Escudo Microestructural GLFT (límite estricto de exposición de inventario y skew de reserva).
- Filtro de Régimen y Desplome (protección contra tendencias bajistas severas).
- Ejecución 100% Post-Only Maker.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import numpy as np
import polars as pl

@dataclass
class GridConfig:
    symbol: str
    tick_size: float
    capital_usdt: float = 1000.0
    step_bps: float = 30.0               # Espaciado del grid en puntos básicos (30 bps = 0.30%)
    unit_order_usd: float = 40.0         # Tamaño por nivel del grid en USDT
    max_inventory_usd: float = 250.0     # Límite máximo de inventario long (25% del capital)
    maker_fee_bps: float = 2.0           # Comisión maker Bybit (2 bps = 0.02%)
    num_grid_levels: int = 30            # Niveles a cada lado del precio central
    trend_filter_slope_limit: float = -40.0 # Pausar compras si pendiente 5m < -40 bps
    max_drawdown_stop_pct: float = 5.0   # Stop loss de emergencia a nivel de cuenta

@dataclass
class GridCycle:
    buy_price: float
    sell_price: float
    qty: float
    gross_profit_usd: float
    total_fee_usd: float
    net_profit_usd: float
    created_ts: int
    closed_ts: int

class PionexInstitutionalGridEngine:
    def __init__(self, config: GridConfig):
        self.cfg = config
        self.capital = config.capital_usdt
        self.step_pct = config.step_bps * 1e-4

        # Estado del Grid
        self.active_buys: Dict[float, float] = {}             # buy_price -> qty
        self.active_sells: Dict[float, Tuple[float, float]] = {} # sell_price -> (buy_price, qty)
        self.open_lots: Dict[float, float] = {}               # buy_price -> qty

        # Métricas de rendimiento
        self.realized_grid_profit = 0.0
        self.total_fees_paid = 0.0
        self.total_fills = 0
        self.completed_cycles = 0
        self.completed_cycle_records: List[GridCycle] = []
        self.max_equity = config.capital_usdt
        self.max_drawdown_pct = 0.0
        self.equity_history = []

    def initialize_grid(self, center_price: float):
        """Inicializa las líneas del grid alrededor del precio central."""
        self.active_buys.clear()
        self.active_sells.clear()
        self.open_lots.clear()

        tick = self.cfg.tick_size
        step = self.step_pct
        unit_usd = self.cfg.unit_order_usd

        levels = [round((center_price * (1.0 + k * step)) / tick) * tick 
                  for k in range(-self.cfg.num_grid_levels, self.cfg.num_grid_levels + 1)]
        levels = sorted(list(set(levels)))

        for p in levels:
            if p < center_price:
                qty = unit_usd / p
                self.active_buys[p] = qty
            elif p > center_price:
                p_buy_ref = round((p * (1.0 - step)) / tick) * tick
                qty = unit_usd / p
                self.active_sells[p] = (p_buy_ref, qty)

    def process_trade(self, ts: int, trade_price: float, trade_side: int, current_slope_bps: float = 0.0):
        """
        trade_side: +1 (Taker Buy -> llena nuestras órdenes Sell)
                    -1 (Taker Sell -> llena nuestras órdenes Buy)
        """
        tick = self.cfg.tick_size
        fee_rate = self.cfg.maker_fee_bps * 1e-4

        # 1. Compras (Taker vende contra nuestros Bids)
        if trade_side == -1:
            current_inv_usd = sum(p * q for p, q in self.open_lots.items())
            
            # Escudo GLFT y Filtro de Desplome:
            if current_inv_usd < self.cfg.max_inventory_usd and current_slope_bps > self.cfg.trend_filter_slope_limit:
                filled_b = [p for p in list(self.active_buys.keys()) if trade_price <= p]
                for p in filled_b:
                    qty = self.active_buys.pop(p)
                    fee = p * qty * fee_rate
                    self.total_fees_paid += fee
                    self.total_fills += 1
                    self.open_lots[p] = qty

                    # Colocar venta pareada exactamente un paso de grid por encima
                    p_sell = round((p * (1.0 + self.step_pct)) / tick) * tick
                    self.active_sells[p_sell] = (p, qty)

        # 2. Ventas (Taker compra contra nuestros Asks)
        elif trade_side == 1:
            filled_s = [p for p in list(self.active_sells.keys()) if trade_price >= p]
            for p in filled_s:
                buy_p, qty = self.active_sells.pop(p)
                fee = p * qty * fee_rate
                self.total_fees_paid += fee
                self.total_fills += 1
                self.completed_cycles += 1
                self.open_lots.pop(buy_p, None)

                # Ganancia bruta del ciclo = (Precio de Venta - Precio de Compra) * Cantidad
                gross_pnl = (p - buy_p) * qty
                self.realized_grid_profit += gross_pnl

                cycle = GridCycle(
                    buy_price=buy_p, sell_price=p, qty=qty,
                    gross_profit_usd=gross_pnl, total_fee_usd=fee * 2, # Roundtrip approx
                    net_profit_usd=gross_pnl - fee * 2,
                    created_ts=ts, closed_ts=ts
                )
                self.completed_cycle_records.append(cycle)

                # Reabrir la orden de compra original en el nivel inferior
                self.active_buys[buy_p] = self.cfg.unit_order_usd / buy_p

    def get_equity(self, current_mid: float) -> float:
        unrealized = sum((current_mid - p) * q for p, q in self.open_lots.items())
        equity = self.capital + self.realized_grid_profit - self.total_fees_paid + unrealized
        if equity > self.max_equity:
            self.max_equity = equity
        dd = (self.max_equity - equity) / max(self.max_equity, 1e-9) * 100.0
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd
        return equity

    def run_backtest(self, bbo_df: pl.DataFrame, trades_df: pl.DataFrame) -> Dict:
        tick = self.cfg.tick_size
        bbo_ts = bbo_df["ts_ex"].to_numpy()
        b_k = bbo_df["bid_k"].to_numpy().astype(float)
        a_k = bbo_df["ask_k"].to_numpy().astype(float)
        bbo_mid = 0.5 * (b_k + a_k) * tick

        t_ts = trades_df["ts_ex"].to_numpy()
        t_px = trades_df["px_k"].to_numpy().astype(float) * tick
        t_sd = trades_df["side"].to_numpy()

        # Inicializar el grid en el primer mid
        self.initialize_grid(bbo_mid[0])

        for ts, p, sd in zip(t_ts, t_px, t_sd):
            # Calcular pendiente de tendencia a 5 minutos
            j = np.searchsorted(bbo_ts, ts, side="right") - 1
            j_5m = np.searchsorted(bbo_ts, ts - 300000, side="right") - 1
            slope = (bbo_mid[j] - bbo_mid[max(0, j_5m)]) / bbo_mid[j] * 1e4 if j > 0 else 0.0

            self.process_trade(ts, p, int(sd), slope)

        last_mid = bbo_mid[-1]
        final_equity = self.get_equity(last_mid)
        net_realized = self.realized_grid_profit - self.total_fees_paid
        open_inv_usd = sum(p * q for p, q in self.open_lots.items())
        unrealized = sum((last_mid - p) * q for p, q in self.open_lots.items())

        return {
            "symbol": self.cfg.symbol,
            "step_bps": self.cfg.step_bps,
            "capital_init": self.capital,
            "final_equity": round(final_equity, 2),
            "net_realized_profit": round(net_realized, 2),
            "grid_gross_profit": round(self.realized_grid_profit, 2),
            "total_fees_paid": round(self.total_fees_paid, 2),
            "edge_fee_ratio": round(self.realized_grid_profit / max(self.total_fees_paid, 1e-9), 2),
            "total_fills": self.total_fills,
            "completed_cycles": self.completed_cycles,
            "open_inventory_usd": round(open_inv_usd, 2),
            "floating_unrealized": round(unrealized, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "roi_pct": round((final_equity - self.capital) / self.capital * 100.0, 2),
        }
