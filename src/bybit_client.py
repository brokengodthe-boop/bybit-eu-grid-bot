import os
import time
import uuid
import logging
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from pybit.unified_trading import HTTP
from pybit.exceptions import InvalidRequestError
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

import pybit._helpers
# [PARCHE RELOJ LOCALIZADO] Restamos 1500ms al timestamp que firma Bybit para compensar el desvío de Windows, sin afectar time.time() globalmente.
pybit._helpers.generate_timestamp = lambda: int(time.time() * 10**3) - 1500

# Estados de instrumento considerados operables. Verifica qué devuelve tu cuenta EU.
ESTADOS_OPERABLES = {"Trading", "1"}


class BybitError(Exception):
    """Fallo al hablar con Bybit: red, límite de peticiones o retCode != 0."""


class OrderStateUnknown(BybitError):
    """No se pudo confirmar si una orden llegó a crearse. No reintentar a ciegas."""


def _f(valor) -> float:
    """float tolerante: None y '' cuentan como 0."""
    if valor is None or str(valor).strip() == "":
        return 0.0
    return float(valor)


def _cuantizar(valor, paso: str, redondeo=ROUND_DOWN) -> Decimal:
    """Ajusta valor a un múltiplo de paso (basePrecision o tickSize)."""
    p = Decimal(str(paso))
    d = Decimal(str(valor))
    return (d / p).to_integral_value(rounding=redondeo) * p


