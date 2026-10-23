import json
from collections import defaultdict

regs = json.load(open("logs/sombra.json"))
por_combo = defaultdict(list)

for r in regs:
    por_combo[(r.get("tp_pct"), r.get("sl_pct"))].append(r.get("timestamp"))
    
for combo, ts in sorted(por_combo.items()):
    # Formatear un poco para que sea legible
    print(f"TP/SL {combo}:")
    print(f"  Primera vez: {min(ts)}")
    print(f"  Última vez:  {max(ts)}")
    print(f"  Conteo total: {len(ts)}")
    print("-" * 30)
