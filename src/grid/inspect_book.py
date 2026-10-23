#!/usr/bin/env python3
import time, sys, json
from pybit.unified_trading import WebSocket

sys.stdout.reconfigure(encoding='utf-8')

def handle_book(msg):
    data = msg.get("data", {})
    bids = data.get("b", [])
    asks = data.get("a", [])
    print(f"[BOOK] Bids: {bids[:2]} | Asks: {asks[:2]}", flush=True)

ws = WebSocket(channel_type="spot", testnet=False)
ws.orderbook_stream(depth=1, symbol="SOLEUR", callback=handle_book)
ws.orderbook_stream(depth=1, symbol="ADAEUR", callback=handle_book)
time.sleep(5)
