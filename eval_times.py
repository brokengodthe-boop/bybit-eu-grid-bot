import json
data=json.load(open('logs/sombra.json', encoding='utf-8'))
v34=[op for op in data if op.get('version')=='v3.4_cot_calibrado']
for i, op in enumerate(v34):
    print(f"{i}: {op.get('timestamp')} - {op.get('symbol')} - allow: {op.get('allow_trade')} - model: {op.get('model')}")
