import re
import os

p = r"C:\Users\estre\.gemini\antigravity-ide\scratch\grvt_trading_bot\src\bybit_client.py"
s = open(p, encoding="utf-8").read()

old_find = s[s.index("    def find_order("):s.index("    # ------------------------------------------------------------------\n    # Escrituras")]
new_find = '''    def find_order(self, symbol: str, order_link_id: str = None, order_id: str = None):
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
        """Velas como listas [ts_ms, open, high, low, close], en orden cronológico."""
        kwargs = {"category": "spot", "symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            kwargs["start"] = int(start_ms)
        if end_ms is not None:
            kwargs["end"] = int(end_ms)
        lista = self._call(self.session.get_kline, **kwargs).get("list", [])
        velas = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in lista]
        return sorted(velas, key=lambda v: v[0])

'''
s = s.replace(old_find, new_find)
open(p, "w", encoding="utf-8").write(s)
print("ok")
