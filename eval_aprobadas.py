import json
data=json.load(open('logs/sombra.json', encoding='utf-8'))
v34=[op for op in data if op.get('version')=='v3.4_cot_calibrado']
aprobadas = [op for op in v34 if op.get('allow_trade')]
for op in aprobadas:
    print(f"--- {op.get('symbol')} ---")
    print(f"Risk: {op.get('risk_score')}")
    print(f"Narrativa: {op.get('narrative')}")
