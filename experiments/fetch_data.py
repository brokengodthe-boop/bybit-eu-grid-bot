"""Descarga y cachea 120 días de velas 1m de los pares del experimento."""
import sys, time
sys.path.insert(0, r"C:\Users\estre\.gemini\antigravity-ide\scratch\grvt_trading_bot")
from src.grid_v2.backtest import fetch_klines

PAIRS = ["BTCEUR", "ETHEUR", "SOLEUR", "XRPEUR", "DOGEEUR", "ADAEUR", "LTCEUR"]
for p in PAIRS:
    t = time.time()
    bars = fetch_klines(p, "1", 120)
    print(f"{p}: {len(bars)} velas ({time.time()-t:.0f}s) desde {bars[0][0]} hasta {bars[-1][0]}", flush=True)
