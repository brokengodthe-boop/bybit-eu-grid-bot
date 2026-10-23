import json

with open('universe_candidates.json') as f:
    cand = json.load(f)['candidates']

for c in cand:
    if c['symbol'] in ['TRUMPUSDT', 'ADAUSDT', 'ATOMUSDT', 'XLMUSDT']:
        min_notional = float(c['minqty']) * float(c['price'])
        print(f"{c['symbol']:<10}: Price={c['price']:<8} MinQty={c['minqty']:<6} QtyStep={c['qtystep']:<6} MinNotional={min_notional:.2f} USDT")
