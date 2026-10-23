import json
import subprocess
import os

with open('logs/sombra.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

v34_aprobadas = [op for op in data if op.get('version') == 'v3.4_cot_calibrado' and op.get('allow_trade')]

with open('logs/sombra_v34_tmp.json', 'w', encoding='utf-8') as f:
    json.dump(v34_aprobadas, f, indent=2)

print(f"Written {len(v34_aprobadas)} approved trades to logs/sombra_v34_tmp.json")

# Now run verificar_volumen.py
subprocess.run(['python', 'verificar_volumen.py', 'logs/sombra_v34_tmp.json', '3'])
