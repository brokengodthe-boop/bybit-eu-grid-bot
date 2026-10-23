import json
with open('logs/auditoria.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
for trade in data:
    if trade.get('cerrado') and trade['timestamp'] > '2026-09-25T19:00:00':
        print(f"{trade['timestamp']} - {trade['symbol']} - Exit Reason: {trade['exit_reason']} - PnL: {trade.get('pnl_pct', 0):+.2f}%")
