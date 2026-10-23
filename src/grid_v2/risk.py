"""
src/grid_v2/risk.py — Gestión de riesgo: topes de inventario a mercado, filtro de pendiente,
volatilidad realizada, tamaño de orden y kill switch por drawdown.

IMPORTANTE: el reloj del filtro de tendencia es el de los DATOS (timestamp del último precio),
no time.time(). Así el enfriamiento funciona igual en live, paper y backtest.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from decimal import Decimal
from typing import Deque, Dict, Optional, Tuple

from src.grid_v2.config import RiskConfig
from src.grid_v2.models import InstrumentInfo, now_ms

logger = logging.getLogger("Risk")

SLOPE_WINDOW_MS = 300_000       # 5 minutos
SLOPE_MIN_SPAN_MS = 240_000     # no se evalúa pendiente sin al menos 4 minutos de historia


class RiskManager:
    def __init__(self, config: RiskConfig, vol_lookback_minutes: int = 240):
        self.config = config
        self.vol_lookback_minutes = vol_lookback_minutes
        self.mid_history: Dict[str, Deque[Tuple[int, Decimal]]] = {}
        self.minute_closes: Dict[str, Deque[float]] = {}
        self._cur_minute: Dict[str, Tuple[int, float]] = {}
        self.last_ts: Dict[str, int] = {}
        self.slope_paused: Dict[str, bool] = {}
        self.slope_recovered_at: Dict[str, int] = {}
        self.peak_equity = Decimal("0")
        self.kill_switch_triggered = False

    # ------------------------------------------------------------------ precios
    def record_price(self, symbol: str, mid: Decimal, ts_ms: Optional[int] = None) -> None:
        ts = ts_ms if ts_ms is not None else now_ms()
        if symbol not in self.mid_history:
            self.mid_history[symbol] = deque()
            self.minute_closes[symbol] = deque(maxlen=self.vol_lookback_minutes + 1)
            self.slope_paused[symbol] = False
        prev = self.last_ts.get(symbol)
        if prev is not None and ts < prev:
            return  # dato desordenado: se ignora
        self.last_ts[symbol] = ts
        h = self.mid_history[symbol]
        h.append((ts, mid))
        cutoff = ts - 2 * SLOPE_WINDOW_MS
        while h and h[0][0] < cutoff:
            h.popleft()

        minute = ts // 60_000
        cur = self._cur_minute.get(symbol)
        if cur is not None and minute != cur[0]:
            self.minute_closes[symbol].append(cur[1])
        self._cur_minute[symbol] = (minute, float(mid))

    def get_5m_slope_bps(self, symbol: str) -> Decimal:
        h = self.mid_history.get(symbol)
        if not h or len(h) < 2:
            return Decimal("0")
        now_ts, now_mid = h[-1]
        if now_ts - h[0][0] < SLOPE_MIN_SPAN_MS or now_mid <= 0:
            return Decimal("0")
        target = now_ts - SLOPE_WINDOW_MS
        old_mid = h[0][1]
        for ts, m in h:          # último punto con ts <= hace 5 minutos
            if ts <= target:
                old_mid = m
            else:
                break
        return (now_mid - old_mid) / old_mid * Decimal("10000")

    def realized_vol_1m_bps(self, symbol: str) -> Optional[Decimal]:
        """Desviación típica de los retornos logarítmicos de 1 minuto, en bps."""
        closes = self.minute_closes.get(symbol)
        if not closes or len(closes) < max(30, self.vol_lookback_minutes // 2):
            return None
        c = list(closes)
        rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]
        if len(rets) < 2:
            return None
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        return Decimal(str(math.sqrt(var) * 1e4))

    # ------------------------------------------------------------------ filtro de tendencia
    def update_trend_filter(self, symbol: str) -> bool:
        """True = compras nuevas permitidas. False = pausadas (las ventas siguen activas)."""
        if not self.config.trend_filter_enabled:
            return True
        slope = self.get_5m_slope_bps(symbol)
        threshold = -self.config.slope_threshold_bps
        now = self.last_ts.get(symbol, now_ms())
        paused = self.slope_paused.get(symbol, False)

        if slope < threshold:
            if not paused:
                logger.warning(f"[{symbol}] Filtro de tendencia: pendiente 5m={slope:.1f} bps < {threshold:.1f}. Compras PAUSADAS.")
            self.slope_paused[symbol] = True
            self.slope_recovered_at.pop(symbol, None)
            return False

        if paused:
            since = self.slope_recovered_at.get(symbol)
            if since is None:
                self.slope_recovered_at[symbol] = now
                logger.debug(f"[{symbol}] Pendiente recuperada ({slope:.1f} bps). Enfriamiento {self.config.slope_cooldown_seconds}s.")
                return False
            if now - since < self.config.slope_cooldown_seconds * 1000:
                return False
            self.slope_paused[symbol] = False
            self.slope_recovered_at.pop(symbol, None)
            logger.info(f"[{symbol}] Enfriamiento completado. Compras REANUDADAS.")
        return True

    def is_paused(self, symbol: str) -> bool:
        return self.slope_paused.get(symbol, False)

    # ------------------------------------------------------------------ inventario
    def check_inventory_limits(
        self,
        symbol: str,
        pair_exposure_quote: Decimal,
        total_exposure_quote: Decimal,
        total_equity: Decimal,
        additional_order_quote: Decimal = Decimal("0"),
    ) -> Tuple[bool, str]:
        """
        pair_exposure_quote / total_exposure_quote = inventario a precio mid + compras pendientes.
        Las compras pendientes cuentan porque, si se llenan, se convierten en inventario.
        """
        if total_equity <= 0:
            return False, "Equity nulo o negativo"
        new_pair = pair_exposure_quote + additional_order_quote
        pair_limit = total_equity * self.config.max_inv_pct_pair
        if new_pair > pair_limit:
            return False, f"Tope de inventario de {symbol} excedido: {new_pair:.2f} > {pair_limit:.2f}"
        new_total = total_exposure_quote + additional_order_quote
        global_limit = total_equity * self.config.max_inv_pct_global
        if new_total > global_limit:
            return False, f"Tope de inventario global excedido: {new_total:.2f} > {global_limit:.2f}"
        return True, "OK"

    # ------------------------------------------------------------------ tamaño
    def compute_order_size_quote(
        self, symbol: str, total_equity: Decimal, num_levels: int, inst: InstrumentInfo
    ) -> Tuple[Decimal, int]:
        """lot = equity / (niveles * factor). Si lot < minOrderAmt reduce niveles; si no cabe ni 1, desactiva."""
        factor = self.config.order_size_factor
        levels = num_levels
        while levels >= 1:
            lot = total_equity / (Decimal(levels) * factor)
            # margen del 2 % para que el redondeo de qty no deje la orden bajo el mínimo
            if lot >= inst.min_order_amt * Decimal("1.02"):
                return lot, levels
            levels -= 1
        logger.warning(f"[{symbol}] Equity {total_equity:.2f} insuficiente para minOrderAmt {inst.min_order_amt}. Par desactivado.")
        return Decimal("0"), 0

    # ------------------------------------------------------------------ kill switch
    def check_drawdown_kill_switch(self, equity: Decimal) -> Tuple[bool, Decimal]:
        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity <= 0:
            return False, Decimal("0")
        dd = (self.peak_equity - equity) / self.peak_equity * Decimal("100")
        if dd >= self.config.max_drawdown_pct and not self.kill_switch_triggered:
            logger.critical(f"KILL SWITCH: drawdown {dd:.2f}% >= {self.config.max_drawdown_pct}%. Compras canceladas.")
            self.kill_switch_triggered = True
        return self.kill_switch_triggered, dd
