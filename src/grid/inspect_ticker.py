#!/usr/bin/env python3
import time, sys, json
from pybit.unified_trading import WebSocket

sys.stdout.reconfigure(encoding='utf-8')

def handle_ticker(msg):
    print("MSG:", json.dumps(msg, indent=2))

ws = WebSocket(channel_type="spot", testnet=False)
ws.ticker_stream(symbol="SOLEUR", callback=handle_ticker)
time.sleep(3)
