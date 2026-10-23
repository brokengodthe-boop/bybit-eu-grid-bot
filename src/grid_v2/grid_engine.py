"""
src/grid_v2/grid_engine.py — Motor de rejilla spot: máquina de estados pura e independiente del venue.

El motor NO habla con el exchange. Produce acciones (órdenes a colocar / cancelar) y consume
eventos (fills, cancelaciones, rechazos). Los "drivers" (backtest, paper, live) aplican esas
acciones. Así la MISMA lógica se ejecuta en los tres modos.

Invariantes:
- Toda cantidad/precio es Decimal. Compras redondeadas hacia abajo al tick, ventas hacia arriba.
- Lotes y órdenes indexados por orderLinkId / lot_id, nunca por precio.
- Una sola orden de compra o lote abierto por nivel de precio (no se apila inventario en el mismo nivel).
- Coste exacto por lote (cost_quote): la comisión de compra en moneda base reduce las unidades,
  no se "pierde" ni se cuenta dos veces. El polvo (dust) por redondeo a qtyStep se arrastra con su coste.
- Un lote nunca se ofrece por debajo de su coste unitario + comisión de venta + margen mínimo,
  salvo liquidación de emergencia del kill switch.
"""
from __future__ import annotations

import logging
import math
import threading
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Dict, List, Optional, Tuple

from src.grid_v2.config import RiskConfig, StrategyConfig
from src.grid_v2.models import (FeeRate, GridMetrics, InstrumentInfo, Lot, LotStatus, Order, OrderSide,
                                OrderStatus, now_ms)
from src.grid_v2.persistence import Database
from src.grid_v2.risk import RiskManager

logger = logging.getLogger("GridEngine")

LINK_PREFIX = "g2"
BPS = Decimal("10000")
ZERO = Decimal("0")


