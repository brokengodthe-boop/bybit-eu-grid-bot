import json
data=json.load(open('logs/sombra.json', encoding='utf-8'))
v34=[op for op in data if op.get('version')=='v3.4_cot_calibrado']
print(f'Total v3.4: {len(v34)}')
print(f'Approved v3.4: {sum(1 for op in v34 if op.get("allow_trade"))}')
print(f'Resolved v3.4: {sum(1 for op in v34 if op.get("resuelto"))}')
