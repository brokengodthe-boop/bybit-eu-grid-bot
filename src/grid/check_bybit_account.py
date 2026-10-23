#!/usr/bin/env python3
"""
src/grid/check_bybit_account.py — Verificación de Conectividad y Saldo de Bybit.
"""
import os, time, sys
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import pybit._helpers

# Fix Windows console UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()

pybit._helpers.generate_timestamp = lambda: int(time.time() * 10**3) - 1500

api_key = os.getenv("BYBIT_API_KEY")
api_secret = os.getenv("BYBIT_API_SECRET")

print(f"Probando conexión con Bybit API...")
print(f"API Key: {api_key[:6]}...{api_key[-4:] if api_key else 'None'}")

for tld_opt in ["eu", "com"]:
    for testnet_flag in [False, True]:
        tipo = f"{'TESTNET' if testnet_flag else 'MAINNET'} (tld={tld_opt})"
        try:
            session = HTTP(
                testnet=testnet_flag,
                tld=tld_opt,
                api_key=api_key,
                api_secret=api_secret,
            )
            resp = session.get_wallet_balance(accountType="UNIFIED")
            if resp.get("retCode") == 0:
                print(f"\n[OK] ¡Conexión EXITOSA en {tipo}!")
                res = resp.get("result", {}).get("list", [{}])[0]
                tot_equity = res.get("totalEquity", "0")
                print(f"Total Equity de la cuenta: {tot_equity} USD")
                coins = res.get("coin", [])
                found_any = False
                for c in coins:
                    wb = float(c.get("walletBalance", 0) or 0)
                    if wb > 0:
                        found_any = True
                        print(f"  - {c.get('coin')}: {wb} (Bloqueado: {c.get('locked')})")
                if not found_any:
                    print("  (La cuenta está a 0 actualmente).")
                sys.exit(0)
            else:
                print(f"Intento {tipo}: retCode={resp.get('retCode')} retMsg={resp.get('retMsg')}")
        except Exception as e:
            print(f"Intento {tipo} falló: {str(e)[:120]}")
