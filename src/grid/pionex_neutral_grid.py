#!/usr/bin/env python3
"""
src/grid/pionex_neutral_grid.py — Pionex Futures Neutral Grid Engine.
Implementa el bot insigne de Pionex para futuros:
- Zona Superior (> P0): Grid SHORT (Vende arriba, recompra abajo para cubrir).
- Zona Inferior (< P0): Grid LONG (Compra abajo, vende arriba para cerrar).
- Trailing Center opcional: El centro P0 sigue la media móvil (VWAP/EMA) para evitar quedar atrapado en tendencias unidireccionales.
- Compounding dinámico de capital: Reinvierte beneficios aumentando el tamaño del lote.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import numpy as np
import polars as pl

@dataclass
class NeutralGridConfig:
    symbol: str
    tick_size: float
    initial_capital_usd: float = 13.56   # 50€ / 4 pares = 12.50€ (~13.56 USDT por par)
    leverage: float = 2.0                # Apalancamiento conservador
    step_bps: float = 30.0               # Espaciado de cada rejilla en bps
    num_levels: int = 8                  # Niveles por lado (8 longs, 8 shorts)
    maker_fee_bps: float = 2.0           # Comisión maker Bybit VIP0 (2 bps)
    trailing_center: bool = True         # Si True, P0 se ajusta con EMA/VWAP para neutralidad total
    trailing_window_ms: int = 1800000    # Ventana de 30 minutos para recentrar
    max_inv_pct: float = 0.40            # Inventario máximo 40% del capital
    min_order_usd: float = 0.50          # Mínimo absoluto de orden
    auto_compound: bool = True           # Reinversión de beneficios

@dataclass
class GridOrder:
    order_id: int
    side: str         # "BUY" o "SELL"
    order_type: str   # "OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "CLOSE_SHORT"
    price: float
    qty: float
    parent_price: float = 0.0

class PionexNeutralGridBot:
    def __init__(self, cfg: NeutralGridConfig):
        self.cfg = cfg
        self.equity = cfg.initial_capital_usd
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.fills = 0
        self.cycles = 0
        
        # Posiciones abiertas:
        # Long lots: precio_compra -> qty
        self.long_lots: Dict[float, float] = {}
        # Short lots: precio_venta -> qty
        self.short_lots: Dict[float, float] = {}
        
        # Órdenes activas en el libro
        self.active_bids: Dict[float, GridOrder] = {} # precio -> orden
        self.active_asks: Dict[float, GridOrder] = {} # precio -> orden
        
        self.p0: float = 0.0
        self.order_counter = 0
        self.max_equity = cfg.initial_capital_usd
        self.max_drawdown_pct = 0.0
        
    def next_id(self) -> int:
        self.order_counter += 1
        return self.order_counter

    def compute_lot_usd(self) -> float:
        eff_cap = (self.equity if self.cfg.auto_compound else self.cfg.initial_capital_usd) * self.cfg.leverage
        # Repartir entre los niveles
        lot = eff_cap / (self.cfg.num_levels * 2)
        return max(lot, self.cfg.min_order_usd)

    def initialize_grid(self, center_p: float):
        self.p0 = center_p
        self.active_bids.clear()
        self.active_asks.clear()
        self.long_lots.clear()
        self.short_lots.clear()
        
        tick = self.cfg.tick_size
        step = self.cfg.step_bps * 1e-4
        lot_usd = self.compute_lot_usd()
        
        # 1. Colocar órdenes LONG por debajo de P0
        for i in range(1, self.cfg.num_levels + 1):
            p_buy = round((self.p0 * (1.0 - i * step)) / tick) * tick
            qty = lot_usd / p_buy
            self.active_bids[p_buy] = GridOrder(
                order_id=self.next_id(),
                side="BUY",
                order_type="OPEN_LONG",
                price=p_buy,
                qty=qty
            )
            
        # 2. Colocar órdenes SHORT por encima de P0
        for i in range(1, self.cfg.num_levels + 1):
            p_sell = round((self.p0 * (1.0 + i * step)) / tick) * tick
            qty = lot_usd / p_sell
            self.active_asks[p_sell] = GridOrder(
                order_id=self.next_id(),
                side="SELL",
                order_type="OPEN_SHORT",
                price=p_sell,
                qty=qty
            )

    def recenter_grid(self, new_center: float):
        """Trailing Grid: desplaza la rejilla si el precio medio se ha movido significativamente."""
        tick = self.cfg.tick_size
        step = self.cfg.step_bps * 1e-4
        # Solo recentrar si el centro se mueve más de 2 steps
        if abs(new_center - self.p0) / self.p0 < (2.0 * step):
            return
            
        old_p0 = self.p0
        self.p0 = new_center
        lot_usd = self.compute_lot_usd()
        
        # Limpiar órdenes OPEN que quedaron desalineadas (mantener órdenes CLOSE)
        bids_to_del = [p for p, o in self.active_bids.items() if o.order_type == "OPEN_LONG"]
        for p in bids_to_del:
            del self.active_bids[p]
            
        asks_to_del = [p for p, o in self.active_asks.items() if o.order_type == "OPEN_SHORT"]
        for p in asks_to_del:
            del self.active_asks[p]
            
        # Reposicionar OPEN_LONG por debajo de nuevo P0
        for i in range(1, self.cfg.num_levels + 1):
            p_buy = round((self.p0 * (1.0 - i * step)) / tick) * tick
            if p_buy not in self.active_bids:
                qty = lot_usd / p_buy
                self.active_bids[p_buy] = GridOrder(
                    order_id=self.next_id(),
                    side="BUY",
                    order_type="OPEN_LONG",
                    price=p_buy,
                    qty=qty
                )
                
        # Reposicionar OPEN_SHORT por encima de nuevo P0
        for i in range(1, self.cfg.num_levels + 1):
            p_sell = round((self.p0 * (1.0 + i * step)) / tick) * tick
            if p_sell not in self.active_asks:
                qty = lot_usd / p_sell
                self.active_asks[p_sell] = GridOrder(
                    order_id=self.next_id(),
                    side="SELL",
                    order_type="OPEN_SHORT",
                    price=p_sell,
                    qty=qty
                )

    def process_trade(self, ts: int, trade_p: float, trade_side: int, current_mid: float):
        tick = self.cfg.tick_size
        step = self.cfg.step_bps * 1e-4
        fee_rate = self.cfg.maker_fee_bps * 1e-4
        lot_usd = self.compute_lot_usd()
        
        # Trailing center check
        if self.cfg.trailing_center and current_mid > 0:
            self.recenter_grid(current_mid)

        # ---------------------------------------------------------------------
        # 1. TAKER SELL (trade_side == -1) -> Golpea nuestros BIDS pasivos
        # ---------------------------------------------------------------------
        if trade_side == -1:
            hit_bids = [p for p in list(self.active_bids.keys()) if trade_p <= p]
            for p in hit_bids:
                order = self.active_bids.pop(p)
                fee = p * order.qty * fee_rate
                self.total_fees += fee
                self.fills += 1
                
                if order.order_type == "OPEN_LONG":
                    # Se abre posición Long
                    self.long_lots[p] = order.qty
                    # Colocar orden de cierre (CLOSE_LONG: Venta a +step)
                    p_sell = round((p * (1.0 + step)) / tick) * tick
                    self.active_asks[p_sell] = GridOrder(
                        order_id=self.next_id(),
                        side="SELL",
                        order_type="CLOSE_LONG",
                        price=p_sell,
                        qty=order.qty,
                        parent_price=p
                    )
                    
                elif order.order_type == "CLOSE_SHORT":
                    # Se cierra posición Short con ganancia!
                    sell_p = order.parent_price
                    qty = order.qty
                    self.short_lots.pop(sell_p, None)
                    
                    gross = (sell_p - p) * qty # Ganancia short: vendimos caro, compramos barato
                    net = gross - (fee * 2)
                    self.realized_pnl += gross
                    self.equity += net
                    self.cycles += 1
                    
                    # Reabrir el OPEN_SHORT original
                    new_qty = lot_usd / sell_p
                    self.active_asks[sell_p] = GridOrder(
                        order_id=self.next_id(),
                        side="SELL",
                        order_type="OPEN_SHORT",
                        price=sell_p,
                        qty=new_qty
                    )

        # ---------------------------------------------------------------------
        # 2. TAKER BUY (trade_side == 1) -> Golpea nuestros ASKS pasivos
        # ---------------------------------------------------------------------
        elif trade_side == 1:
            hit_asks = [p for p in list(self.active_asks.keys()) if trade_p >= p]
            for p in hit_asks:
                order = self.active_asks.pop(p)
                fee = p * order.qty * fee_rate
                self.total_fees += fee
                self.fills += 1
                
                if order.order_type == "OPEN_SHORT":
                    # Se abre posición Short
                    self.short_lots[p] = order.qty
                    # Colocar orden de cierre (CLOSE_SHORT: Compra a -step)
                    p_buy = round((p * (1.0 - step)) / tick) * tick
                    self.active_bids[p_buy] = GridOrder(
                        order_id=self.next_id(),
                        side="BUY",
                        order_type="CLOSE_SHORT",
                        price=p_buy,
                        qty=order.qty,
                        parent_price=p
                    )
                    
                elif order.order_type == "CLOSE_LONG":
                    # Se cierra posición Long con ganancia!
                    buy_p = order.parent_price
                    qty = order.qty
                    self.long_lots.pop(buy_p, None)
                    
                    gross = (p - buy_p) * qty # Ganancia long: compramos barato, vendemos caro
                    net = gross - (fee * 2)
                    self.realized_pnl += gross
                    self.equity += net
                    self.cycles += 1
                    
                    # Reabrir el OPEN_LONG original
                    new_qty = lot_usd / buy_p
                    self.active_bids[buy_p] = GridOrder(
                        order_id=self.next_id(),
                        side="BUY",
                        order_type="OPEN_LONG",
                        price=buy_p,
                        qty=new_qty
                    )

    def get_mark_to_market(self, mid_p: float) -> Tuple[float, float, float]:
        """Calcula el equity mark-to-market considerando PnL flotante de longs y shorts."""
        unrealized_long = sum((mid_p - p) * q for p, q in self.long_lots.items())
        unrealized_short = sum((p - mid_p) * q for p, q in self.short_lots.items())
        tot_unrealized = unrealized_long + unrealized_short
        
        total_equity = self.cfg.initial_capital_usd + self.realized_pnl - self.total_fees + tot_unrealized
        if total_equity > self.max_equity:
            self.max_equity = total_equity
        dd = (self.max_equity - total_equity) / max(self.max_equity, 1e-9) * 100.0
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd
            
        return total_equity, tot_unrealized, dd

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
            curr_mid = bbo_mid[max(0, j)]
            self.process_trade(ts, p, int(sd), curr_mid)

        last_mid = bbo_mid[-1]
        final_equity, unrl, max_dd = self.get_mark_to_market(last_mid)
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
            "unrealized_pnl": round(unrl, 3),
            "edge_fee_ratio": round(self.realized_pnl / max(self.total_fees, 1e-9), 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "open_longs": len(self.long_lots),
            "open_shorts": len(self.short_lots),
        }
