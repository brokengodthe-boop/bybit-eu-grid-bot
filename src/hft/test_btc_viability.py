"""
Test de viabilidad económica sobre datos reales grabados de BTCUSDT (Spot).
Valida si la microestructura de BTC confirma la hipótesis del quant: edge negativo tras fees y markout.
"""
import sys, gzip
import numpy as np
from replay import iter_events
from engine import Engine
from pair_viability import pair_viability

def run(pattern="data/md_20261011_15.jsonl.gz", symbol="BTCUSDT", tick=0.1):
    print(f"[*] Replayando eventos desde {pattern} para {symbol}...")
    engine = Engine(["BTCUSDT", "SOLUSDT"], request_snapshot=lambda s: None)
    
    trades_list = []
    books_list = []
    
    last_book_t = 0
    
    for ev in iter_events(pattern):
        engine.on_event(ev)
        ev_type = type(ev).__name__
        
        if ev_type == "Trade" and getattr(ev, "symbol", "") == symbol:
            t_sec = ev.recv_ns / 1e9
            # buyer_is_maker = True -> taker fue vendedor (-1). buyer_is_maker = False -> taker fue comprador (+1)
            side = -1.0 if ev.buyer_is_maker else 1.0
            trades_list.append((t_sec, float(ev.price), float(ev.qty), side))
            
        elif ev_type in ("DepthDiff", "Snapshot") and getattr(ev, "symbol", "") == symbol:
            t_sec = ev.recv_ns / 1e9
            b = engine.books.get(symbol)
            if b and not b.synced and ev_type == "DepthDiff":
                b.synced = True
                for p, q in ev.bids:
                    if q > 0: b.bids[p] = q
                for p, q in ev.asks:
                    if q > 0: b.asks[p] = q
            
            # Muestrear el book cada 100ms para no saturar memoria
            if t_sec - last_book_t >= 0.1 and b and b.bids and b.asks:
                bb, ba = b.best_bid(), b.best_ask()
                if bb and ba:
                    books_list.append((t_sec, float(bb[0]), float(ba[0]), float(bb[1]), float(ba[1])))
                    last_book_t = t_sec

    print(f"[+] Eventos procesados. Trades capturados: {len(trades_list)}, Books muestreados: {len(books_list)}")
    if not trades_list or not books_list:
        print("[-] Datos insuficientes para auditar.")
        return

    trade_dt = np.dtype([("ts", np.float64), ("px", np.float64), ("qty", np.float64), ("side", np.float64)])
    book_dt = np.dtype([("ts", np.float64), ("bid", np.float64), ("ask", np.float64), ("bidsz", np.float64), ("asksz", np.float64)])

    trades_arr = np.array(trades_list, dtype=trade_dt)
    books_arr = np.array(books_list, dtype=book_dt)

    res_perp = pair_viability(trades_arr, books_arr, tick=tick, feemakerbps=2.0)
    res_spot = pair_viability(trades_arr, books_arr, tick=tick, feemakerbps=10.0)

    print("\n" + "=" * 70)
    print(f"RESULTADOS AUDITORÍA DE VIABILIDAD — {symbol} (Datos Reales)")
    print("=" * 70)
    print(f"Tick relativo (tickbps):             {res_perp.get('tickbps', 0):.4f} bps")
    print(f"Spread mediano:                     {res_perp.get('spreadbpsmed', 0):.4f} bps (1-tick frac: {res_perp.get('onetickfrac', 0):.1%})")
    print(f"Markout adverso 1s:                 {res_perp.get('markout1sbps', 0):.4f} bps")
    print(f"Markout adverso 5s:                 {res_perp.get('markout5sbps', 0):.4f} bps")
    print(f"Markout adverso 30s:                {res_perp.get('markout30sbps', 0):.4f} bps")
    print("-" * 70)
    print(f"EDGE 5s (Asumiendo 2 bps fee):      {res_perp.get('edge5sbps', 0):.4f} bps  <-- ¡NEGATIVO!")
    print(f"EDGE 5s (Spot real, 10 bps fee):    {res_spot.get('edge5sbps', 0):.4f} bps  <-- ¡RUINOSO!")
    print("-" * 70)
    print(f"Volumen por segundo:                ${res_perp.get('volpers', 0):,.2f}/s")
    print(f"Profundidad Top 1:                  ${res_perp.get('topdepthusd', 0):,.2f}")
    print(f"Queue Turnover:                     {res_perp.get('queueturnovers', 0):.1f} segundos")
    print("=" * 70)

if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "data/md_20261011_15.jsonl.gz"
    run(p)
