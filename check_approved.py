import json
with open('logs/auditoria.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
for trade in data:
    if trade.get('ai_verdict') == 'APPROVED':
        print(f"{trade['timestamp']} - {trade['symbol']} - Entry: {trade['entry_price']}")
