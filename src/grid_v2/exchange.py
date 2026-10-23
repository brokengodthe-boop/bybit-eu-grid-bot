"""
src/grid_v2/exchange.py — Adaptador Bybit EU (API v5, solo SPOT).

- Sin parches a funciones internas de pybit: se mide el desfase con /v5/market/time y, si el reloj local
  queda fuera del margen que admite Bybit (timestamp <= server+1000 ms y >= server-recv_window), se
  aborta con instrucciones para sincronizar el reloj del sistema.
- tickSize, qtyStep(basePrecision), minOrderQty, minOrderAmt y comisiones maker/taker se leen por API.
- Órdenes límite siempre PostOnly. Este adaptador NO reintenta con el mismo orderLinkId: devuelve el
  resultado y el driver decide (recalcular precio con backoff, o descartar).
- WebSocket público y privado apuntando al dominio EU.
- Las claves nunca se registran en logs.
"""
from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Tuple

from pybit.unified_trading import HTTP, WebSocket

from src.grid_v2.config import ExchangeConfig
from src.grid_v2.models import FeeRate, InstrumentInfo, OrderSide

logger = logging.getLogger("Exchange")

POSTONLY_HINTS = ("postonly", "post only", "post_only", "willtakeliquidity")
TRANSIENT_HINTS = ("timeout", "timed out", "connection", "too many", "rate limit", "10006", "10016", "server")


class ClockError(RuntimeError):
    pass


