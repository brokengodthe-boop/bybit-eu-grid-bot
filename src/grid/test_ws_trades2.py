#!/usr/bin/env python3
import time, sys
from pybit.unified_trading import WebSocket

sys.stdout.reconfigure(encoding='utf-8')

trades = []
def handle_trade(msg):
    data = msg.get("data", [])
    for t in data:
        trades.append(t)
        print(f"[LIVE TRADE] {t.get('s')} | Px: {t.get('p')} | Qty: {t.get('v')} | Side: {t.get('S')}", flush=True)

ws = WebSocket(channel_type="spot", testnet=False)
ws.trade_stream(symbol="BTCEUR", callback=handle_trade)
ws.trade_stream(symbol="SOLUSDC", callback=handle_trade)
time.sleep(6)
print(f"Total trades: {len(trades)}")
