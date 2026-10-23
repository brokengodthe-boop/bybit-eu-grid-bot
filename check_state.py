import sys
sys.path.insert(0, ".")
from src.bybit_client import BybitClient

bybit = BybitClient(testnet=False)

for p in ["WLDUSDC", "ENAUSDC", "ONDOUSDC", "ALGOUSDC"]:
    orders = bybit.get_open_orders(p)
    if orders:
        for o in orders:
            print(f"{p}: {o.get('side')} {o.get('orderType')} qty={o.get('qty')} price={o.get('price')}")
    else:
        print(f"{p}: Sin ordenes abiertas")

r = bybit.session.get_wallet_balance(accountType="UNIFIED", coin="USDC")
coins = r["result"]["list"][0]["coin"]
for c in coins:
    if c["coin"] == "USDC":
        print("\nUSDC Balance detallado:")
        for k, v in c.items():
            print(f"  {k}: {v}")
