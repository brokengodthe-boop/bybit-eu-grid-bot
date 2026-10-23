#!/usr/bin/env python3
import time, sys
from pybit.unified_trading import HTTP

session = HTTP(testnet=False, tld="eu")
end_ms = int(time.time() * 1000)
start_ms = end_ms - (30 * 24 * 3600 * 1000) # 30 días atrás

bars = []
curr_start = start_ms

print(f"Descargando klines de ADAEUR desde hace 30 días...")
while curr_start < end_ms:
    resp = session.get_kline(
        category="spot",
        symbol="ADAEUR",
        interval="15", # 15 minutos: 30 días = 2880 barras (15 llamadas)
        start=curr_start,
        limit=200
    )
    data = resp.get("result", {}).get("list", [])
    if not data:
        break
    # Bybit devuelve la más nueva primero; invertimos
    for b in reversed(data):
        bars.append(b)
    # Siguiente timestamp
    latest_ts = int(data[0][0])
    curr_start = latest_ts + (15 * 60 * 1000)
    if len(data) < 200 or curr_start >= end_ms:
        break
    time.sleep(0.05)

print(f"Total barras obtenidas: {len(bars)}")
if bars:
    print(f"Primera barra: {bars[0]}")
    print(f"Última barra: {bars[-1]}")
