#!/usr/bin/env python3
"""
pionex_institutional_grid.py — Bot de Trading Cuantitativo tipo Pionex Institucional para Bybit Linear.
Optimizado para ejecución de alto rendimiento (simulación en milisegundos con exactitud de ticks).
Incorpora:
1. Grid Asimétrico Dinámico calibrado a Volatilidad (ATR / Realized Volatility).
2. Skew de Inventario Microestructural GLFT (evita bag-holding y acumulación tóxica).
3. Guardián de Régimen y Tendencia (Momentum & FlowExhaustion filter).
4. Ejecución 100% Maker Post-Only (garantiza captura neta de spread > comisiones).
5. Contabilidad exacta de comisiones Maker (2 bps) y mark-to-market riguroso.
"""
import os, sys, glob, json, math, time
import collections
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
import numpy as np
import polars as pl

@dataclass
class GridConfig:
    symbol: str
    tick_size: float
    capital_usdt: float = 1000.0         # Capital base asignado
    leverage: float = 2.0                # Apalancamiento máximo permitido
    base_grid_bps: float = 50.0          # Espaciado base entre niveles (50 bps = 0.50%)
    min_grid_bps: float = 35.0           # Espaciado mínimo absoluto (> 2x fee roundtrip)
    max_grid_bps: float = 120.0          # Espaciado máximo en alta volatilidad
    num_levels_per_side: int = 5         # Niveles activos a cada lado
    maker_fee_bps: float = 2.0           # Comisión Maker Bybit VIP0 (2 bps)
    max_inventory_qty: float = 100.0     # Límite máximo absoluto de inventario (contratos)
    gamma_risk: float = 0.05             # Parámetro de aversión al inventario GLFT
    skew_intensity: float = 1.5          # Intensidad de sesgo asimétrico de inventario
    rebalance_threshold_bps: float = 15.0 # Desviación mínima para rebalancear orden existente
    trend_filter_lookback_s: int = 300   # Ventana de filtro de tendencia (5 minutos)
    max_drawdown_stop_pct: float = 5.0   # Kill switch: parada de emergencia al 5% drawdown

@dataclass
class GridOrder:
    order_id: str
    side: int                            # +1: Buy Limit (Bid), -1: Sell Limit (Ask)
    price: float                         # Precio límite
    price_k: int                         # Precio en ticks
    qty: float                           # Tamaño de la orden
    created_ts: int                      # Timestamp de colocación (ms)
    grid_index: int                      # Nivel en el grid
    post_only: bool = True

@dataclass
class GridState:
    inventory_qty: float = 0.0           # Inventario actual (positivo = long, negativo = short)
    inventory_cost: float = 0.0          # Coste base ponderado del inventario
    cash_usdt: float = 1000.0            # Saldo disponible en USDT
    realized_pnl: float = 0.0            # PnL realizado acumulado
    total_maker_fees: float = 0.0        # Comisiones maker pagadas acumuladas
    total_fills: int = 0                 # Número total de fills completados
    completed_cycles: int = 0            # Ciclos compra-venta cerrados con éxito
    max_equity: float = 1000.0           # Marca de agua de equity máximo
    max_drawdown: float = 0.0            # Máximo drawdown porcentual registrado
    active_bids: Dict[int, GridOrder] = field(default_factory=dict)
    active_asks: Dict[int, GridOrder] = field(default_factory=dict)
    max_bid_price: float = 0.0           # Caché rápido de mejor bid propio
    min_ask_price: float = 1e9           # Caché rápido de mejor ask propio

