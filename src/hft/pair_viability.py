"""
Auditoría Cuantitativa de Viabilidad de Pares (Pair Viability).
Mide si un par ofrece edge económico neto para Market Making pasivo (Maker)
teniendo en cuenta comisiones base (2 bps) y markout de selección adversa.
"""
import numpy as np

def pair_viability(trades, books, tick: float, feemakerbps: float = 2.0, horizons=(1, 5, 30)) -> dict:
    """
    trades: np.ndarray estructurado con campos:
            ts (float en s), px (float), qty (float), side (+1 compra agresiva / taker buy, -1 venta / taker sell)
    books : np.ndarray con campos:
            ts (float en s), bid (float), ask (float), bidsz (float), asksz (float)
    """
    if len(books) == 0 or len(trades) == 0:
        return {}

    mid = (books["bid"] + books["ask"]) / 2.0
    spreadbps = (books["ask"] - books["bid"]) / mid * 1e4
    tickbps = tick / float(np.median(mid)) * 1e4
    onetickfrac = float(np.mean(np.isclose(books["ask"] - books["bid"], tick)))

    idx = np.searchsorted(books["ts"], trades["ts"])
    idx = np.clip(idx, 0, len(books) - 1)
    
    out = {
        "tickbps": tickbps,
        "spreadbpsmed": float(np.median(spreadbps)),
        "onetickfrac": float(onetickfrac),
    }
    
    for h in horizons:
        j = np.clip(np.searchsorted(books["ts"], trades["ts"] + h), 0, len(books) - 1)
        # El maker está en el lado opuesto del agresor:
        # Si agresor compra (+1), el maker vendió -> pierde si el mid sube.
        # mk mide cuánto se mueve el mid a favor del agresor (en contra del maker).
        mk = trades["side"] * (mid[j] - mid[idx]) / mid[idx] * 1e4
        out[f"markout{h}sbps"] = float(np.mean(mk))
        
    half_spread = float(np.median(spreadbps)) / 2.0
    out["edge5sbps"] = half_spread - out.get("markout5sbps", 0.0) - feemakerbps
    
    dur = float(books["ts"][-1] - books["ts"][0])
    if dur > 0:
        out["volpers"] = float(np.sum(trades["qty"] * trades["px"]) / dur)
    else:
        out["volpers"] = 0.0
        
    out["topdepthusd"] = float(np.median((books["bidsz"] + books["asksz"]) / 2.0 * mid))
    out["queueturnovers"] = out["topdepthusd"] / max(out["volpers"] / 2.0, 1e-9)
    
    return out
