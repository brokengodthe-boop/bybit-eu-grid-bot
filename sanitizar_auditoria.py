import json
from src.bybit_client import BybitClient

b = BybitClient(testnet=False)
balances = b.get_all_balances()
valid_symbols = b.get_valid_symbols()

with open("logs/auditoria.json", "r", encoding="utf-8") as f:
    auditoria = json.load(f)

print(f"Total registros antes: {len(auditoria)}")
abiertos_antes = sum(1 for r in auditoria if not r.get("cerrado", False))
print(f"Registros abiertos antes: {abiertos_antes}")

# Criptomonedas que realmente tienen saldo significativo (> 0.1 USD)
activos_reales = {}
for coin, amt in balances.items():
    if coin in ["USDC", "USDT", "EUR"]:
        continue
    sym = f"{coin}USDC"
    if sym in valid_symbols:
        p = b.get_current_price(sym)
        if (amt * p) > 0.5:
            activos_reales[coin] = {"symbol": sym, "amount": amt, "price": p}

print(f"Activos reales con saldo en Bybit: {list(activos_reales.keys())}")

# Para cada activo real, mantendremos ABIERTO solo el registro más reciente
ultimos_por_activo = {}
for idx, r in enumerate(auditoria):
    sym = r.get("symbol", "")
    coin = sym.replace("USDC", "")
    if coin in activos_reales and r.get("ai_verdict") in ["APPROVED", "RECOVERED"]:
        ultimos_por_activo[coin] = idx

for idx, r in enumerate(auditoria):
    sym = r.get("symbol", "")
    coin = sym.replace("USDC", "")
    
    # Si es un registro antiguo que no es el último activo real:
    if idx not in ultimos_por_activo.values():
        r["cerrado"] = True
        if not r.get("exit_reason"):
            if r.get("ai_verdict") == "VETO":
                r["exit_reason"] = "VETOED_SCAN"
            else:
                r["exit_reason"] = "HISTORICO_CERRADO"
    else:
        # Es el registro activo real
        r["cerrado"] = False
        print(f"[CONSERVADO ABIERTO] {sym} (Registro #{idx})")

with open("logs/auditoria.json", "w", encoding="utf-8") as f:
    json.dump(auditoria, f, indent=4, ensure_ascii=False)

abiertos_despues = sum(1 for r in auditoria if not r.get("cerrado", False))
print(f"Registros abiertos después: {abiertos_despues}")