class BybitSpotExchange:
    def __init__(self, config: ExchangeConfig, check_clock: bool = True):
        self.config = config
        self._rl_lock = threading.Lock()
        self._last_call = 0.0
        self._min_interval = 1.0 / max(1, config.rate_limit_per_second)
        self.public = HTTP(testnet=config.testnet, tld=config.tld)
        self.clock_offset_ms = self.measure_clock_offset()
        if check_clock:
            self.assert_clock_ok()
        self.session = HTTP(testnet=config.testnet, tld=config.tld, api_key=config.api_key,
                            api_secret=config.api_secret, recv_window=config.recv_window)
        self.instruments: Dict[str, InstrumentInfo] = {}
        self.fees: Dict[str, FeeRate] = {}
        self.ws_public: Optional[WebSocket] = None
        self.ws_private: Optional[WebSocket] = None

    # ------------------------------------------------------------------ utilidades
    def _rate_limit(self) -> None:
        with self._rl_lock:
            wait = self._min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    def _call(self, fn: Callable, *, retries: int = 3, **kwargs) -> dict:
        """Llamada REST con limitador de tasa y reintentos acotados solo para errores transitorios."""
        last: Optional[Exception] = None
        for attempt in range(retries):
            self._rate_limit()
            try:
                resp = fn(**kwargs)
                if resp.get("retCode") == 0:
                    return resp
                msg = str(resp.get("retMsg", ""))
                if any(h in msg.lower() for h in TRANSIENT_HINTS) and attempt < retries - 1:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                return resp
            except Exception as e:  # pybit lanza excepciones para retCode != 0 en algunos casos
                last = e
                text = str(e).lower()
                if any(h in text for h in POSTONLY_HINTS):
                    return {"retCode": -1, "retMsg": str(e)}
                if attempt < retries - 1 and any(h in text for h in TRANSIENT_HINTS):
                    logger.warning(f"Error transitorio en {getattr(fn, '__name__', 'api')}: {e}. Reintento {attempt + 1}")
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                return {"retCode": -1, "retMsg": str(e)}
        return {"retCode": -1, "retMsg": str(last)}

    # ------------------------------------------------------------------ reloj
    def measure_clock_offset(self, samples: int = 3) -> int:
        """offset = hora_servidor - hora_local (ms), usando el mínimo RTT de varias muestras."""
        best: Optional[Tuple[float, int]] = None
        for _ in range(samples):
            t0 = time.time() * 1000
            r = self.public.get_server_time()
            t1 = time.time() * 1000
            server = int(r["result"]["timeNano"]) // 1_000_000
            rtt = t1 - t0
            off = int(server - (t0 + rtt / 2))
            if best is None or rtt < best[0]:
                best = (rtt, off)
        assert best is not None
        logger.info(f"Reloj: desfase servidor-local {best[1]:+d} ms (RTT {best[0]:.0f} ms), recv_window={self.config.recv_window} ms")
        return best[1]

    def assert_clock_ok(self) -> None:
        ahead = -self.clock_offset_ms   # >0: reloj local adelantado
        if ahead > self.config.max_clock_ahead_ms:
            raise ClockError(f"Reloj local adelantado {ahead} ms (Bybit rechaza > 1000 ms). "
                             "Sincroniza Windows: 'w32tm /resync' como administrador.")
        if -ahead > self.config.recv_window - 1000:
            raise ClockError(f"Reloj local atrasado {-ahead} ms, fuera de recv_window={self.config.recv_window}. "
                             "Sincroniza el reloj o aumenta recv_window.")

    # ------------------------------------------------------------------ metadatos
    def get_instrument_info(self, symbol: str) -> InstrumentInfo:
        if symbol in self.instruments:
            return self.instruments[symbol]
        r = self._call(self.public.get_instruments_info, category="spot", symbol=symbol)
        lst = r.get("result", {}).get("list") or []
        if r.get("retCode") != 0 or not lst:
            raise RuntimeError(f"Instrumento {symbol} no disponible: {r.get('retMsg')}")
        i = lst[0]
        if i.get("status") != "Trading":
            raise RuntimeError(f"{symbol} no está en estado Trading ({i.get('status')})")
        lf, pf = i["lotSizeFilter"], i["priceFilter"]
        inst = InstrumentInfo(
            symbol=symbol, base_coin=i["baseCoin"], quote_coin=i["quoteCoin"],
            tick_size=Decimal(pf["tickSize"]), qty_step=Decimal(lf["basePrecision"]),
            min_order_qty=Decimal(lf["minOrderQty"]), min_order_amt=Decimal(lf["minOrderAmt"]),
        )
        self.instruments[symbol] = inst
        logger.info(f"[{symbol}] tick={inst.tick_size} qtyStep={inst.qty_step} minQty={inst.min_order_qty} "
                    f"minAmt={inst.min_order_amt} {inst.quote_coin}")
        return inst

    def get_fee_rate(self, symbol: str) -> FeeRate:
        if symbol in self.fees:
            return self.fees[symbol]
        if not self.config.api_key:
            raise RuntimeError("Se necesitan claves API (.env) para leer la comisión REAL de la cuenta.")
        r = self._call(self.session.get_fee_rates, category="spot", symbol=symbol)
        lst = r.get("result", {}).get("list") or []
        if r.get("retCode") != 0 or not lst:
            raise RuntimeError(f"No se pudo leer la comisión de {symbol}: {r.get('retMsg')}")
        f = FeeRate(symbol=symbol, maker_fee_rate=Decimal(lst[0]["makerFeeRate"]),
                    taker_fee_rate=Decimal(lst[0]["takerFeeRate"]))
        self.fees[symbol] = f
        logger.info(f"[{symbol}] comisión real: maker {f.maker_fee_bps:.1f} bps / taker {f.taker_fee_bps:.1f} bps")
        return f

    # ------------------------------------------------------------------ mercado y cuenta
    def get_best_bid_ask(self, symbol: str) -> Tuple[Decimal, Decimal]:
        r = self._call(self.public.get_orderbook, category="spot", symbol=symbol, limit=1)
        res = r.get("result", {})
        return Decimal(res["b"][0][0]), Decimal(res["a"][0][0])

    def get_mid(self, symbol: str) -> Decimal:
        bid, ask = self.get_best_bid_ask(symbol)
        return (bid + ask) / 2

    def get_balances(self) -> Dict[str, Decimal]:
        r = self._call(self.session.get_wallet_balance, accountType="UNIFIED")
        if r.get("retCode") != 0:
            raise RuntimeError(f"Saldo no disponible: {r.get('retMsg')}")
        out: Dict[str, Decimal] = {}
        for acc in r["result"]["list"]:
            for c in acc.get("coin", []):
                if c.get("walletBalance"):
                    out[c["coin"]] = Decimal(c["walletBalance"])
        return out

    # ------------------------------------------------------------------ órdenes
    def place_post_only(self, symbol: str, side: OrderSide, price: Decimal, qty: Decimal,
                        link_id: str) -> Tuple[str, Optional[str], str]:
        """Devuelve (estado, orderId, mensaje) con estado en {'ok', 'postonly_reject', 'error'}."""
        r = self._call(self.session.place_order, retries=1, category="spot", symbol=symbol, side=side.value,
                       orderType="Limit", price=str(price), qty=str(qty), timeInForce="PostOnly",
                       orderLinkId=link_id)
        if r.get("retCode") == 0:
            return "ok", r["result"].get("orderId"), ""
        msg = str(r.get("retMsg", ""))
        if any(h in msg.lower() for h in POSTONLY_HINTS):
            return "postonly_reject", None, msg
        return "error", None, msg

    def place_market_sell(self, symbol: str, qty: Decimal, link_id: str) -> Tuple[bool, str]:
        """Solo para liquidación de emergencia del kill switch (taker)."""
        r = self._call(self.session.place_order, retries=1, category="spot", symbol=symbol, side="Sell",
                       orderType="Market", qty=str(qty), marketUnit="baseCoin", orderLinkId=link_id)
        return r.get("retCode") == 0, str(r.get("retMsg", ""))

    def cancel_order(self, symbol: str, link_id: str) -> Tuple[bool, str]:
        r = self._call(self.session.cancel_order, category="spot", symbol=symbol, orderLinkId=link_id)
        msg = str(r.get("retMsg", ""))
        # "order not exists" = ya no está viva (llena o cancelada): el estado real llega por WS/REST
        return r.get("retCode") == 0 or "not exist" in msg.lower(), msg

    def get_open_orders(self, symbol: str) -> List[dict]:
        out: List[dict] = []
        cursor = ""
        while True:
            r = self._call(self.session.get_open_orders, category="spot", symbol=symbol, limit=50, cursor=cursor)
            res = r.get("result", {})
            out.extend(res.get("list", []))
            cursor = res.get("nextPageCursor") or ""
            if not cursor or not res.get("list"):
                return out

    def get_order(self, symbol: str, link_id: str) -> Optional[dict]:
        r = self._call(self.session.get_open_orders, category="spot", symbol=symbol, orderLinkId=link_id)
        lst = r.get("result", {}).get("list") or []
        if lst:
            return lst[0]
        r = self._call(self.session.get_order_history, category="spot", symbol=symbol, orderLinkId=link_id)
        lst = r.get("result", {}).get("list") or []
        return lst[0] if lst else None

    def get_executions(self, symbol: str, start_ms: int) -> List[dict]:
        """Ejecuciones propias desde start_ms (ventana máx. 7 días por consulta en Bybit)."""
        out: List[dict] = []
        start = max(start_ms, int(time.time() * 1000) - 7 * 86_400_000 + 60_000)
        cursor = ""
        while True:
            r = self._call(self.session.get_executions, category="spot", symbol=symbol, startTime=start,
                           limit=100, cursor=cursor)
            res = r.get("result", {})
            out.extend(res.get("list", []))
            cursor = res.get("nextPageCursor") or ""
            if not cursor or not res.get("list"):
                break
        out.sort(key=lambda e: int(e.get("execTime", 0)))
        return out

    # ------------------------------------------------------------------ WebSockets (dominio EU)
    def start_public_ws(self, symbols: List[str], on_book: Callable[[dict], None],
                        on_trade: Optional[Callable[[dict], None]] = None) -> None:
        self.ws_public = WebSocket(testnet=self.config.testnet, channel_type="spot", tld=self.config.tld)
        for s in symbols:
            self.ws_public.orderbook_stream(depth=1, symbol=s, callback=on_book)
            if on_trade:
                self.ws_public.trade_stream(symbol=s, callback=on_trade)
        logger.info(f"WebSocket público conectado ({self.config.tld}): {symbols}")

    def start_private_ws(self, on_order: Callable[[dict], None], on_execution: Callable[[dict], None]) -> None:
        if not self.config.api_key or not self.config.api_secret:
            raise RuntimeError("Claves API ausentes: no se puede abrir el WebSocket privado.")
        self.ws_private = WebSocket(testnet=self.config.testnet, channel_type="private", tld=self.config.tld,
                                    api_key=self.config.api_key, api_secret=self.config.api_secret)
        self.ws_private.order_stream(callback=on_order)
        self.ws_private.execution_stream(callback=on_execution)
        logger.info(f"WebSocket privado conectado ({self.config.tld}): order + execution")

    def close(self) -> None:
        for ws in (self.ws_public, self.ws_private):
            try:
                if ws:
                    ws.exit()
            except Exception:
                pass
