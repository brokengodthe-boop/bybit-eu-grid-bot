#!/usr/bin/env python3
"""
gen_signals.py — Generador causal de señales de FlowExhaustion para Pasada 3.
Procesa secuencialmente BBO y Trades para generar signals.parquet con sigbid y sigask cada 100ms.
"""
import os, glob, math
import numpy as np
import polars as pl

class Ewma:
    __slots__ = ("tau", "v", "t")
    def __init__(self, tau: float):
        self.tau = tau
        self.v = 0.0
        self.t = None

    def decay(self, t: float):
        if self.t is not None and t > self.t:
            self.v = self.v * math.exp(-(t - self.t) / self.tau)
        self.t = t

    def add_count(self, t: float, x: float):
        self.decay(t)
        self.v += x / self.tau

    def update_level(self, t: float, x: float):
        if self.t is None:
            self.v = x
            self.t = t
            return
        dt = t - self.t
        if dt > 0:
            a = 1.0 - math.exp(-dt / self.tau)
            self.t = t
            self.v += a * (x - self.v)

class TwoWayFlowExhaustion:
    def __init__(self, levels: int = 5):
        # Para ventas agresivas (disparo de sigbid)
        self.sell_fast = Ewma(1.0)
        self.sell_slow = Ewma(10.0)
        self.peak_sell_d = 0.0

        # Para compras agresivas (disparo de sigask)
        self.buy_fast = Ewma(1.0)
        self.buy_slow = Ewma(10.0)
        self.peak_buy_d = 0.0

        # Métricas de Order Book
        self.obifast = Ewma(0.5)
        self.obislow = Ewma(5.0)
        self.vmid = Ewma(1.0)
        self.vmp = Ewma(1.0)
        self.midslow = Ewma(30.0)

        self.lastmid = None
        self.lastmp = None

    def on_trade(self, t: float, qty: float, side: int):
        # side: +1 taker buy, -1 taker sell
        if side < 0:
            self.sell_fast.add_count(t, qty)
            self.sell_slow.add_count(t, qty)
            d_sell = self.sell_fast.v / max(self.sell_slow.v, 1e-12)
            self.peak_sell_d = max(self.peak_sell_d * 0.999, d_sell)
        else:
            self.buy_fast.add_count(t, qty)
            self.buy_slow.add_count(t, qty)
            d_buy = self.buy_fast.v / max(self.buy_slow.v, 1e-12)
            self.peak_buy_d = max(self.peak_buy_d * 0.999, d_buy)

    def on_bbo(self, t: float, bid_p: float, bid_s: float, ask_p: float, ask_s: float):
        mid = (bid_p + ask_p) / 2.0
        tot_s = bid_s + ask_s
        mp = (bid_p * ask_s + ask_p * bid_s) / max(tot_s, 1e-12)
        obi = (bid_s - ask_s) / max(tot_s, 1e-12)

        self.obifast.update_level(t, obi)
        self.obislow.update_level(t, obi)

        if self.lastmid is not None:
            self.vmid.update_level(t, mid - self.lastmid)
            self.vmp.update_level(t, mp - self.lastmp)

        self.lastmid = mid
        self.lastmp = mp
        self.midslow.update_level(t, mid)

    def signals(self, t: float, sigma_ticks_30s: float, dropk: float = 1.5):
        if self.lastmid is None or self.midslow.t is None:
            return False, False

        self.sell_fast.decay(t)
        self.sell_slow.decay(t)
        d_sell = self.sell_fast.v / max(self.sell_slow.v, 1e-12)

        self.buy_fast.decay(t)
        self.buy_slow.decay(t)
        d_buy = self.buy_fast.v / max(self.buy_slow.v, 1e-12)

        # 1. Agotamiento vendedor -> sigbid
        fell = (self.lastmid - self.midslow.v) < -dropk * sigma_ticks_30s
        decel_sell = self.peak_sell_d > 1.8 and d_sell < 0.8
        obiturn_bid = self.obifast.v > self.obislow.v and self.obifast.v > -0.2
        sigbid = bool(fell and decel_sell and obiturn_bid)
        if sigbid:
            self.peak_sell_d = 0.0

        # 2. Agotamiento comprador -> sigask
        rose = (self.lastmid - self.midslow.v) > dropk * sigma_ticks_30s
        decel_buy = self.peak_buy_d > 1.8 and d_buy < 0.8
        obiturn_ask = self.obifast.v < self.obislow.v and self.obifast.v < 0.2
        sigask = bool(rose and decel_buy and obiturn_ask)
        if sigask:
            self.peak_buy_d = 0.0

        return sigbid, sigask

