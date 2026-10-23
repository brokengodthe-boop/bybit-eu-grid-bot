"""
src/grid_v2/backtest.py — Backtest con la MISMA lógica del motor (GridEngine) que paper y live.

Datos: velas de 1 minuto de Bybit EU Spot (la API pública no ofrece trades históricos para pares EUR).
Para no inflar resultados con velas, el simulador es conservador:
  - Recorrido intra-vela: vela alcista O->L->H->C, bajista O->H->L->C. Una venta creada en una vela solo
    puede llenarse si el máximo llega DESPUÉS del mínimo en ese recorrido (sin mirar al futuro).
  - Una orden límite solo se llena si el precio la ATRAVIESA `fill_through_ticks` ticks (aproxima la cola).
  - PostOnly: una compra a precio >= precio actual o una venta <= precio actual se rechaza (nunca taker).
  - Comisiones maker reales de la cuenta; compra cobrada en moneda base, venta en cotizada.
  - Liquidación de emergencia (si está activada) a precio de cierre con comisión taker.

Una cartera = un único motor con caja compartida entre pares, exactamente como en live.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from pybit.unified_trading import HTTP

from src.grid_v2.config import RiskConfig, StrategyConfig
from src.grid_v2.grid_engine import GridEngine
from src.grid_v2.models import FeeRate, InstrumentInfo, Order, OrderSide, OrderStatus
from src.grid_v2.persistence import NullDatabase
from src.grid_v2.risk import RiskManager

logger = logging.getLogger("Backtest")
ZERO = Decimal("0")
Bar = Tuple[int, Decimal, Decimal, Decimal, Decimal]  # ts, open, high, low, close


# ====================================================================== datos
def fetch_klines(symbol: str, interval: str, days: int, cache_dir: str = "data/backtest_cache",
                 tld: str = "eu") -> List[Bar]:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol}_{interval}m_{days}d.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 12 * 3600:
        with open(path, "r") as f:
            raw = json.load(f)
    else:
        http = HTTP(testnet=False, tld=tld)
        step_ms = int(interval) * 60_000
        end = (int(time.time() * 1000) // step_ms) * step_ms
        start = end - days * 86_400_000
        rows: Dict[int, list] = {}
        cursor = end
        while cursor > start:
            for attempt in range(5):
                try:
                    r = http.get_kline(category="spot", symbol=symbol, interval=interval, end=cursor, limit=1000)
                    break
                except Exception as e:  # red/rate-limit: reintento acotado con backoff
                    if attempt == 4:
                        raise
                    time.sleep(1.5 * (attempt + 1))
                    logger.warning(f"[{symbol}] reintento kline ({e})")
            data = r.get("result", {}).get("list", [])
            if not data:
                break
            for b in data:
                rows[int(b[0])] = b[:5]
            oldest = min(int(b[0]) for b in data)
            if oldest >= cursor:
                break
            cursor = oldest - step_ms
            time.sleep(0.06)
        raw = [rows[k] for k in sorted(rows) if k >= start]
        with open(path, "w") as f:
            json.dump(raw, f)
    return [(int(b[0]), Decimal(b[1]), Decimal(b[2]), Decimal(b[3]), Decimal(b[4])) for b in raw]


# ====================================================================== simulador
class SimVenue:
    """Libro simulado: aplica las acciones del motor y genera fills conservadores."""

    def __init__(self, engine: GridEngine, fill_through_ticks: int):
        self.e = engine
        self.ft = fill_through_ticks
        self.rejected_postonly = 0

    def apply(self, symbol: str, price: Decimal) -> None:
        to_place, to_cancel = self.e.plan(symbol)
        for o in to_cancel:
            self.e.on_order_cancelled(o.order_link_id)
        for o in to_place:
            self._place(o, price)

    def _place(self, o: Order, price: Decimal) -> None:
        if o.side == OrderSide.BUY and o.price >= price:
            self.rejected_postonly += 1
            self.e.on_order_rejected(o.order_link_id)
        elif o.side == OrderSide.SELL and o.price <= price:
            self.rejected_postonly += 1
            inst = self.e.instruments[o.symbol]
            new = self.e.reprice_sell(o.order_link_id, best_bid=price)
            if new is not None:
                self.e.mark_order_live(new.order_link_id)
        else:
            self.e.mark_order_live(o.order_link_id)

    def sweep(self, symbol: str, a: Decimal, b: Decimal, ts: int) -> None:
        """Mueve el precio de a hacia b llenando las órdenes atravesadas en orden de precio."""
        inst = self.e.instruments[symbol]
        fee = self.e.fees[symbol]
        margin = inst.tick_size * self.ft
        if b < a:   # bajada: compras de mayor a menor precio
            buys = sorted((o for o in self.e.active_orders.values()
                           if o.symbol == symbol and o.side == OrderSide.BUY and o.status == OrderStatus.NEW
                           and b <= o.price - margin), key=lambda o: -o.price)
            for o in buys:
                q = o.remaining_qty
                for s in self.e.on_fill(o.order_link_id, o.price, q, q * fee.maker_fee_rate, inst.base_coin, ts_ms=ts):
                    self._place(s, o.price)
        elif b > a:  # subida: ventas de menor a mayor precio
            sells = sorted((o for o in self.e.active_orders.values()
                            if o.symbol == symbol and o.side == OrderSide.SELL and o.status == OrderStatus.NEW
                            and b >= o.price + margin), key=lambda o: o.price)
            for o in sells:
                q = o.remaining_qty
                self.e.on_fill(o.order_link_id, o.price, q, o.price * q * fee.maker_fee_rate, inst.quote_coin, ts_ms=ts)

    def liquidate(self, symbol: str, price: Decimal, ts: int) -> None:
        inst, fee = self.e.instruments[symbol], self.e.fees[symbol]
        for o in [o for o in self.e.active_orders.values() if o.symbol == symbol and o.side == OrderSide.SELL]:
            self.e.on_order_cancelled(o.order_link_id)
        for o in self.e.emergency_liquidation_orders():
            if o.symbol != symbol:
                continue
            q = o.qty
            self.e.on_fill(o.order_link_id, price, q, price * q * fee.taker_fee_rate, inst.quote_coin, ts_ms=ts)


def run_portfolio(
    bars: Dict[str, List[Bar]],
    instruments: Dict[str, InstrumentInfo],
    fees: Dict[str, FeeRate],
    strategy: StrategyConfig,
    risk_cfg: RiskConfig,
    capital: Decimal,
    fill_through_ticks: int = 1,
    t_start: Optional[int] = None,
    t_end: Optional[int] = None,
    warmup_minutes: int = 240,
) -> Dict:
    """Simula la cartera completa en [t_start, t_end). Las velas previas sirven de calentamiento (sin operar)."""
    strat = replace(strategy, initial_capital_eur=capital, pairs=list(bars))
    risk = RiskManager(risk_cfg, vol_lookback_minutes=strat.vol_lookback_minutes)
    engine = GridEngine(strat, risk, NullDatabase())
    active = [s for s in bars if engine.register_instrument(instruments[s], fees[s])]
    venue = SimVenue(engine, fill_through_ticks)

    timeline: Dict[int, List[Tuple[str, Bar]]] = {}
    for s in active:
        for b in bars[s]:
            if t_end is not None and b[0] >= t_end:
                continue
            timeline.setdefault(b[0], []).append((s, b))
    ts_sorted = sorted(timeline)
    if not ts_sorted:
        return {"error": "sin datos"}
    t0 = t_start if t_start is not None else ts_sorted[0] + warmup_minutes * 60_000
    warm_from = t0 - warmup_minutes * 60_000

    first_px: Dict[str, Decimal] = {}
    last_px: Dict[str, Decimal] = {}
    peak = capital
    max_dd = ZERO
    bars_total = bars_inv = bars_underwater = 0
    exposure_sum = ZERO
    liquidated = False

    for ts in ts_sorted:
        if ts < warm_from:
            continue
        trading = ts >= t0
        for s, (_, o, h, l, c) in timeline[ts]:
            if not trading:
                engine.update_mid_price(s, c, ts + 59_999)
                continue
            first_px.setdefault(s, o)
            engine.update_mid_price(s, o, ts)
            if not liquidated:
                venue.apply(s, o)
            path = (o, l, h, c) if c >= o else (o, h, l, c)
            for a, b in zip(path, path[1:]):
                venue.sweep(s, a, b, ts + 30_000)
            engine.update_mid_price(s, c, ts + 59_999)
            last_px[s] = c
        if not trading:
            continue
        eq = engine.calculate_mark_to_market_equity()
        if engine.risk.kill_switch_triggered and risk_cfg.emergency_liquidate and not liquidated:
            for s in active:
                venue.liquidate(s, engine.mids[s], ts)
            liquidated = True
            eq = engine.calculate_mark_to_market_equity()
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100 if peak > 0 else ZERO)
        bars_total += 1
        inv = engine.inventory_value()
        if inv > 0:
            bars_inv += 1
            if engine.metrics.unrealized_pnl < 0:
                bars_underwater += 1
        exposure_sum += inv / eq if eq > 0 else ZERO

    eq = engine.calculate_mark_to_market_equity()
    m = engine.metrics
    days = (ts_sorted[-1] - t0) / 86_400_000 if ts_sorted[-1] > t0 else 0
    hold_h = [x / 3_600_000 for x in engine.hold_times_ms]
    per_pair = {}
    for s in active:
        per_pair[s] = {
            "hold_return_pct": float((last_px[s] / first_px[s] - 1) * 100) if s in first_px else 0.0,
            "open_lots": sum(1 for lt in engine.open_lots.values() if lt.symbol == s),
        }
    hold_avg = sum(v["hold_return_pct"] for v in per_pair.values()) / max(1, len(per_pair))
    return {
        "days": round(days, 1),
        "initial_capital": float(capital),
        "final_equity": float(eq),
        "return_pct": float((eq / capital - 1) * 100),
        "realized_pnl": float(m.realized_pnl),
        "unrealized_pnl": float(m.unrealized_pnl),
        "fees_paid": float(m.total_fees_quote),
        "cycles": m.completed_cycles,
        "liquidated_lots": m.liquidated_lots,
        "open_lots": len(engine.open_lots),
        "avg_net_bps_per_cycle": float(sum(engine.closed_cycle_bps) / len(engine.closed_cycle_bps)) if engine.closed_cycle_bps else 0.0,
        "max_drawdown_pct": float(max_dd),
        "avg_hold_hours": sum(hold_h) / len(hold_h) if hold_h else 0.0,
        "max_hold_hours": max(hold_h) if hold_h else 0.0,
        "pct_time_with_inventory": 100.0 * bars_inv / max(1, bars_total),
        "pct_time_underwater": 100.0 * bars_underwater / max(1, bars_total),
        "avg_exposure_pct": float(exposure_sum / max(1, bars_total) * 100),
        "postonly_rejects": venue.rejected_postonly,
        "kill_switch": engine.risk.kill_switch_triggered,
        "buy_hold_return_pct": hold_avg,
        "per_pair": per_pair,
    }


def format_report(title: str, r: Dict) -> str:
    if "error" in r:
        return f"{title}: {r['error']}"
    lines = [
        "=" * 78, f" {title}", "=" * 78,
        f" Periodo: {r['days']} días | Capital inicial: {r['initial_capital']:.2f} €",
        f" Equity final (caja + inventario a mid): {r['final_equity']:.2f} €  ({r['return_pct']:+.2f} %)",
        f" PnL REALIZADO: {r['realized_pnl']:+.3f} €   |   PnL LATENTE: {r['unrealized_pnl']:+.3f} €",
        f" Comisiones pagadas: {r['fees_paid']:.3f} €",
        f" Ciclos cerrados: {r['cycles']} | neto medio/ciclo: {r['avg_net_bps_per_cycle']:+.1f} bps | "
        f"lotes liquidados: {r['liquidated_lots']} | lotes abiertos al final: {r['open_lots']}",
        f" Drawdown máximo: {r['max_drawdown_pct']:.2f} % | kill switch: {'SÍ' if r['kill_switch'] else 'no'}",
        f" Tenencia media/máx de un lote: {r['avg_hold_hours']:.1f} h / {r['max_hold_hours']:.1f} h",
        f" Tiempo con inventario: {r['pct_time_with_inventory']:.1f} % | con pérdida latente: {r['pct_time_underwater']:.1f} % "
        f"| exposición media: {r['avg_exposure_pct']:.1f} %",
        f" Referencia comprar y mantener (media de pares): {r['buy_hold_return_pct']:+.2f} %",
        " Por par (variación de precio en el periodo / lotes abiertos al final):",
    ]
    for s, v in r["per_pair"].items():
        lines.append(f"   {s:<9} precio {v['hold_return_pct']:+7.2f} %   lotes abiertos {v['open_lots']}")
    if r["unrealized_pnl"] < 0:
        lines.append(" AVISO: hay lotes con pérdida latente; el capital NO está preservado mientras no se recuperen.")
    return "\n".join(lines)
