#!/usr/bin/env python3
"""
Test trade and orderbook streams
"""
import time, sys
from pybit.unified_trading import WebSocket

sys.stdout.reconfigure(encoding='utf-8')

trades = []

def handle_trade(msg):
    data = msg.get("data", [])
    for t in data:
        trades.append(t)
        print(f"[LIVE TRADE] {t.get('s')} | Px: {t.get('p')} | Qty: {t.get('v')} | Side: {t.get('S')}", flush=True)

try:
    ws = WebSocket(channel_type="spot", testnet=False)
    ws.trade_stream(symbol="SOLEUR", callback=handle_trade)
    ws.trade_stream(symbol="XRPEUR", callback=handle_trade)
    print("Suscrito a trades en vivo de SOLEUR y XRPEUR...")
    time.sleep(5)
    print(f"Total trades recibidos en 5s: {len(trades)}")
except Exception as e:
    print("Error:", e)
