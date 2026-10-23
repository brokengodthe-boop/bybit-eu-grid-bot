"""
Calibración MFE/MAE para Antigravity.
Señal: Z-score rolling sobre log-retornos del mid muestreado a 1s.
Entrada: best ask (taker buy). Marcado: best bid (salida ejecutable).
Sin salidas: solo observa durante 10 minutos y registra excursiones.
"""
import sys
import math
import random
import glob
from collections import deque
from dataclasses import dataclass, field

import pandas as pd

# =====================================================================
# ADAPTADOR — AJUSTADO A NUESTRA API
# =====================================================================
from replay import iter_events
from engine import Engine

def ev_ts_ms(ev) -> int:
    """Timestamp del evento en milisegundos."""
    return getattr(ev, "ts", getattr(ev, "recv_ns", 0)) // 1000000

def is_trade(ev) -> bool:
    return type(ev).__name__ == "Trade"

def trade_signed_qty(ev) -> float:
    """Suma de qty firmada (+compra agresora, -venta) de un mensaje Trade."""
    # En Bybit y nuestro parser, buyer_maker=True significa que el maker fue el comprador,
    # por lo que el taker (agresor) fue el vendedor (-).
    # Si buyer_maker=False, el taker fue el comprador (+).
    return float(ev.qty) if not ev.buyer_is_maker else -float(ev.qty)

def top_of_book(engine, symbol):
    """Devuelve (best_bid, best_ask) como float, o None si el libro no es válido."""
    b = engine.books.get(symbol)
    if not b: return None
    bb, ba = b.best_bid(), b.best_ask()
    if bb is None or ba is None: return None
    return float(bb[0]), float(ba[0])
# =====================================================================

HORIZONS_S = [1, 5, 30, 120, 600]
HORIZONS_MS = [h * 1000 for h in HORIZONS_S]
LEVELS_BPS = [2, 5, 10, 20, 50, 100, 150]   # rejilla de first-touch

Z_WINDOW = 300          # nº de retornos de 1s en la ventana (5 min)
Z_TRIGGER = -2.5
COOLDOWN_MS = 30000     # no abrir otra señal en 30s
FLOW_WINDOW_MS = 5000
RANDOM_RATE = 1 / 600   # prob. por segundo de una entrada aleatoria (~1 cada 10 min)

class RollingZ:
    def __init__(self, n):
        self.n = n
        self.buf = deque()
        self.s = 0.0
        self.s2 = 0.0

    def push(self, x):
        """Devuelve el z de x respecto a la ventana ANTERIOR (sin look-ahead)."""
        z = None
        if len(self.buf) == self.n:
            m = self.s / self.n
            var = self.s2 / self.n - m * m
            if var > 1e-18:
                z = (x - m) / math.sqrt(var)
        self.buf.append(x)
        self.s += x
        self.s2 += x * x
        if len(self.buf) > self.n:
            o = self.buf.popleft()
            self.s -= o
            self.s2 -= o * o
        return z

@dataclass
class Probe:
    kind: str                 # "signal" | "random"
    t0: int
    entry: float              # ask
    spread_bps: float
    z: float
    flow5s: float
    hi: float = -math.inf     # max bid observado
    lo: float = math.inf      # min bid observado
    thi: int = 0
    tlo: int = 0
    hidx: int = 0
    out: dict = field(default_factory=dict)
    uptouch: dict = field(default_factory=dict)
    dntouch: dict = field(default_factory=dict)

    def update(self, ts, bid):
        if bid > self.hi: self.hi, self.thi = bid, ts - self.t0
        if bid < self.lo: self.lo, self.tlo = bid, ts - self.t0
        r = (bid / self.entry - 1) * 1e4
        for L in LEVELS_BPS:
            if L not in self.uptouch and r >= L:  self.uptouch[L] = ts - self.t0
            if L not in self.dntouch and r <= -L: self.dntouch[L] = ts - self.t0

    def snapshot(self, hs):
        self.out[f"mfe{hs}s"] = (self.hi / self.entry - 1) * 1e4
        self.out[f"mae{hs}s"] = (self.lo / self.entry - 1) * 1e4
        self.out[f"tmfe{hs}s"] = self.thi
        self.out[f"tmae{hs}s"] = self.tlo

    def row(self):
        d = dict(kind=self.kind, t0=self.t0, entry=self.entry,
                 spread_bps=self.spread_bps, z=self.z, flow5s=self.flow5s, **self.out)
        for L in LEVELS_BPS:
            d[f"up{L}ms"] = self.uptouch.get(L)   # None = no tocado en 10m
            d[f"dn{L}ms"] = self.dntouch.get(L)
        return d

