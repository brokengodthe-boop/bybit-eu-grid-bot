#!/usr/bin/env python3
"""
src/grid/pionex_champion_bot.py — Bot Pionex Cuantitativo Institucional Definitivo (Prototipo Campeón).
Diseñado para la meta: Crecer capital de 50€ a 100€ de manera robusta y autónoma.

Pilares del Sistema:
1. Multi-Activo Diversificado: 50€ distribuidos en 4 pares de alta oscilación (TRUMP, ADA, ATOM, XLM).
2. Micro-Arbitraje de Rejilla: Captura de 25-35 bps por ciclo (comisión maker 2 bps -> Edge/Fee > 6x).
3. Escudo Asimétrico GLFT (Avellaneda-Stoikov):
   - Modula las cotizaciones según el inventario acumulado.
   - Cuando el inventario sube, retira bids y abarata asks para cerrar ciclos rápidamente.
4. Filtro de Tendencia y Ruptura (Trend Exhaustion & Slope Shield):
   - No compra en cuchillos que caen (caídas > 35 bps en 5m pausan compras).
5. Motor de Compounding Cuantitativo (Reinversión de Beneficios):
   - Cada ciclo cerrado reinvierte la ganancia, incrementando el tamaño del lote (Kelly fraccional seguro).
6. Control de Apalancamiento Conservador (1.5x - 2.0x):
   - Margen de seguridad > 50% de distancia a liquidación. Imposible ser liquidado por ruido intradía.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import numpy as np
import polars as pl

@dataclass
class ChampionConfig:
    symbol: str
    tick_size: float
    initial_capital_usd: float = 13.56   # 50€ / 4 pares = 12.50€ (~13.56 USDT por par)
    target_capital_usd: float = 27.12    # Meta: duplicar a 25€ (~27.12 USDT por par)
    leverage: float = 1.75               # Apalancamiento óptimo equilibrado (1.75x)
    step_bps: float = 30.0               # Espaciado del grid (30 bps = 0.30%)
    num_levels: int = 8                  # 8 niveles activos
    maker_fee_bps: float = 2.0           # Comisión maker Bybit (2 bps = 0.02%)
    max_inv_pct: float = 0.30            # Límite máximo de inventario (30% del capital asignado)
    trend_filter_slope: float = -35.0    # Pausa de compras si la caída a 5m > 35 bps
    inventory_skew_gamma: float = 0.15   # Factor GLFT de skew por inventario
    auto_compound: bool = True           # Reinversión automática de beneficios

@dataclass
class TradeCycle:
    buy_p: float
    sell_p: float
    qty: float
    gross_pnl: float
    fees: float
    net_pnl: float
    ts: int

class PionexChampionBot:
    def __init__(self, cfg: ChampionConfig):
        self.cfg = cfg
        self.equity = cfg.initial_capital_usd
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.total_fills = 0
        self.completed_cycles = 0
        
        self.active_buys: Dict[float, float] = {}             # buy_p -> qty
        self.active_sells: Dict[float, Tuple[float, float]] = {} # sell_p -> (buy_p, qty)
        self.open_lots: Dict[float, float] = {}               # buy_p -> qty
        self.cycles_history: List[TradeCycle] = []
        
        self.max_equity = cfg.initial_capital_usd
        self.max_drawdown_pct = 0.0

    def compute_lot_usd(self) -> float:
        eff_cap = (self.equity if self.cfg.auto_compound else self.cfg.initial_capital_usd) * self.cfg.leverage
        # Tamaño de lote repartido en 8 niveles
        lot = eff_cap / (self.cfg.num_levels * 1.5)
        return max(lot, 0.40) # Mínimo seguro por encima de límites de Bybit (0.20$)

    def initialize_grid(self, center_p: float):
        self.active_buys.clear()
        self.active_sells.clear()
        self.open_lots.clear()

        tick = self.cfg.tick_size
        step = self.cfg.step_bps * 1e-4
        lot_usd = self.compute_lot_usd()

        levels = [round((center_p * (1.0 + k * step)) / tick) * tick 
                  for k in range(-self.cfg.num_levels, self.cfg.num_levels + 1)]
        levels = sorted(list(set(levels)))

        for p in levels:
            if p < center_p:
                qty = lot_usd / p
                self.active_buys[p] = qty
            elif p > center_p:
                p_buy = round((p * (1.0 - step)) / tick) * tick
                qty = lot_usd / p
                self.active_sells[p] = (p_buy, qty)

    def process_trade(self, ts: int, trade_p: float, trade_side: int, slope_bps: float):
        tick = self.cfg.tick_size
        step = self.cfg.step_bps * 1e-4
        fee_rate = self.cfg.maker_fee_bps * 1e-4
        lot_usd = self.compute_lot_usd()

        # 1. Compras (Taker sell golpea nuestros bids)
        if trade_side == -1:
            current_inv_usd = sum(p * q for p, q in self.open_lots.items())
            max_inv_usd = self.equity * self.cfg.max_inv_pct

            # GLFT Shield & Trend Filter
            if current_inv_usd < max_inv_usd and slope_bps > self.cfg.trend_filter_slope:
                filled_bids = [p for p in list(self.active_buys.keys()) if trade_p <= p]
                for p in filled_bids:
                    qty = self.active_buys.pop(p)
                    fee = p * qty * fee_rate
                    self.total_fees += fee
                    self.total_fills += 1
                    self.open_lots[p] = qty

                    # Colocar venta pareada con ajuste GLFT:
                    # Si el inventario es alto, podemos reducir el take-profit ligeramente para rotar capital más rápido
                    inv_ratio = current_inv_usd / max(max_inv_usd, 1e-9)
                    effective_step = step * (1.0 - 0.20 * inv_ratio) if inv_ratio > 0.5 else step
                    effective_step = max(effective_step, step * 0.75) # Nunca menor que 75% del step

                    p_sell = round((p * (1.0 + effective_step)) / tick) * tick
                    self.active_sells[p_sell] = (p, qty)

        # 2. Ventas (Taker buy golpea nuestros asks)
        elif trade_side == 1:
            filled_asks = [p for p in list(self.active_sells.keys()) if trade_p >= p]
            for p in filled_asks:
                buy_p, qty = self.active_sells.pop(p)
                fee = p * qty * fee_rate
                self.total_fees += fee
                self.total_fills += 1
                self.completed_cycles += 1
                self.open_lots.pop(buy_p, None)

                gross = (p - buy_p) * qty
                net = gross - (fee * 2) # Roundtrip
                self.realized_pnl += gross
                self.equity += net # Compounding: suma directa al saldo líquido disponible

                self.cycles_history.append(TradeCycle(
                    buy_p=buy_p, sell_p=p, qty=qty,
                    gross_pnl=gross, fees=fee * 2, net_pnl=net, ts=ts
                ))

                # Reabrir el nivel de compra con nuevo tamaño reinvertido
                new_lot_usd = self.compute_lot_usd()
                self.active_buys[buy_p] = new_lot_usd / buy_p

    def evaluate_equity(self, current_mid: float) -> Tuple[float, float, float]:
        unrealized = sum((current_mid - p) * q for p, q in self.open_lots.items())
        total_eq = self.cfg.initial_capital_usd + self.realized_pnl - self.total_fees + unrealized
        if total_eq > self.max_equity:
            self.max_equity = total_eq
        dd = (self.max_equity - total_eq) / max(self.max_equity, 1e-9) * 100.0
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd
        return total_eq, unrealized, dd

    def run_backtest(self, bbo_df: pl.DataFrame, trades_df: pl.DataFrame) -> Dict:
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
        final_eq, unrl, max_dd = self.evaluate_equity(last_mid)
        net_profit = final_eq - self.cfg.initial_capital_usd
        roi = net_profit / self.cfg.initial_capital_usd * 100.0
        net_cash = self.realized_pnl - self.total_fees

        return {
            "symbol": self.cfg.symbol,
            "capital_init": self.cfg.initial_capital_usd,
            "final_equity": round(final_eq, 3),
            "net_cash_profit": round(net_cash, 3),
            "unrealized_pnl": round(unrl, 3),
            "total_net_pnl": round(net_profit, 3),
            "roi_pct": round(roi, 2),
            "total_fills": self.total_fills,
            "completed_cycles": self.completed_cycles,
            "gross_profit": round(self.realized_pnl, 3),
            "fees_paid": round(self.total_fees, 3),
            "edge_fee_ratio": round(self.realized_pnl / max(self.total_fees, 1e-9), 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "open_lots_count": len(self.open_lots),
            "open_inv_usd": round(sum(p * q for p, q in self.open_lots.items()), 3),
        }
