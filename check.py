import json
from src.bybit_client import BybitClient

bybit = BybitClient(testnet=False)
balances = bybit.get_all_balances()

with open("logs/auditoria.json", "r", encoding="utf-8") as f:
    data = json.load(f)

open_trades = [d for d in data if not d.get("cerrado", False)]
print(f"Total posiciones no cerradas en auditoria: {len(open_trades)}")

for t in open_trades:
    sym = t['symbol']
    coin = sym.replace("USDC", "")
    entry = t['entry_price']
    current = bybit.get_current_price(sym)
    amount = balances.get(coin, 0)
    
    if current > 0:
        pnl = ((current - entry) / entry) * 100
        print(f"{sym} | Balance: {amount:.4f} {coin} | Entrada: {entry:.6f} | Actual: {current:.6f} | PnL: {pnl:+.2f}%")
    else:
        print(f"{sym} | Balance: {amount:.4f} {coin} | Entrada: {entry:.6f} | Actual: NO DISPONIBLE")