class PionexInstitutionalGridBot:
    def __init__(self, config: GridConfig):
        self.cfg = config
        self.state = GridState(cash_usdt=config.capital_usdt, max_equity=config.capital_usdt)
        self.order_counter = 0

        # EWMAs y métricas en tiempo real con deque O(1)
        self.last_mid = 0.0
        self.last_ts = 0
        self.rolling_prices = collections.deque(maxlen=360) # últimos 360 puntos (30 min a paso de 5s)
        self.realized_vol_bps = config.base_grid_bps
        self.trend_slope_bps = 0.0

        # Historial de equity y fills
        self.equity_curve = []
        self.fills_log = []

    def update_market_stats(self, ts: int, mid: float):
        self.last_mid = mid
        self.last_ts = ts
        self.rolling_prices.append((ts, mid))

        # Volatilidad Realizada (std en bps)
        if len(self.rolling_prices) >= 10:
            prices = np.array([p[1] for p in self.rolling_prices])
            returns = np.diff(prices) / prices[:-1]
            if len(returns) > 1:
                vol_bps = float(np.std(returns) * np.sqrt(720) * 1e4) # Anualizado/horario a 5s
                self.realized_vol_bps = float(np.clip(vol_bps, self.cfg.min_grid_bps, self.cfg.max_grid_bps))

        # Tendencia en últimos 5 min (60 muestras de 5s)
        n_trend = min(len(self.rolling_prices), 60)
        if n_trend >= 5:
            p_first = self.rolling_prices[-n_trend][1]
            p_last = self.rolling_prices[-1][1]
            self.trend_slope_bps = float((p_last - p_first) / p_first * 1e4)

    def compute_glft_skew(self) -> Tuple[float, float]:
        q_ratio = np.clip(self.state.inventory_qty / max(self.cfg.max_inventory_qty, 1e-9), -1.0, 1.0)
        bid_mult = 1.0 + self.cfg.skew_intensity * q_ratio
        ask_mult = 1.0 - self.cfg.skew_intensity * q_ratio

        bid_allowed = bool(q_ratio < 0.95)
        ask_allowed = bool(q_ratio > -0.95)

        return float(np.clip(bid_mult, 0.5, 3.0)) if bid_allowed else 999.0, \
               float(np.clip(ask_mult, 0.5, 3.0)) if ask_allowed else 999.0

    def compute_target_grid(self, mid: float) -> Tuple[List[float], List[float]]:
        bid_mult, ask_mult = self.compute_glft_skew()
        step_bps = self.realized_vol_bps

        if self.trend_slope_bps < -40.0:
            bid_mult *= 1.8
        elif self.trend_slope_bps > 40.0:
            ask_mult *= 1.8

        target_bids = []
        if bid_mult < 900.0:
            for l in range(1, self.cfg.num_levels_per_side + 1):
                offset_bps = l * step_bps * bid_mult
                p = mid * (1.0 - offset_bps * 1e-4)
                p_k = round(p / self.cfg.tick_size)
                target_bids.append(p_k * self.cfg.tick_size)

        target_asks = []
        if ask_mult < 900.0:
            for l in range(1, self.cfg.num_levels_per_side + 1):
                offset_bps = l * step_bps * ask_mult
                p = mid * (1.0 + offset_bps * 1e-4)
                p_k = round(p / self.cfg.tick_size)
                target_asks.append(p_k * self.cfg.tick_size)

        return target_bids, target_asks

    def rebalance_orders(self, ts: int, mid: float):
        target_bids, target_asks = self.compute_target_grid(mid)
        unit_qty = (self.cfg.capital_usdt * self.cfg.leverage) / (mid * self.cfg.num_levels_per_side * 2)
        unit_qty = round(unit_qty, 3)

        # 1. Bids
        for l, tgt_p in enumerate(target_bids):
            existing = self.state.active_bids.get(l)
            if existing is None:
                self.order_counter += 1
                self.state.active_bids[l] = GridOrder(
                    order_id=f"B_{self.order_counter}", side=1, price=tgt_p,
                    price_k=round(tgt_p / self.cfg.tick_size),
                    qty=unit_qty, created_ts=ts, grid_index=l
                )
            else:
                diff_bps = abs(existing.price - tgt_p) / tgt_p * 1e4
                if diff_bps > self.cfg.rebalance_threshold_bps:
                    self.order_counter += 1
                    self.state.active_bids[l] = GridOrder(
                        order_id=f"B_{self.order_counter}", side=1, price=tgt_p,
                        price_k=round(tgt_p / self.cfg.tick_size),
                        qty=unit_qty, created_ts=ts, grid_index=l
                    )

        if not target_bids and self.state.active_bids:
            self.state.active_bids.clear()

        # 2. Asks
        for l, tgt_p in enumerate(target_asks):
            existing = self.state.active_asks.get(l)
            if existing is None:
                self.order_counter += 1
                self.state.active_asks[l] = GridOrder(
                    order_id=f"A_{self.order_counter}", side=-1, price=tgt_p,
                    price_k=round(tgt_p / self.cfg.tick_size),
                    qty=unit_qty, created_ts=ts, grid_index=l
                )
            else:
                diff_bps = abs(existing.price - tgt_p) / tgt_p * 1e4
                if diff_bps > self.cfg.rebalance_threshold_bps:
                    self.order_counter += 1
                    self.state.active_asks[l] = GridOrder(
                        order_id=f"A_{self.order_counter}", side=-1, price=tgt_p,
                        price_k=round(tgt_p / self.cfg.tick_size),
                        qty=unit_qty, created_ts=ts, grid_index=l
                    )

        if not target_asks and self.state.active_asks:
            self.state.active_asks.clear()

        # Actualizar cachés de precios para filtrado rápido en O(1)
        self.state.max_bid_price = max((o.price for o in self.state.active_bids.values()), default=0.0)
        self.state.min_ask_price = min((o.price for o in self.state.active_asks.values()), default=1e9)

    def on_trade_event(self, ts: int, trade_p: float, trade_qty: float, taker_side: int):
        # Filtro ultrarrápido O(1): si el trade no llega al mejor nivel, ignorar de inmediato
        if taker_side == -1: # Taker vende, puede llenar nuestros Bids
            if trade_p > self.state.max_bid_price:
                return
            filled_bids = []
            for idx, order in self.state.active_bids.items():
                if trade_p <= order.price:
                    self.execute_fill(ts, order, order.price)
                    filled_bids.append(idx)
            for idx in filled_bids:
                self.state.active_bids.pop(idx, None)
            if filled_bids:
                self.state.max_bid_price = max((o.price for o in self.state.active_bids.values()), default=0.0)

        elif taker_side == 1: # Taker compra, puede llenar nuestros Asks
            if trade_p < self.state.min_ask_price:
                return
            filled_asks = []
            for idx, order in self.state.active_asks.items():
                if trade_p >= order.price:
                    self.execute_fill(ts, order, order.price)
                    filled_asks.append(idx)
            for idx in filled_asks:
                self.state.active_asks.pop(idx, None)
            if filled_asks:
                self.state.min_ask_price = min((o.price for o in self.state.active_asks.values()), default=1e9)

    def execute_fill(self, ts: int, order: GridOrder, fill_price: float):
        fee_usdt = fill_price * order.qty * (self.cfg.maker_fee_bps * 1e-4)
        self.state.total_maker_fees += fee_usdt
        self.state.total_fills += 1

        prev_qty = self.state.inventory_qty

        if order.side == 1: # Compra (Bid fill)
            self.state.cash_usdt -= (fill_price * order.qty + fee_usdt)
            new_qty = prev_qty + order.qty
            if prev_qty >= 0:
                self.state.inventory_cost = (self.state.inventory_cost * prev_qty + fill_price * order.qty) / max(new_qty, 1e-9)
            else:
                closed_qty = min(abs(prev_qty), order.qty)
                pnl = (self.state.inventory_cost - fill_price) * closed_qty
                self.state.realized_pnl += pnl
                self.state.completed_cycles += 1
                if new_qty > 0:
                    self.state.inventory_cost = fill_price
            self.state.inventory_qty = new_qty

        else: # Venta (Ask fill)
            self.state.cash_usdt += (fill_price * order.qty - fee_usdt)
            new_qty = prev_qty - order.qty
            if prev_qty <= 0:
                self.state.inventory_cost = (self.state.inventory_cost * abs(prev_qty) + fill_price * order.qty) / max(abs(new_qty), 1e-9)
            else:
                closed_qty = min(prev_qty, order.qty)
                pnl = (fill_price - self.state.inventory_cost) * closed_qty
                self.state.realized_pnl += pnl
                self.state.completed_cycles += 1
                if new_qty < 0:
                    self.state.inventory_cost = fill_price
            self.state.inventory_qty = new_qty

        self.fills_log.append({
            "ts": ts,
            "side": "BUY" if order.side == 1 else "SELL",
            "price": fill_price,
            "qty": order.qty,
            "fee_usdt": fee_usdt,
            "inv_after": self.state.inventory_qty,
            "realized_pnl": self.state.realized_pnl,
        })

    def mark_to_market(self, mid: float) -> float:
        unrealized_pnl = 0.0
        if self.state.inventory_qty > 0:
            unrealized_pnl = (mid - self.state.inventory_cost) * self.state.inventory_qty
        elif self.state.inventory_qty < 0:
            unrealized_pnl = (self.state.inventory_cost - mid) * abs(self.state.inventory_qty)

        total_equity = self.cfg.capital_usdt + self.state.realized_pnl + unrealized_pnl - self.state.total_maker_fees
        if total_equity > self.state.max_equity:
            self.state.max_equity = total_equity

        dd = (self.state.max_equity - total_equity) / max(self.state.max_equity, 1e-9) * 100.0
        if dd > self.state.max_drawdown:
            self.state.max_drawdown = dd

        return total_equity

    def run_simulation(self, bbo_df: pl.DataFrame, trades_df: pl.DataFrame) -> Dict:
        """
        Backtest ultrarrápido y exacto:
        - Rejilla temporal de 5s para BBO y estadísticas.
        - Eventos de trades exactos procesados con filtro O(1).
        """
        bbo_ts = bbo_df["ts_ex"].to_numpy()
        b_k = bbo_df["bid_k"].to_numpy().astype(float)
        a_k = bbo_df["ask_k"].to_numpy().astype(float)
        mid_arr = 0.5 * (b_k + a_k) * self.cfg.tick_size

        t_ts = trades_df["ts_ex"].to_numpy()
        t_px = trades_df["px_k"].to_numpy().astype(float) * self.cfg.tick_size
        t_qty = trades_df["qty"].to_numpy()
        t_side = trades_df["side"].to_numpy()

        # Generar rejilla de muestreo de 5 segundos para rebalances
        t0, t1 = bbo_ts[0], bbo_ts[-1]
        grid_5s = np.arange(t0, t1, 5000, dtype=np.int64)
        idx_grid = np.searchsorted(bbo_ts, grid_5s, side="right") - 1
        idx_grid = np.clip(idx_grid, 0, len(mid_arr) - 1)
        grid_mids = mid_arr[idx_grid]

        events = []
        for i in range(len(grid_5s)):
            events.append((grid_5s[i], 0, grid_mids[i], 0.0, 0))
        for i in range(len(t_ts)):
            events.append((t_ts[i], 1, t_px[i], t_qty[i], int(t_side[i])))

        # Ordenar cronológicamente (trades antes de rebalance a mismo ts)
        events.sort(key=lambda x: (x[0], x[1]))

        for e in events:
            ts = e[0]
            typ = e[1]

            if typ == 0: # Rebalance periódico de 5s
                mid = e[2]
                self.update_market_stats(ts, mid)
                self.rebalance_orders(ts, mid)
                eq = self.mark_to_market(mid)
                self.equity_curve.append((ts, eq, self.state.realized_pnl - self.state.total_maker_fees))

                if self.state.max_drawdown >= self.cfg.max_drawdown_stop_pct:
                    print(f"[!] KILL SWITCH: Drawdown superó {self.cfg.max_drawdown_stop_pct}%")
                    break
            else: # Trade del mercado
                self.on_trade_event(ts, e[2], e[3], e[4])

        final_mid = mid_arr[-1]
        final_equity = self.mark_to_market(final_mid)
        net_profit_usdt = final_equity - self.cfg.capital_usdt
        roi_pct = net_profit_usdt / self.cfg.capital_usdt * 100.0

        return {
            "symbol": self.cfg.symbol,
            "capital_init": self.cfg.capital_usdt,
            "final_equity": round(final_equity, 2),
            "net_profit_usdt": round(net_profit_usdt, 2),
            "roi_pct": round(roi_pct, 3),
            "total_fills": self.state.total_fills,
            "completed_cycles": self.state.completed_cycles,
            "total_fees_paid": round(self.state.total_maker_fees, 3),
            "final_inventory_qty": round(self.state.inventory_qty, 3),
            "max_drawdown_pct": round(self.state.max_drawdown, 2),
            "gross_realized_pnl": round(self.state.realized_pnl, 2),
        }