def run(pattern, symbol, seed=7):
    rng = random.Random(seed)
    eng = Engine([symbol], request_snapshot=lambda s: None)
    rz = RollingZ(Z_WINDOW)
    flow = deque()            # (ts, signed_qty)
    active, done = [], []
    nextsec, lastmid, lastsig = None, None, -1018

    for ev in iter_events(pattern):
        if not hasattr(ev, "symbol") or ev.symbol != symbol:
            continue

        ts = ev_ts_ms(ev)
        eng.on_event(ev)

        if is_trade(ev):
            flow.append((ts, trade_signed_qty(ev)))
        while flow and flow[0][0] < ts - FLOW_WINDOW_MS:
            flow.popleft()

        tob = top_of_book(eng, symbol)
        if tob is None:
            continue
        bid, ask = tob
        if bid <= 0 or ask <= bid:
            continue

        # 1) Actualizar sondas vivas (snapshot ANTES de incluir el evento que cruza h)
        still = []
        for p in active:
            el = ts - p.t0
            while p.hidx < len(HORIZONS_MS) and el > HORIZONS_MS[p.hidx]:
                p.snapshot(HORIZONS_S[p.hidx])
                p.hidx += 1
            if p.hidx == len(HORIZONS_MS):
                done.append(p.row())
            else:
                p.update(ts, bid)
                still.append(p)
        active = still

        # 2) Muestreo del mid a 1s y señal
        mid = 0.5 * (bid + ask)
        if nextsec is None:
            nextsec = (ts // 1000 + 1) * 1000
        firedz = None
        while ts >= nextsec:          # cierra cada segundo con el último mid
            if lastmid is not None:
                z = rz.push(math.log(mid / lastmid))
                if z is not None and z <= Z_TRIGGER:
                    firedz = z
            lastmid = mid
            nextsec += 1000
            if rng.random() < RANDOM_RATE:
                active.append(Probe("random", ts, ask, (ask/bid-1)*1e4, float("nan"),
                                    sum(q for _, q in flow)))

        if firedz is not None and ts - lastsig >= COOLDOWN_MS:
            lastsig = ts
            active.append(Probe("signal", ts, ask, (ask/bid-1)*1e4, firedz,
                                sum(q for _, q in flow)))

    # Las sondas sin completar los 10m se descartan (sesgo de fin de fichero)
    return pd.DataFrame(done)

def report(df, fee_bps_roundtrip=20.0):
    if df.empty:
        print("No hay datos en el DataFrame.")
        return None

    print(f"\nSondas: {df['kind'].value_counts().to_dict()}")
    q = [0.1, 0.25, 0.5, 0.75, 0.9]
    for k, g in df.groupby("kind"):
        print(f"\n=== {k} === (spread mediano en entrada: {g.spread_bps.median():.2f} bps)")
        rows = {}
        for h in HORIZONS_S:
            if f"mfe{h}s" in g.columns:
                rows[f"{h}s MFE"] = g[f"mfe{h}s"].quantile(q)
                rows[f"{h}s MAE"] = g[f"mae{h}s"].quantile(q)
        print(pd.DataFrame(rows).T.round(2))

    # Prob. de TP antes que SL (exacta, gracias a first-touch), neta de comisiones
    print(f"\nP(TP antes que SL en 10m) y expectativa neta (fees RT={fee_bps_roundtrip} bps)")
    sig = df[df.kind == "signal"]
    if sig.empty:
        return None

    out = []
    for tp in LEVELS_BPS:
        for sl in LEVELS_BPS:
            u = pd.to_numeric(sig[f"up{tp}ms"])
            d = pd.to_numeric(sig[f"dn{sl}ms"])
            win = u.notna() & (d.isna() | (u < d))
            loss = d.notna() & (u.isna() | (d <= u))
            timeout = ~(win | loss)
            
            # ev = (win.mean()*tp - loss.mean()*sl + timeout.mean()*sig["mfe600s"].where(timeout).median(skipna=True)*0 - fee_bps_roundtrip)
            # Aproximación conservadora timeout=0
            ev = (win.mean() * tp - loss.mean() * sl - fee_bps_roundtrip)
            
            out.append(dict(tp=tp, sl=sl, pwin=win.mean(), ploss=loss.mean(),
                            ptimeout=timeout.mean(), evbps=ev))
    
    grid = pd.DataFrame(out)
    print(grid.pivot(index="tp", columns="sl", values="evbps").round(2))
    return grid

if __name__ == "__main__":
    pattern = sys.argv[1] if len(sys.argv) > 1 else "data/md_*.jsonl.gz"
    symbol = sys.argv[2] if len(sys.argv) > 2 else "BTCUSDT"
    df = run(pattern, symbol)
    df.to_parquet("mfemae.parquet")
    report(df)
