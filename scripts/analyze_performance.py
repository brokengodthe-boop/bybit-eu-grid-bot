import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()
cmc_key = os.getenv("CMC_API_KEY")

with open("logs/auditoria.json", "r", encoding="utf-8") as f:
    trades = json.load(f)

# Fetch latest prices for all symbols
symbols = sorted(list(set(t.get("symbol") for t in trades if t.get("symbol"))))
symbols_str = ",".join(symbols)
url = f"https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol={symbols_str}"
res = requests.get(url, headers={"X-CMC_PRO_API_KEY": cmc_key})
data = res.json().get("data", {})

live_prices = {}
for s in symbols:
    it = data.get(s)
    if isinstance(it, list):
        it = it[0]
    if isinstance(it, dict):
        live_prices[s] = it["quote"]["USD"]["price"]

# 1. Analisis Global de los 50 ciclos
print("=" * 80)
print(f"REPORTE CUANTITATIVO: {len(trades)} CICLOS ANALIZADOS (13:14 a 22:16)")
print("=" * 80)

# 2. Agrupar en sesiones de posicion continuas
sessions = []
for t in trades:
    sym = t["symbol"]
    if not sessions or sessions[-1]["symbol"] != sym:
        sessions.append({
            "symbol": sym,
            "start_time": t["timestamp"][:16],
            "entry_price": t["entry_price"],
            "all_entries": [t["entry_price"]],
            "count": 1
        })
    else:
        sessions[-1]["all_entries"].append(t["entry_price"])
        sessions[-1]["count"] += 1

# Simulacion de Cartera Unica de 100 EUR con reinversion (Compounding)
capital_1x = 100.0
capital_5x = 100.0
leverage = 5

table_rows = []
for idx, sess in enumerate(sessions):
    sym = sess["symbol"]
    entry = sess["entry_price"]
    
    # Precio de salida: el ultimo de la sesion o el precio live si sigue activo
    if idx == len(sessions) - 1:
        exit_p = live_prices.get(sym, entry)
    else:
        exit_p = sess["all_entries"][-1]
        if exit_p == entry and len(sessions) > idx + 1:
            # Si se mantuvo constante en el registro, comparamos contra precio de mercado
            exit_p = live_prices.get(sym, entry)
            
    pct_raw = (exit_p - entry) / entry
    
    # Reglas de salida paramétrica (+6% TP, -2% SL)
    if pct_raw >= 0.06:
        realized = 0.06
        status = "TP (+6%)"
    elif pct_raw <= -0.02:
        realized = -0.02
        status = "SL (-2%)"
    else:
        realized = pct_raw
        status = f"Flotante ({pct_raw*100:+.2f}%)"
        
    pnl_1x = capital_1x * realized
    capital_1x = max(0.0, capital_1x + pnl_1x)
    
    pnl_5x = capital_5x * realized * leverage
    capital_5x = max(0.0, capital_5x + pnl_5x)
    
    table_rows.append({
        "id": idx + 1,
        "time": sess["start_time"],
        "symbol": sym,
        "cycles": sess["count"],
        "entry": entry,
        "exit": exit_p,
        "pct_1x": realized * 100,
        "pct_5x": realized * leverage * 100,
        "cap_1x": capital_1x,
        "cap_5x": capital_5x,
        "status": status
    })

print(f"{'#':<3} {'HORA':<17} {'ACTIVO':<8} {'ENTRADA':<12} {'SALIDA':<12} {'RET 1x':<9} {'RET 5x':<9} {'SALDO 5x':<10} {'ESTADO'}")
print("-" * 95)
for r in table_rows:
    print(f"{r['id']:<3} {r['time']:<17} #{r['symbol']:<7} ${r['entry']:<11.6f} ${r['exit']:<11.6f} {r['pct_1x']:+6.2f}% {r['pct_5x']:+6.2f}% -> {r['cap_5x']:6.2f} EUR  {r['status']}")

print("=" * 95)
print(f"RESULTADOS FINALES PARA UN CAPITAL INICIAL DE 100.00 EUR TRAS 9 HORAS:")
print(f" -> En Modo Spot (1x, sin apalancamiento):  {capital_1x:.2f} EUR  (Beneficio Neto: {capital_1x - 100:+.2f} EUR / {(capital_1x - 100):+.2f}%)")
print(f" -> En Modo Bot (5x Isolated Margin):     {capital_5x:.2f} EUR  (Beneficio Neto: {capital_5x - 100:+.2f} EUR / {(capital_5x - 100):+.2f}%)")
print("=" * 95)
