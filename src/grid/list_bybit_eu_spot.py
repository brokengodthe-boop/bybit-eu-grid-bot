#!/usr/bin/env python3
"""
src/grid/list_bybit_eu_spot.py — Lista los símbolos y cotizaciones de Bybit EU Spot.
"""
from pybit.unified_trading import HTTP
import sys

sys.stdout.reconfigure(encoding='utf-8')
session = HTTP(testnet=False, tld="eu")
resp = session.get_instruments_info(category="spot")
items = resp.get("result", {}).get("list", [])
symbols = [i["symbol"] for i in items]
print(f"Total símbolos Spot en Bybit EU: {len(symbols)}")

eur_pairs = [s for s in symbols if s.endswith("EUR")]
usdc_pairs = [s for s in symbols if s.endswith("USDC")]
usdt_pairs = [s for s in symbols if s.endswith("USDT")]

print(f"\nPares contra EUR ({len(eur_pairs)}): {eur_pairs}")
print(f"\nPares contra USDC ({len(usdc_pairs)}): {usdc_pairs[:20]}...")
print(f"\nPares contra USDT ({len(usdt_pairs)}): {usdt_pairs}")
