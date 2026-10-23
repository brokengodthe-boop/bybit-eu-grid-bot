import json
from src.bybit_client import BybitClient

b = BybitClient(testnet=False)
balances = b.get_all_balances()
current_equity = b.get_total_equity()

with open("logs/auditoria.json", "r", encoding="utf-8") as f:
    auditoria = json.load(f)

# Buscar los primeros registros con total_equity o depósitos
equities = [x.get("total_equity") for x in auditoria if x.get("total_equity") is not None]
initial_equity = equities[0] if equities else None

# Encontrar precios de entrada de las posiciones actuales
# Buscamos la última compra registrada para cada moneda con balance
open_positions = {}
for coin, amt in balances.items():
    if coin in ["USDC", "USDT", "EUR"]:
        continue
    sym = f"{coin}USDC"
    p = b.get_current_price(sym)
    if p > 0 and (amt * p) > 0.5:
        # Buscar en auditoria la última entrada para este símbolo donde la IA aprobó la compra
        entry_price = None
        for record in reversed(auditoria):
            if record.get("symbol") in [sym, coin] and record.get("ai_verdict") in ["APPROVED", None]:
                entry_price = record.get("entry_price")
                entry_time = record.get("timestamp")
                break
        
        val_actual = amt * p
        pnl_pct = ((p - entry_price) / entry_price * 100) if entry_price else 0.0
        val_costo = amt * entry_price if entry_price else val_actual
        pnl_usd = val_actual - val_costo
        
        open_positions[coin] = {
            "amount": amt,
            "current_price": p,
            "entry_price": entry_price,
            "current_val": val_actual,
            "cost_val": val_costo,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd
        }

print("=== REPORTE DE RENTABILIDAD ===")
print(f"Equity Actual: ${current_equity:.2f} USD")
if initial_equity:
    print(f"Primer Equity Registrado en Auditoría: ${initial_equity:.2f} USD")
    diff = current_equity - initial_equity
    pct_total = (diff / initial_equity) * 100
    print(f"Rendimiento Global Cuenta: {diff:+.2f} USD ({pct_total:+.2f}%)\n")

print("--- PnL NO REALIZADO (Posiciones Abiertas) ---")
total_unrealized_pnl_usd = 0
for coin, data in open_positions.items():
    print(f"Activo: {coin}")
    print(f"  - Cantidad: {data['amount']}")
    print(f"  - Precio Entrada: ${data['entry_price'] if data['entry_price'] else 'Desconocido'}")
    print(f"  - Precio Actual:  ${data['current_price']}")
    print(f"  - Valor Actual:   ${data['current_val']:.2f} USD")
    print(f"  - Rendimiento:    {data['pnl_pct']:+.2f}% ({data['pnl_usd']:+.2f} USD)")
    total_unrealized_pnl_usd += data['pnl_usd']
    print("")

print(f"Total PnL Flotante: {total_unrealized_pnl_usd:+.2f} USD")
