#!/usr/bin/env python3
"""universe_scanner.py — Pre-filtrado del universo Bybit Linear (USDT perps)."""
import argparse, json, math, statistics, time, csv
from decimal import Decimal
import requests

BASE = "https://api.bybit.com"
S = requests.Session()

def get(path, params):
    for attempt in range(5):
        try:
            r = S.get(BASE + path, params=params, timeout=10)
            if r.status_code == 200:
                j = r.json()
                if j.get("retCode") == 0:
                    return j["result"]
        except Exception:
            pass
        time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"fallo en {path} {params}")

def instruments():
    out, cursor = [], ""
    while True:
        p = {"category": "linear", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        res = get("/v5/market/instruments-info", p)
        out += res["list"]
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            return out

def tickers():
    return {t["symbol"]: t for t in get("/v5/market/tickers", {"category": "linear"})["list"]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-tick-bps", type=float, default=4.0)
    ap.add_argument("--max-tick-bps", type=float, default=30.0)   # por encima: suele ser ilíquido
    ap.add_argument("--min-turnover", type=float, default=20e6)
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--out", default="universe_candidates")
    a = ap.parse_args()

    print("[*] Obteniendo lista de contratos Linear Perpetuals...")
    inst = [i for i in instruments()
            if i.get("status") == "Trading"
            and i.get("quoteCoin") == "USDT"
            and i.get("settleCoin") == "USDT"
            and i.get("contractType") == "LinearPerpetual"
            and not i.get("isPreListing", False)]
    meta = {i["symbol"]: i for i in inst}
    print(f"[1] Trading/USDT/Perpetual encontrados: {len(meta)}")

    # Muestreo repetido de BBO
    print(f"[*] Muestreando BBO ({a.samples} muestras, intervalo {a.interval}s)...")
    obs = {s: [] for s in meta}
    last = {}
    for k in range(a.samples):
        tk = tickers()
        for s in meta:
            t = tk.get(s)
            if not t:
                continue
            try:
                bid, ask = Decimal(t["bid1Price"]), Decimal(t["ask1Price"])
            except Exception:
                continue
            if bid <= 0 or ask <= bid:
                continue
            tick = Decimal(meta[s]["priceFilter"]["tickSize"])
            mid = (bid + ask) / 2
            obs[s].append((float((ask - bid) / tick), float((ask - bid) / mid * 10000)))
            last[s] = t
        print(f"  -> Muestra {k+1}/{a.samples} completada ({len(tk)} tickers leídos)")
        if k < a.samples - 1:
            time.sleep(a.interval)

    rows = []
    for s, o in obs.items():
        if not o or s not in last:
            continue
        t, m = last[s], meta[s]
        tick = float(m["priceFilter"]["tickSize"])
        px = float(t.get("markPrice") or t.get("lastPrice") or 0)
        if px <= 0:
            continue
        spticks = [x[0] for x in o]
        rows.append({
            "symbol": s,
            "price": px,
            "ticksize": tick,
            "tickbps": (tick / px) * 1e4,
            "turnover24h": float(t.get("turnover24h", 0) or 0),
            "oiusd": float(t.get("openInterestValue", 0) or 0),
            "funding": float(t.get("fundingRate", 0) or 0),
            "spreadticksmed": statistics.median(spticks),
            "onetickfrac": sum(1 for x in spticks if round(x) == 1) / len(spticks),
            "spreadbpsmed": statistics.median(x[1] for x in o),
            "minqty": m["lotSizeFilter"].get("minOrderQty"),
            "qtystep": m["lotSizeFilter"].get("qtyStep"),
            "nsamples": len(o),
        })

    def stage(r, tb, tv):
        return a.max_tick_bps >= r["tickbps"] >= tb and r["turnover24h"] >= tv

    # Embudo informativo + relajación explícita
    sel = []
    for tb, tv in [(a.min_tick_bps, a.min_turnover), (a.min_tick_bps, 10e6),
                   (3.0, 10e6), (3.0, 5e6), (2.0, 5e6)]:
        sel = [r for r in rows if stage(r, tb, tv)]
        print(f"[2] tickbps >= {tb} & turnover >= {tv/1e6:.0f}M -> {len(sel)} candidatos")
        if len(sel) >= min(a.top, 10):
            break
    if not sel:
        print("[!] Ningún candidato con los umbrales estándar; seleccionando mejores por turnover con tickbps >= 1.5...")
        sel = [r for r in rows if r["tickbps"] >= 1.5]
        sel.sort(key=lambda r: r["turnover24h"], reverse=True)

    # Score: spread bruto disponible menos coste, ponderado por liquidez.
    for r in sel:
        halfspread = r["spreadbpsmed"] / 2.0
        r["grossedgebps"] = halfspread - 2.0         # antes de markout
        r["score"] = (r["grossedgebps"]
                      * math.log10(max(r["turnover24h"], 1.0))
                      * (0.5 + 0.5 * r["onetickfrac"])
                      / max(1.0, r["spreadticksmed"] - 1.0))
    sel.sort(key=lambda r: r["score"], reverse=True)
    top = sel[:a.top]

    with open(a.out + ".json", "w") as f:
        json.dump({"ts": int(time.time()), "candidates": top}, f, indent=2)
    if top:
        with open(a.out + ".csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(top[0].keys()))
            w.writeheader()
            w.writerows(top)

    print("\n" + "=" * 95)
    print(f"{'SYMBOL':<16} {'TICK (bps)':<12} {'SPREAD MED':<16} {'1-TICK %':<10} {'VOL 24H':<14} {'SCORE':<8}")
    print("=" * 95)
    for r in top:
        print(f"{r['symbol']:<16} {r['tickbps']:6.2f} bps   "
              f"{r['spreadticksmed']:.1f}t ({r['spreadbpsmed']:.1f} bps)    "
              f"{r['onetickfrac']:.0%}        "
              f"${r['turnover24h']/1e6:8.1f}M    "
              f"{r['score']:6.2f}")
    print("=" * 95)
    print(f"Resultados guardados en {a.out}.json y {a.out}.csv\n")

if __name__ == "__main__":
    main()