def quantize_price_down(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def quantize_price_up(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def quantize_qty_down(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step


def new_link_id(kind: str) -> str:
    """orderLinkId único (<= 36 caracteres, límite de Bybit) con prefijo propio para reconocer órdenes del bot."""
    return f"{LINK_PREFIX}{kind}-{uuid.uuid4().hex[:24]}"


class GridEngine:
    def __init__(self, strategy: StrategyConfig, risk: RiskManager, db: Database):
        self.strategy = strategy
        self.risk = risk
        self.db = db
        self._lock = threading.RLock()

        self.instruments: Dict[str, InstrumentInfo] = {}
        self.fees: Dict[str, FeeRate] = {}
        self.fee_floor_step_bps: Dict[str, Decimal] = {}

        self.active_orders: Dict[str, Order] = {}
        self.open_lots: Dict[str, Lot] = {}
        self.lots_by_sell_id: Dict[str, Lot] = {}

        # fills parciales de compra aún no convertidos en lote: link_id -> [net_qty, cost_quote, gross_qty, fee_base]
        self.buy_pool: Dict[str, List[Decimal]] = {}
        # polvo por redondeo: cantidad y coste exacto
        self.residual_qty: Dict[str, Decimal] = {}
        self.residual_cost: Dict[str, Decimal] = {}

        self.metrics = GridMetrics(cash_balance=strategy.initial_capital_eur)
        self.mids: Dict[str, Decimal] = {}
        self.hold_times_ms: List[int] = []
        self._sold_cost: Dict[str, Decimal] = {}
        self.closed_cycle_bps: List[Decimal] = []

    # ================================================================== configuración
    def register_instrument(self, inst: InstrumentInfo, fee: FeeRate) -> bool:
        with self._lock:
            roundtrip = fee.maker_fee_bps * 2
            step = max(self.strategy.step_min_bps, self.strategy.k_fee_multiplier * roundtrip)
            expected_net = step - roundtrip
            if expected_net < self.strategy.min_expected_net_bps:
                logger.warning(f"[{inst.symbol}] Beneficio neto esperado {expected_net:.1f} bps < "
                               f"{self.strategy.min_expected_net_bps} bps. Par NO operado.")
                return False
            self.instruments[inst.symbol] = inst
            self.fees[inst.symbol] = fee
            self.fee_floor_step_bps[inst.symbol] = step
            logger.info(f"[{inst.symbol}] Step mínimo={step:.1f} bps | comisiones ida+vuelta={roundtrip:.1f} bps | "
                        f"neto esperado/ciclo=+{expected_net:.1f} bps | modo={self.strategy.step_mode}")
            return True

    def current_step_bps(self, symbol: str) -> Decimal:
        floor = self.fee_floor_step_bps[symbol]
        if self.strategy.step_mode != "adaptive":
            return floor
        sigma = self.risk.realized_vol_1m_bps(symbol)
        if sigma is None:
            return floor
        target = self.strategy.vol_multiplier * sigma * Decimal(str(math.sqrt(self.strategy.vol_horizon_minutes)))
        return min(max(floor, target), max(floor, self.strategy.step_max_bps))

    # ================================================================== precios y contabilidad
    def update_mid_price(self, symbol: str, mid: Decimal, ts_ms: Optional[int] = None) -> None:
        with self._lock:
            self.mids[symbol] = mid
            self.risk.record_price(symbol, mid, ts_ms)

    def _clock(self, symbol: str) -> int:
        return self.risk.last_ts.get(symbol, now_ms())

    def inventory_value(self, symbol: Optional[str] = None) -> Decimal:
        v = ZERO
        for lot in self.open_lots.values():
            if symbol is None or lot.symbol == symbol:
                v += lot.net_qty * self.mids.get(lot.symbol, lot.buy_price)
        for sym, q in self.residual_qty.items():
            if (symbol is None or sym == symbol) and q > 0:
                v += q * self.mids.get(sym, ZERO)
        return v

    def calculate_mark_to_market_equity(self) -> Decimal:
        """Equity = caja + inventario (lotes + polvo) a precio mid. PnL latente = valor - coste exacto."""
        with self._lock:
            value = self.inventory_value()
            cost = sum((l.cost_quote for l in self.open_lots.values()), ZERO) + sum(self.residual_cost.values(), ZERO)
            self.metrics.unrealized_pnl = value - cost
            self.metrics.mark_to_market_equity = self.metrics.cash_balance + value
            return self.metrics.mark_to_market_equity

    def reserved_cash(self) -> Decimal:
        return sum((o.price * o.remaining_qty for o in self.active_orders.values()
                    if o.side == OrderSide.BUY and o.status.is_active), ZERO)

    def pending_buy_notional(self, symbol: Optional[str] = None) -> Decimal:
        return sum((o.price * o.remaining_qty for o in self.active_orders.values()
                    if o.side == OrderSide.BUY and o.status.is_active and (symbol is None or o.symbol == symbol)), ZERO)

    # ================================================================== planificación
    def plan(self, symbol: str) -> Tuple[List[Order], List[Order]]:
        """Devuelve (órdenes_a_colocar, órdenes_a_cancelar) para un par."""
        with self._lock:
            inst = self.instruments.get(symbol)
            mid = self.mids.get(symbol)
            if inst is None or not mid or mid <= 0:
                return [], []
            my_buys = [o for o in self.active_orders.values()
                       if o.symbol == symbol and o.side == OrderSide.BUY and o.status.is_active]

            # 0) autocuración: todo lote abierto debe tener su venta viva (tras cancelación/rechazo/reinicio).
            #    Las ventas se mantienen SIEMPRE, también con kill switch o filtro de tendencia activos.
            to_place: List[Order] = []
            for lot in list(self.open_lots.values()):
                if lot.symbol == symbol and (not lot.sell_order_link_id or lot.sell_order_link_id not in self.active_orders):
                    to_place.append(self._new_sell_for_lot(lot, lot.target_sell_price))

            equity = self.calculate_mark_to_market_equity()
            killed, _ = self.risk.check_drawdown_kill_switch(equity)
            if killed:
                return to_place, my_buys

            if not self.risk.update_trend_filter(symbol):
                return to_place, (my_buys if self.risk.config.cancel_buys_on_pause else [])

            step = self.current_step_bps(symbol)
            step_pct = step / BPS

            # 1) re-centrado: compras demasiado lejos del precio actual inmovilizan caja -> cancelar
            lowest_allowed = mid * (1 - (Decimal(self.strategy.num_levels) + self.strategy.recenter_buffer_levels) * step_pct)
            to_cancel = [o for o in my_buys if o.status != OrderStatus.PENDING and (o.price < lowest_allowed or o.price >= mid)]
            cancel_ids = {o.order_link_id for o in to_cancel}
            live_buys = [o for o in my_buys if o.order_link_id not in cancel_ids]

            # 2) tamaño y caja disponible (las órdenes a cancelar aún reservan caja hasta confirmarse)
            lot_quote, levels = self.risk.compute_order_size_quote(symbol, equity, self.strategy.num_levels, inst)
            if levels == 0:
                return to_place, to_cancel
            available = self.metrics.cash_balance - self.reserved_cash()

            pair_exposure = self.inventory_value(symbol) + self.pending_buy_notional(symbol)
            total_exposure = self.inventory_value() + self.pending_buy_notional()
            occupied = [o.price for o in live_buys] + [l.buy_price for l in self.open_lots.values() if l.symbol == symbol]
            band = step_pct / 2

            for i in range(1, levels + 1):
                p = quantize_price_down(mid * (1 - Decimal(i) * step_pct), inst.tick_size)
                if p <= 0:
                    break
                if any(abs(p - q) <= q * band for q in occupied):
                    continue
                qty = quantize_qty_down(lot_quote / p, inst.qty_step)
                cost = qty * p
                if qty < inst.min_order_qty or cost < inst.min_order_amt:
                    continue
                if cost > available:
                    break
                ok, _ = self.risk.check_inventory_limits(symbol, pair_exposure, total_exposure, equity, cost)
                if not ok:
                    break
                o = Order(order_link_id=new_link_id("b"), symbol=symbol, side=OrderSide.BUY, price=p, qty=qty,
                          status=OrderStatus.PENDING, step_bps=step,
                          created_time=self._clock(symbol), updated_time=self._clock(symbol))
                self.active_orders[o.order_link_id] = o
                self.db.save_order(o)
                to_place.append(o)
                occupied.append(p)
                available -= cost
                pair_exposure += cost
                total_exposure += cost
            return to_place, to_cancel

    # ================================================================== eventos de órdenes
    def mark_order_live(self, link_id: str, order_id: Optional[str] = None) -> None:
        with self._lock:
            o = self.active_orders.get(link_id)
            if o and o.status == OrderStatus.PENDING:
                o.status = OrderStatus.NEW
                o.order_id = order_id or o.order_id
                self.db.save_order(o)

    def on_order_rejected(self, link_id: str) -> None:
        with self._lock:
            o = self.active_orders.pop(link_id, None)
            if not o:
                return
            o.status = OrderStatus.REJECTED
            self.db.save_order(o)
            if o.side == OrderSide.SELL and o.lot_id and o.lot_id in self.open_lots:
                self.lots_by_sell_id.pop(link_id, None)
                self.open_lots[o.lot_id].sell_order_link_id = None

    def on_order_cancelled(self, link_id: str) -> List[Order]:
        """Cancelación confirmada. Si la compra tenía fills parciales, se materializa su lote."""
        with self._lock:
            o = self.active_orders.pop(link_id, None)
            if not o:
                return []
            o.status = OrderStatus.CANCELLED
            o.updated_time = self._clock(o.symbol)
            self.db.save_order(o)
            if o.side == OrderSide.BUY and link_id in self.buy_pool:
                return self._materialize_lot(o, final=True)
            if o.side == OrderSide.SELL and o.lot_id in self.open_lots:
                self.lots_by_sell_id.pop(link_id, None)
                self.open_lots[o.lot_id].sell_order_link_id = None
            return []

    def on_order_filled(self, order_link_id: str, exec_price: Decimal, exec_qty: Decimal, exec_fee: Decimal,
                        fee_coin: str, is_partial: bool = False, ts_ms: Optional[int] = None,
                        exec_id: Optional[str] = None) -> Optional[Order]:
        """Compatibilidad: devuelve la primera orden de venta generada (si la hay)."""
        out = self.on_fill(order_link_id, exec_price, exec_qty, exec_fee, fee_coin, ts_ms, exec_id)
        return out[0] if out else None

    def on_fill(self, link_id: str, exec_price: Decimal, exec_qty: Decimal, exec_fee: Decimal, fee_coin: str,
                ts_ms: Optional[int] = None, exec_id: Optional[str] = None) -> List[Order]:
        """Procesa UNA ejecución (posiblemente parcial). Idempotente si se pasa exec_id."""
        with self._lock:
            o = self.active_orders.get(link_id) or self.db.get_order(link_id)
            if o is None:
                logger.error(f"Ejecución de orden desconocida {link_id}: ignorada (revisar manualmente)")
                return []
            ts = ts_ms if ts_ms is not None else self._clock(o.symbol)
            if exec_id is not None and not self.db.register_execution(exec_id, link_id, ts):
                return []  # ya contabilizada (WS + REST)
            if exec_qty <= 0:
                return []

            inst = self.instruments[o.symbol]
            o.cum_exec_qty += exec_qty
            o.cum_exec_value += exec_qty * exec_price
            o.cum_exec_fee += exec_fee
            o.fee_coin = fee_coin
            o.updated_time = ts
            done = o.cum_exec_qty >= o.qty
            o.status = OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED
            if done:
                self.active_orders.pop(link_id, None)
            else:
                self.active_orders[link_id] = o
            self.db.save_order(o)
            self.metrics.total_fills += 1

            if o.side == OrderSide.BUY:
                result = self._on_buy_fill(o, inst, exec_price, exec_qty, exec_fee, fee_coin, done)
            else:
                result = self._on_sell_fill(o, inst, exec_price, exec_qty, exec_fee, fee_coin, ts, done)
            self.persist_state()
            return result

    # ------------------------------------------------------------------ compras
    def _on_buy_fill(self, o: Order, inst: InstrumentInfo, price: Decimal, qty: Decimal, fee: Decimal,
                     fee_coin: str, done: bool) -> List[Order]:
        gross_cost = price * qty
        self.metrics.cash_balance -= gross_cost
        if fee_coin == inst.base_coin:
            net = qty - fee
            self.metrics.total_fees_quote += fee * price
            fee_base = fee
        else:  # comisión cobrada en moneda cotizada
            net = qty
            self.metrics.cash_balance -= fee
            gross_cost += fee
            self.metrics.total_fees_quote += fee
            fee_base = ZERO
        pool = self.buy_pool.setdefault(o.order_link_id, [ZERO, ZERO, ZERO, ZERO])
        pool[0] += net
        pool[1] += gross_cost
        pool[2] += qty
        pool[3] += fee_base
        return self._materialize_lot(o, final=done)

    def _sell_target(self, symbol: str, avg_buy: Decimal, unit_cost: Decimal, step_bps: Decimal) -> Decimal:
        inst, fee = self.instruments[symbol], self.fees[symbol]
        by_step = avg_buy * (1 + step_bps / BPS)
        floor = unit_cost * (1 + self.strategy.min_expected_net_bps / BPS) / (1 - fee.maker_fee_rate)
        return quantize_price_up(max(by_step, floor), inst.tick_size)

    def _materialize_lot(self, o: Order, final: bool) -> List[Order]:
        """Convierte los fills acumulados de una compra en un lote vendible cuando cumple los mínimos."""
        pool = self.buy_pool.get(o.order_link_id)
        if not pool or pool[0] <= 0:
            self.buy_pool.pop(o.order_link_id, None)
            return []
        inst = self.instruments[o.symbol]
        sym = o.symbol
        total_qty = pool[0] + self.residual_qty.get(sym, ZERO)
        total_cost = pool[1] + self.residual_cost.get(sym, ZERO)
        unit_cost = total_cost / total_qty
        sell_qty = quantize_qty_down(total_qty, inst.qty_step)
        avg_buy = o.cum_exec_value / o.cum_exec_qty
        step = o.step_bps if o.step_bps > 0 else self.fee_floor_step_bps[sym]
        target = self._sell_target(sym, avg_buy, unit_cost, step)

        sellable = sell_qty >= inst.min_order_qty and sell_qty * target >= inst.min_order_amt
        if not sellable:
            if final:  # la compra terminó y no llega a mínimos: pasa a polvo con su coste exacto
                self.residual_qty[sym] = total_qty
                self.residual_cost[sym] = total_cost
                self.buy_pool.pop(o.order_link_id, None)
                logger.warning(f"[{sym}] Fill de {pool[0]} no alcanza mínimos de venta: acumulado como polvo.")
            return []
        if not final:
            # fill parcial vendible: se crea lote solo por lo ejecutado; el resto de la orden sigue viva
            pass

        lot_cost = sell_qty * unit_cost
        self.residual_qty[sym] = total_qty - sell_qty
        self.residual_cost[sym] = total_cost - lot_cost
        lot = Lot(
            lot_id=f"lot-{o.order_link_id}-{int(o.cum_exec_qty / inst.qty_step)}",
            symbol=sym, buy_order_link_id=o.order_link_id, buy_price=avg_buy, buy_qty=pool[2],
            fee_base_coin=pool[3], net_qty=sell_qty, cost_quote=lot_cost, target_sell_price=target,
            status=LotStatus.OPEN, opened_at=o.updated_time,
        )
        self.buy_pool.pop(o.order_link_id, None)
        self.open_lots[lot.lot_id] = lot
        sell = self._new_sell_for_lot(lot, target)
        logger.info(f"[{sym}] COMPRA {pool[2]} @ {avg_buy} | fee base {pool[3]} | lote {sell_qty} "
                    f"(coste {lot_cost:.4f}) -> venta {target}")
        return [sell]

    def _new_sell_for_lot(self, lot: Lot, price: Decimal) -> Order:
        sell = Order(order_link_id=new_link_id("s"), symbol=lot.symbol, side=OrderSide.SELL, price=price,
                     qty=lot.net_qty, status=OrderStatus.PENDING, lot_id=lot.lot_id,
                     created_time=self._clock(lot.symbol), updated_time=self._clock(lot.symbol))
        lot.sell_order_link_id = sell.order_link_id
        lot.target_sell_price = price
        self.active_orders[sell.order_link_id] = sell
        self.lots_by_sell_id[sell.order_link_id] = lot
        self.db.save_lot(lot)
        self.db.save_order(sell)
        return sell

    def reprice_sell(self, link_id: str, best_bid: Decimal) -> Optional[Order]:
        """Venta PostOnly rechazada (el mercado ya está por encima del objetivo): se recoloca 1 tick sobre el bid,
        que es >= objetivo, así que nunca se vende por debajo del objetivo."""
        with self._lock:
            lot = self.lots_by_sell_id.get(link_id)
            self.on_order_rejected(link_id)
            if not lot or lot.lot_id not in self.open_lots:
                return None
            inst = self.instruments[lot.symbol]
            price = max(lot.target_sell_price, quantize_price_up(best_bid + inst.tick_size, inst.tick_size))
            return self._new_sell_for_lot(lot, price)

    # ------------------------------------------------------------------ ventas
    def _on_sell_fill(self, o: Order, inst: InstrumentInfo, price: Decimal, qty: Decimal, fee: Decimal,
                      fee_coin: str, ts: int, done: bool) -> List[Order]:
        lot = self.open_lots.get(o.lot_id or "") or self.lots_by_sell_id.get(o.order_link_id)
        if lot is None:
            logger.error(f"Venta {o.order_link_id} sin lote asociado: revisar manualmente")
            return []
        qty = min(qty, lot.net_qty)
        proceeds = price * qty
        fee_quote = fee if fee_coin == inst.quote_coin else fee * price
        slice_cost = lot.cost_quote * qty / lot.net_qty if lot.net_qty > 0 else ZERO
        pnl = proceeds - fee_quote - slice_cost

        self.metrics.cash_balance += proceeds - fee_quote
        self.metrics.realized_pnl += pnl
        self.metrics.total_fees_quote += fee_quote
        lot.gross_profit += proceeds - slice_cost
        lot.net_profit += pnl
        lot.sell_fee += fee_quote
        lot.cost_quote -= slice_cost
        lot.net_qty -= qty            # en lotes abiertos net_qty = cantidad aún por vender
        lot.executed_sell_price = price
        self._sold_cost[lot.lot_id] = self._sold_cost.get(lot.lot_id, ZERO) + slice_cost

        if done or lot.net_qty <= 0:
            liquidation = o.order_link_id.startswith(f"{LINK_PREFIX}x")
            lot.status = LotStatus.LIQUIDATED if liquidation else LotStatus.CLOSED
            lot.closed_at = ts
            invested = self._sold_cost.pop(lot.lot_id, ZERO) + lot.cost_quote
            if lot.net_qty > 0:  # resto no vendido (venta cancelada tras parcial): pasa a polvo con su coste
                self.residual_qty[lot.symbol] = self.residual_qty.get(lot.symbol, ZERO) + lot.net_qty
                self.residual_cost[lot.symbol] = self.residual_cost.get(lot.symbol, ZERO) + lot.cost_quote
                lot.net_qty, lot.cost_quote = ZERO, ZERO
            self.open_lots.pop(lot.lot_id, None)
            self.lots_by_sell_id.pop(o.order_link_id, None)
            bps = lot.net_profit / invested * BPS if invested > 0 else ZERO
            if liquidation:
                self.metrics.liquidated_lots += 1
            else:
                self.metrics.completed_cycles += 1
                self.hold_times_ms.append(ts - lot.opened_at)
                self.closed_cycle_bps.append(bps)
            logger.info(f"[{lot.symbol}] {'LIQUIDADO' if liquidation else 'CICLO CERRADO'} {lot.lot_id}: "
                        f"neto {lot.net_profit:+.4f} ({bps:+.1f} bps) | realizado acumulado {self.metrics.realized_pnl:+.4f}")
        self.db.save_lot(lot)
        return []

    # ================================================================== kill switch: liquidación
    def emergency_liquidation_orders(self) -> List[Order]:
        """Órdenes de venta a mercado (taker) para todos los lotes abiertos. Solo si emergency_liquidate=True."""
        with self._lock:
            out: List[Order] = []
            for lot in list(self.open_lots.values()):
                if lot.sell_order_link_id and lot.sell_order_link_id in self.active_orders:
                    continue  # el driver debe cancelar antes la venta límite
                o = Order(order_link_id=new_link_id("x"), symbol=lot.symbol, side=OrderSide.SELL,
                          price=self.mids.get(lot.symbol, lot.buy_price), qty=lot.net_qty,
                          status=OrderStatus.PENDING, lot_id=lot.lot_id)
                lot.sell_order_link_id = o.order_link_id
                self.active_orders[o.order_link_id] = o
                self.lots_by_sell_id[o.order_link_id] = lot
                self.db.save_order(o)
                out.append(o)
            return out

    # ================================================================== persistencia de estado
    def persist_state(self) -> None:
        m = self.metrics
        self.db.set_state("cash", str(m.cash_balance))
        self.db.set_state("realized_pnl", str(m.realized_pnl))
        self.db.set_state("fees", str(m.total_fees_quote))
        self.db.set_state("cycles", str(m.completed_cycles))
        self.db.set_state("peak_equity", str(self.risk.peak_equity))
        self.db.set_state("kill_switch", "1" if self.risk.kill_switch_triggered else "0")
        for s in self.instruments:
            self.db.set_state(f"res_qty:{s}", str(self.residual_qty.get(s, ZERO)))
            self.db.set_state(f"res_cost:{s}", str(self.residual_cost.get(s, ZERO)))
        for link, pool in self.buy_pool.items():
            self.db.set_state(f"pool:{link}", ",".join(str(x) for x in pool))

    def load_state(self) -> None:
        """Restaura caja, PnL, polvo, lotes y órdenes activas desde SQLite (arranque tras reinicio)."""
        with self._lock:
            g = self.db.get_state
            if g("cash") is not None:
                self.metrics.cash_balance = Decimal(g("cash"))
                self.metrics.realized_pnl = Decimal(g("realized_pnl", "0"))
                self.metrics.total_fees_quote = Decimal(g("fees", "0"))
                self.metrics.completed_cycles = int(g("cycles", "0"))
                self.risk.peak_equity = Decimal(g("peak_equity", "0"))
                self.risk.kill_switch_triggered = g("kill_switch", "0") == "1"
            for s, v in self.db.get_state_prefix("res_qty:").items():
                self.residual_qty[s] = Decimal(v)
            for s, v in self.db.get_state_prefix("res_cost:").items():
                self.residual_cost[s] = Decimal(v)
            for lot in self.db.get_open_lots():
                self.open_lots[lot.lot_id] = lot
            for o in self.db.get_active_orders():
                self.active_orders[o.order_link_id] = o
                if o.side == OrderSide.SELL and o.lot_id in self.open_lots:
                    self.lots_by_sell_id[o.order_link_id] = self.open_lots[o.lot_id]
            for link, v in self.db.get_state_prefix("pool:").items():
                if link in self.active_orders:
                    self.buy_pool[link] = [Decimal(x) for x in v.split(",")]

    # ================================================================== reconciliación con el exchange
    def reconcile(self, exchange_open_orders: List[dict]) -> Tuple[List[Tuple[str, str]], List[Order]]:
        """
        Cuadra el estado local con las órdenes abiertas reales. Debe llamarse DESPUÉS de reproducir
        (on_fill con exec_id) las ejecuciones ocurridas mientras el bot estaba parado.

        Devuelve:
          - órdenes del bot vivas en el exchange que el estado local no reconoce -> (symbol, orderLinkId) a cancelar
          - ventas a (re)colocar para lotes abiertos sin orden de venta viva
        Nunca crea órdenes duplicadas: una orden local viva en el exchange se conserva tal cual.
        """
        with self._lock:
            live = {o.get("orderLinkId"): o for o in exchange_open_orders if o.get("orderLinkId")}
            to_cancel: List[Tuple[str, str]] = []
            for link, ex in live.items():
                if link.startswith(LINK_PREFIX) and link not in self.active_orders:
                    to_cancel.append((ex.get("symbol", ""), link))
            for link, o in list(self.active_orders.items()):
                if link in live:
                    o.status = OrderStatus.NEW if o.cum_exec_qty == 0 else OrderStatus.PARTIALLY_FILLED
                    o.order_id = live[link].get("orderId", o.order_id)
                    self.db.save_order(o)
                else:
                    # ya no está viva y sus fills (si los hubo) se reprodujeron antes: se da por cancelada
                    self.on_order_cancelled(link)
            to_place: List[Order] = []
            for lot in list(self.open_lots.values()):
                sid = lot.sell_order_link_id
                if not sid or sid not in self.active_orders:
                    to_place.append(self._new_sell_for_lot(lot, lot.target_sell_price))
            logger.info(f"Reconciliación: {len(self.open_lots)} lotes abiertos, {len(self.active_orders)} órdenes vivas, "
                        f"{len(to_cancel)} órdenes huérfanas a cancelar, {len(to_place)} ventas a recolocar.")
            return to_cancel, to_place
