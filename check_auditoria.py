import json
with open('logs/auditoria.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

for trade in data[-10:]:
    if not trade.get('cerrado', False):
        print(f"{trade['timestamp']} - {trade['symbol']} - Entry: {trade['entry_price']}")