def generate_signals_for_symbol(root: str, sym: str):
    base = f"{root}/symbol={sym}/date=20261011"
    bbo_files = sorted(glob.glob(f"{base}/bbo_*.parquet"))
    tr_files = sorted(glob.glob(f"{base}/trades_*.parquet"))

    if not bbo_files or not tr_files:
        print(f"[!] No hay archivos para {sym}")
        return

    bbo_df = pl.read_parquet(bbo_files).select([
        pl.col("ts_ex").cast(pl.Int64).alias("ts"),
        pl.col("bid_k").cast(pl.Int64),
        pl.col("bid_sz").cast(pl.Float64),
        pl.col("ask_k").cast(pl.Int64),
        pl.col("ask_sz").cast(pl.Float64),
    ]).sort("ts")

    tr_df = pl.read_parquet(tr_files).select([
        pl.col("ts_ex").cast(pl.Int64).alias("ts"),
        pl.col("qty").cast(pl.Float64),
        pl.col("side").cast(pl.Int8),
    ]).sort("ts")

    ts_bbo = bbo_df["ts"].to_numpy()
    bk = bbo_df["bid_k"].to_numpy().astype(float)
    bsz = bbo_df["bid_sz"].to_numpy()
    ak = bbo_df["ask_k"].to_numpy().astype(float)
    asz = bbo_df["ask_sz"].to_numpy()
    mid = 0.5 * (bk + ak)

    # Estimar sigma_ticks en 30s
    t_start, t_end = ts_bbo[0], ts_bbo[-1]
    grid_30s = np.arange(t_start, t_end, 30_000)
    idx_grid = np.searchsorted(ts_bbo, grid_30s, side="right") - 1
    idx_grid = np.clip(idx_grid, 0, len(mid) - 1)
    sigma_30s = max(float(np.std(np.diff(mid[idx_grid]))), 1.0)

    print(f"[*] {sym}: {len(bbo_df)} BBOs, {len(tr_df)} Trades, sigma_30s = {sigma_30s:.2f} ticks")

    # Unir cronológicamente BBO y Trades
    events = []
    # typ 0 = BBO, typ 1 = Trade
    for i in range(len(bbo_df)):
        events.append((ts_bbo[i], 0, bk[i], bsz[i], ak[i], asz[i]))
    
    ts_tr = tr_df["ts"].to_numpy()
    qty_tr = tr_df["qty"].to_numpy()
    side_tr = tr_df["side"].to_numpy()
    for i in range(len(tr_df)):
        events.append((ts_tr[i], 1, qty_tr[i], side_tr[i], 0.0, 0.0))

    events.sort(key=lambda x: (x[0], x[1])) # trades antes que book si mismo ts

    fe = TwoWayFlowExhaustion()
    
    # Muestrear cada 100ms
    sample_grid = np.arange(t_start + 60_000, t_end - 60_000, 100, dtype=np.int64)
    sig_ts = []
    sig_bid = []
    sig_ask = []

    evt_idx = 0
    n_evts = len(events)

    for st in sample_grid:
        while evt_idx < n_evts and events[evt_idx][0] <= st:
            e = events[evt_idx]
            evt_idx += 1
            if e[1] == 0: # BBO
                fe.on_bbo(e[0] / 1000.0, e[2], e[3], e[4], e[5])
            else: # Trade
                fe.on_trade(e[0] / 1000.0, e[2], int(e[3]))

        sb, sa = fe.signals(st / 1000.0, sigma_30s)
        if sb or sa:
            sig_ts.append(st)
            sig_bid.append(sb)
            sig_ask.append(sa)

    out_df = pl.DataFrame({
        "ts": np.array(sig_ts, dtype=np.int64),
        "sigbid": np.array(sig_bid, dtype=bool),
        "sigask": np.array(sig_ask, dtype=bool),
    })

    out_path = f"{root}/symbol={sym}/signals.parquet"
    out_df.write_parquet(out_path, compression="zstd")
    print(f"[+] Guardado {out_path}: {len(out_df)} puntos (sigbid={sum(sig_bid)}, sigask={sum(sig_ask)})")

def main():
    symbols = ["USUSDT", "ATOMUSDT", "XLMUSDT", "TRUMPUSDT", "ADAUSDT"]
    for s in symbols:
        generate_signals_for_symbol("parquet", s)

if __name__ == "__main__":
    main()
