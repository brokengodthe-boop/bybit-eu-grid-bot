import os
import logging
import threading
from pybit.unified_trading import WebSocket
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class BybitWebSocketClient:
    """
    Cliente WebSocket para recibir precios en tiempo real de Bybit Spot.
    Mantiene un diccionario de precios actualizado en milisegundos.
    Reemplaza el polling REST para la monitorización de posiciones abiertas.
    """

    def __init__(self, testnet=False):
        self.testnet = testnet
        self.prices = {}       # {symbol: last_price}
        self.volumes = {}      # {symbol: volume_24h}
        self.ws = None
        self._subscribed = set()
        self._lock = threading.Lock()
        self._connected = False

    def _handle_ticker(self, message):
        """Callback ejecutado cada vez que llega un tick de precio (milisegundos)."""
        try:
            data = message.get("data", {})
            symbol = data.get("symbol", "")
            last_price = data.get("lastPrice")
            volume_24h = data.get("volume24h")

            if symbol and last_price:
                with self._lock:
                    self.prices[symbol] = float(last_price)
                    if volume_24h:
                        self.volumes[symbol] = float(volume_24h)
        except Exception as e:
            logger.error(f"[WS] Error procesando ticker: {e}")

    def connect(self):
        """Inicia la conexión WebSocket con Bybit Spot."""
        if self._connected:
            return
        try:
            self.ws = WebSocket(
                testnet=self.testnet,
                channel_type="spot",
            )
            self._connected = True
            print("[WS] ✅ Conexión WebSocket establecida con Bybit.", flush=True)
        except Exception as e:
            logger.error(f"[WS] Error conectando WebSocket: {e}")
            self._connected = False

    def subscribe(self, symbol: str):
        """Suscribe a un par para recibir precios en tiempo real."""
        if not self._connected:
            self.connect()
        if symbol not in self._subscribed and self.ws:
            try:
                self.ws.ticker_stream(symbol=symbol, callback=self._handle_ticker)
                self._subscribed.add(symbol)
                print(f"[WS] 📡 Suscrito a ticker en tiempo real: {symbol}", flush=True)
            except Exception as e:
                logger.error(f"[WS] Error suscribiendo a {symbol}: {e}")

    def subscribe_multiple(self, symbols: list):
        """Suscribe a múltiples pares de una vez."""
        for s in symbols:
            self.subscribe(s)

    def get_price(self, symbol: str) -> float:
        """Obtiene el último precio recibido por WebSocket (latencia <100ms)."""
        with self._lock:
            return self.prices.get(symbol, 0.0)

    def get_all_prices(self) -> dict:
        """Devuelve copia de todos los precios actuales."""
        with self._lock:
            return dict(self.prices)

    @property
    def subscribed_symbols(self) -> set:
        return set(self._subscribed)

    @property
    def is_connected(self) -> bool:
        return self._connected