class BybitClient:
    """Wrapper de pybit. Las lecturas LANZAN BybitError si fallan (nunca devuelven vacío)."""

    def __init__(self, testnet=False):
        self.api_key = os.getenv("BYBIT_API_KEY")
        self.api_secret = os.getenv("BYBIT_API_SECRET")
        self.testnet = testnet

        if not self.api_key or not self.api_secret or self.api_key == "YOUR_BYBIT_API_KEY_HERE":
            logger.warning("Claves de Bybit no configuradas correctamente en .env")

        self.session = HTTP(
            testnet=self.testnet,
            tld="eu",
            api_key=self.api_key,
            api_secret=self.api_secret,
        )

    # ------------------------------------------------------------------
    # Núcleo: toda lectura pasa por aquí
    # ------------------------------------------------------------------
    def _call(self, fn, **kwargs):
        """Llama a pybit y devuelve 'result'. Lanza BybitError ante cualquier fallo."""
        nombre = getattr(fn, "__name__", "llamada")
        try:
            resp = fn(**kwargs)
        except Exception as e:
            raise BybitError(f"{nombre}: {e}") from e
        if not isinstance(resp, dict) or resp.get("retCode") != 0:
            code = resp.get("retCode") if isinstance(resp, dict) else None
            msg = resp.get("retMsg") if isinstance(resp, dict) else resp
            raise BybitError(f"{nombre}: retCode={code} retMsg={msg}")
        return resp.get("result", {})

    def _cuenta(self, **kwargs):
        """Devuelve el bloque de la cuenta UNIFIED. Lanza BybitError si viene vacío."""
        result = self._call(self.session.get_wallet_balance, accountType="UNIFIED", **kwargs)
        lista = result.get("list", [])
        if not lista:
            raise BybitError("get_wallet_balance: respuesta sin datos de cuenta")
        return lista[0]

    # ------------------------------------------------------------------
    # Lecturas
    # ------------------------------------------------------------------
    def get_balance(self, coin="USDC"):
        """Saldo libre (wallet - locked) de una moneda. 0.0 solo si la moneda no aparece."""
        cuenta = self._cuenta(coin=coin)
        for c in cuenta.get("coin", []):
            if c.get("coin") == coin:
                return max(0.0, _f(c.get("walletBalance")) - _f(c.get("locked")))
        return 0.0

    def get_all_balances(self):
        """Todos los saldos > 0 (walletBalance, incluye lo bloqueado en órdenes)."""
        cuenta = self._cuenta()
        balances = {}
        for c in cuenta.get("coin", []):
            wallet = _f(c.get("walletBalance"))
            if wallet > 0:
                balances[c.get("coin")] = wallet
        return balances

    def get_current_price(self, symbol: str):
        result = self._call(self.session.get_tickers, category="spot", symbol=symbol)
        lista = result.get("list", [])
        if not lista:
            raise BybitError(f"get_tickers: sin datos para {symbol}")
        precio = _f(lista[0].get("lastPrice"))
        if precio <= 0:
            raise BybitError(f"get_tickers: precio inválido para {symbol}")
        return precio

    def get_klines(self, symbol: str, interval: str = "15", limit: int = 20) -> str:
        """Velas OHLCV en texto (orden cronológico). Lanza BybitError si no hay datos."""
        result = self._call(self.session.get_kline, category="spot", symbol=symbol,
                            interval=interval, limit=limit)
        lista = result.get("list", [])
        if not lista:
            raise BybitError(f"get_kline: sin velas para {symbol}")
        # Bybit: [startTime, open, high, low, close, volume, turnover]; la más nueva va primero
        lines = ["Hora | Open | High | Low | Close | Volume"]
        for k in reversed(lista):
            hora = datetime.fromtimestamp(int(k[0]) / 1000).strftime("%H:%M")
            lines.append(f"{hora} | {k[1]} | {k[2]} | {k[3]} | {k[4]} | {k[5]}")
        return "\n".join(lines)

    def get_total_equity(self):
        return _f(self._cuenta().get("totalEquity"))

    def get_margin_ratio(self):
        """OJO: en Spot puro suele ser ~0; no sirve como circuit breaker."""
        return _f(self._cuenta().get("accountIMRate")) * 100

    def get_open_orders(self, symbol: str, order_link_id: str = None):
        kwargs = {"category": "spot", "symbol": symbol}
        if order_link_id:
            kwargs["orderLinkId"] = order_link_id
        return self._call(self.session.get_open_orders, **kwargs).get("list", [])

    def get_valid_symbols(self):
        """Símbolos spot operables. Lanza BybitError si la lista viene vacía."""
        result = self._call(self.session.get_instruments_info, category="spot")
        items = result.get("list", [])
        if not items:
            raise BybitError("get_instruments_info: lista vacía")
        operables = {i["symbol"] for i in items if i.get("status") in ESTADOS_OPERABLES}
        if not operables:
            logger.warning("Ningún símbolo con estado operable; uso la lista sin filtrar. "
                           "Revisa ESTADOS_OPERABLES.")
            operables = {i["symbol"] for i in items}
        return operables

    def find_order(self, symbol: str, order_link_id: str = None, order_id: str = None):
        """Busca una orden por orderLinkId u orderId (abiertas y luego historial).
        Devuelve el dict de la orden (orderStatus, avgPrice, cumExecQty...) o None si no existe."""
        if not order_link_id and not order_id:
            raise ValueError("find_order requiere order_link_id u order_id")
        filtro = {"orderLinkId": order_link_id} if order_link_id else {"orderId": order_id}
        for fn in (self.session.get_open_orders, self.session.get_order_history):
            result = self._call(fn, category="spot", symbol=symbol, **filtro)
            lista = result.get("list", [])
            if lista:
                return lista[0]
        return None

    def get_klines_raw(self, symbol: str, interval: str = "1", start_ms: int = None,
                       end_ms: int = None, limit: int = 200):
        """Velas como listas [ts_ms, open, high, low, close, volume], en orden cronológico."""
        kwargs = {"category": "spot", "symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            kwargs["start"] = int(start_ms)
        if end_ms is not None:
            kwargs["end"] = int(end_ms)
        lista = self._call(self.session.get_kline, **kwargs).get("list", [])
        # [ts_ms, open, high, low, close, volume]
        velas = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in lista]
        return sorted(velas, key=lambda v: v[0])

    # ------------------------------------------------------------------
    # Escrituras
    # ------------------------------------------------------------------
    def cancel_all_orders(self, symbol: str):
        """Devuelve True/False y NO lanza: los llamadores la usan tras órdenes ya ejecutadas
        y una excepción ahí dejaría el estado sin guardar. Si falla, la venta posterior
        fallará por saldo bloqueado y se reintentará en el siguiente ciclo."""
        try:
            self._call(self.session.cancel_all_orders, category="spot", symbol=symbol)
            return True
        except BybitError as e:
            logger.error(f"[Bybit] No se pudieron cancelar órdenes de {symbol}: {e}")
            return False

    def place_spot_order(self, symbol: str, side: str, qty: float, market_unit: str = "quoteCoin",
                         order_type: str = "Market", price: float = None,
                         order_link_id: str = None, time_in_force: str = None):
        """
        Envía una orden Spot.
        - Devuelve el dict de resultado (orderId, orderLinkId) si se creó.
        - Devuelve None si la orden NO se envió o Bybit la rechazó (seguro reintentar/fallback).
        - Lanza OrderStateUnknown si hubo fallo de red y no se pudo confirmar si se creó.
        """
        side_c = side.capitalize()
        tipo = order_type.capitalize()
        link_id = (order_link_id or f"{symbol}-{side_c[0]}-{int(time.time())}-{uuid.uuid4().hex[:6]}")[:36]

        # Reglas de precisión del par
        try:
            info = self._call(self.session.get_instruments_info, category="spot", symbol=symbol)
            item = info["list"][0]
            paso_qty = item["lotSizeFilter"]["basePrecision"]
            tick = item["priceFilter"]["tickSize"]
        except (BybitError, KeyError, IndexError) as e:
            logger.error(f"[Bybit] No se pudieron leer las reglas de {symbol}; orden NO enviada: {e}")
            return None

        # Ventas, Limit y compras en baseCoin llevan cantidad en moneda base
        qty_en_base = side_c == "Sell" or tipo == "Limit" or market_unit == "baseCoin"
        paso = paso_qty if qty_en_base else "0.0001"  # compra a mercado en USDC
        qty_fmt = format(_cuantizar(qty, paso, ROUND_DOWN), "f")
        if Decimal(qty_fmt) <= 0:
            logger.error(f"[Bybit] Cantidad {qty} de {symbol} queda en 0 tras ajustar precisión; orden NO enviada")
            return None

        params = {
            "category": "spot", "symbol": symbol, "side": side_c,
            "orderType": tipo, "qty": qty_fmt, "orderLinkId": link_id,
        }
        if side_c == "Buy" and tipo == "Market":
            params["marketUnit"] = market_unit
        if tipo == "Limit":
            if price is None:
                logger.error("[Bybit] Orden Limit sin precio; NO enviada")
                return None
            params["price"] = format(_cuantizar(price, tick, ROUND_HALF_UP), "f")
            if time_in_force:  # p. ej. "PostOnly" para garantizar maker
                params["timeInForce"] = time_in_force

        logger.info(f"[Bybit] Enviando orden Spot: {params}")
        try:
            resp = self.session.place_order(**params)
        except InvalidRequestError as e:
            logger.error(f"[Bybit] Orden rechazada por Bybit: {e}")
            return None
        except Exception as e:
            logger.error(f"[Bybit] Fallo enviando orden ({e}); verificando por orderLinkId...")
            return self._verificar_orden(symbol, link_id)

        if isinstance(resp, dict) and resp.get("retCode") == 0:
            res = resp.get("result", {}) or {}
            res.setdefault("orderLinkId", link_id)
            logger.info(f"[Bybit] Orden creada. OrderID: {res.get('orderId')}")
            return res
        logger.error(f"[Bybit] Orden rechazada: {resp}")
        return None

    def _verificar_orden(self, symbol: str, link_id: str):
        """Tras un fallo ambiguo: si la orden existe la devuelve; si no, lanza OrderStateUnknown.
        (time.sleep bloquea el hilo; en código async llama a place_spot_order con to_thread.)"""
        time.sleep(2)
        try:
            found = self.find_order(symbol, link_id)
        except BybitError as e:
            raise OrderStateUnknown(f"{symbol} {link_id}: no se pudo verificar ({e})") from e
        if found:
            logger.warning(f"[Bybit] La orden {link_id} sí se creó pese al fallo de red.")
            return {"orderId": found.get("orderId"), "orderLinkId": link_id}
        raise OrderStateUnknown(f"{symbol} {link_id}: fallo de red y la orden no aparece; estado incierto")
