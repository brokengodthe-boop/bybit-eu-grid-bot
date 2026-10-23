#!/usr/bin/env python3
"""
Test websocket connection on Bybit Spot
"""
import time, sys
from pybit.unified_trading import WebSocket

sys.stdout.reconfigure(encoding='utf-8')

prices = {}

def handle_ticker(msg):
    data = msg.get("data", {})
    sym = data.get("symbol")
    lp = data.get("lastPrice")
    if sym and lp:
        prices[sym] = float(lp)
        print(f"[WS TICK] {sym} -> {lp}", flush=True)

try:
    ws = WebSocket(channel_type="spot", testnet=False)
    ws.ticker_stream(symbol="ADAEUR", callback=handle_ticker)
    ws.ticker_stream(symbol="SOLEUR", callback=handle_ticker)
    print("WebSocket iniciado, esperando ticks...")
    time.sleep(5)
    print("Precios recibidos:", prices)
except Exception as e:
    print("Error:", e)
