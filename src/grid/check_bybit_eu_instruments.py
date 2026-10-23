#!/usr/bin/env python3
"""
src/grid/check_bybit_eu_instruments.py — Inspecciona qué mercados (spot vs linear) están habilitados en Bybit EU.
"""
import sys
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import pybit._helpers

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

session = HTTP(testnet=False, tld="eu")

for cat in ["spot", "linear", "inverse"]:
    try:
        resp = session.get_instruments_info(category=cat)
        ret = resp.get("retCode")
        items = resp.get("result", {}).get("list", [])
        print(f"Categoría '{cat}': retCode={ret}, total instrumentos={len(items)}")
        if items:
            syms = [i.get("symbol") for i in items[:15]]
            print(f"  Primeros 15 símbolos: {syms}")
    except Exception as e:
        print(f"Categoría '{cat}' falló: {e}")
