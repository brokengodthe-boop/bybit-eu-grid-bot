#!/usr/bin/env python3
"""
eval_queue_markouts.py — Pasada 2: Simulación de órdenes virtuales con modelo de cola + markouts condicionados.
Evalúa los fills bajo tres modelos de cola (optimista, pesimista, pro-rata con potencia k)
sobre las tablas Parquet producidas por dump_tables.py.
"""
import argparse, os, sys
import numpy as np
import polars as pl

try:
    from numba import njit
except ImportError:
    def njit(*args, **kwargs):
        def decorator(f):
            return f
        return decorator

MODELS = {"optimista": (0, 0.0), "pesimista": (1, 0.0), "prorata_k1": (2, 1.0)}
HORIZONS_MS = (0, 1000, 5000, 30000)

def load_events(root: str, sym: str, date: str):
    base = f"{root}/symbol={sym}/date={date}"
    
    bbo = (pl.scan_parquet(f"{base}/bbo_*.parquet")
           .select([
               pl.col("ts_ex").cast(pl.Int64).alias("ts"),
               pl.col("bid_k").cast(pl.Int64).alias("bidk"),
               pl.col("ask_k").cast(pl.Int64).alias("askk"),
               pl.col("bid_sz").cast(pl.Float64).alias("bidsz"),
               pl.col("ask_sz").cast(pl.Float64).alias("asksz"),
           ])
           .with_columns(pl.lit(1, pl.Int8).alias("typ")))

    tr = (pl.scan_parquet(f"{base}/trades_*.parquet")
          .select([
              pl.col("ts_ex").cast(pl.Int64).alias("ts"),
              pl.col("px_k").cast(pl.Int64).alias("trk"),
              pl.col("qty").cast(pl.Float64).alias("trqty"),
              pl.col("side").cast(pl.Int8).alias("trside"),
          ])
          .with_columns(pl.lit(0, pl.Int8).alias("typ")))

    ev = (pl.concat([bbo, tr], how="diagonal_relaxed")
          .sort(["ts", "typ"])
          .with_columns(pl.col(["bidk", "askk", "bidsz", "asksz"]).forward_fill())
          .drop_nulls(["bidk", "askk"])
          .with_columns([
              pl.col("trk").fill_null(0),
              pl.col("trqty").fill_null(0.0),
              pl.col("trside").fill_null(0),
          ])
          .collect())

    def to_np(col, dtype):
        return ev[col].to_numpy().astype(dtype)

    return (to_np("ts", np.int64), to_np("typ", np.int8),
            to_np("bidk", np.int64), to_np("bidsz", np.float64),
            to_np("askk", np.int64), to_np("asksz", np.float64),
            to_np("trk", np.int64), to_np("trqty", np.float64), to_np("trside", np.int8))

@njit(cache=True)
def sim_order(ts, typ, bk, bs, ak, asz, tk, tq, tsd, i0, s, k, q0, tend, model, kexp, qord):
    """Devuelve el índice del evento de fill o -1 si expira."""
    n = ts.shape[0]
    Q = q0
    prevS = q0
    traded = 0.0
    visible = True

    for i in range(i0 + 1, n):
        if ts[i] > tend:
            return -1

        if typ[i] == 0:  # Trade
            if tsd[i] == -s:  # Taker golpea contra nuestro lado
                d = s * (tk[i] - k)
                if d < 0:
                    return i  # Trade-through: precio nos atravesó
                if d == 0:
                    Q -= tq[i]
                    traded += tq[i]
                    if Q < 0.0 and -Q >= qord:
                        return i
        else:  # BBO
            opp = ak[i] if s == 1 else bk[i]
            if s * (opp - k) <= 0:
                return i  # Contraparte cruza nuestro precio

            L = bk[i] if s == 1 else ak[i]
            S = bs[i] if s == 1 else asz[i]

            if L == k:
                if visible:
                    dec = prevS - S - traded  # Cancelaciones no explicadas por trades
                    if dec > 0.0 and Q > 0.0:
                        if model == 0:
                            share = 1.0  # Optimista: todo por delante
                        elif model == 1:
                            share = 0.0  # Pesimista: todo por detrás
                        else:
                            B = max(prevS - Q, 0.0)
                            fa = Q ** kexp
                            fb = B ** kexp
                            share = fa / (fa + fb) if (fa + fb) > 0.0 else 1.0
                        Q -= dec * share

                if Q > S:
                    Q = S
                prevS = S
                traded = 0.0
                visible = True

            elif s * (L - k) < 0:  # Nuestro nivel se vació: quedamos solos
                if Q > 0.0:
                    Q = 0.0
                prevS = 0.0
                traded = 0.0
                visible = True

            else:  # Nos mejoraron el precio: nivel invisible desde BBO
                visible = False
                traded = 0.0

    return -1

@njit(cache=True)
def run_all(ts, typ, bk, bs, ak, asz, tk, tq, tsd, samples, lat, live, model, kexp, qord, out):
    m = 0
    for t in samples:
        j = np.searchsorted(ts, t, side="right") - 1
        if j < 0 or ak[j] <= bk[j]:
            continue

        tl = t + lat
        i0 = np.searchsorted(ts, tl, side="right") - 1

        for s in (1, -1):
            k = bk[j] if s == 1 else ak[j]
            curr_k = bk[i0] if s == 1 else ak[i0]
            if curr_k != k:
                continue  # El precio se movió durante la latencia

            q0 = bs[i0] if s == 1 else asz[i0]
            fi = sim_order(ts, typ, bk, bs, ak, asz, tk, tq, tsd, i0, s, k, q0,
                           tl + live, model, kexp, qord)

            out[m, 0] = s
            out[m, 1] = k
            out[m, 2] = q0
            out[m, 3] = tl
            out[m, 4] = ts[fi] if fi >= 0 else -1.0
            m += 1

    return m

def calc_markouts(ts, bk, ak, side, k, tf, h):
    midk = (bk + ak) / 2.0
    res = np.full(tf.shape[0], np.nan)
    ok = (tf >= 0) & (tf + h <= ts[-1])
    if np.any(ok):
        idx = np.searchsorted(ts, tf[ok] + h, side="right") - 1
        res[ok] = side[ok] * (midk[idx] - k[ok]) / k[ok] * 1e4
    return res

def process(root, outroot, sym, date, a):
    print(f"[*] Cargando eventos para {sym} ({date})...")
    ev = load_events(root, sym, date)
    ts, typ, bk, bs, ak, asz, tk, tq, tsd = ev

    samples = np.arange(ts[0] + a.step_ms, ts[-1] - a.live_ms - 30000, a.step_ms, dtype=np.int64)
    print(f"[*] Muestreando {len(samples)} puntos temporales cada {a.step_ms}ms...")

    models = dict(MODELS)
    for kx in a.kexp_grid:
        models[f"powk_{kx:g}"] = (2, float(kx))

    frames = []
    for name, (mcode, kexp) in models.items():
        out = np.empty((2 * samples.shape[0], 5), dtype=np.float64)
        m = run_all(ts, typ, bk, bs, ak, asz, tk, tq, tsd, samples, a.lat_ms, a.live_ms, mcode, kexp, a.qord, out)
        o = out[:m]
        side, k, tf = o[:, 0], o[:, 1], o[:, 4]

        cols = {
            "symbol": sym,
            "date": date,
            "model": name,
            "side": side.astype(np.int8),
            "k": k.astype(np.int64),
            "q0": o[:, 2],
            "tlive": o[:, 3].astype(np.int64),
            "tfill": tf.astype(np.int64),
            "filled": tf >= 0,
        }
        for h in HORIZONS_MS:
            cols[f"mo{h // 1000}s"] = calc_markouts(ts, bk, ak, side, k, tf, h)

        frames.append(pl.DataFrame(cols))

    fills = pl.concat(frames).with_columns([
        (pl.col("mo5s") - a.fee_bps).alias("edge5net"),
        ((pl.col("tfill") - pl.col("tlive")) / 1000.0).alias("ttfs"),
    ])

    d = f"{outroot}/symbol={sym}/date={date}"
    os.makedirs(d, exist_ok=True)
    out_file = f"{d}/fills.parquet"
    fills.write_parquet(out_file, compression="zstd")
    print(f"[+] Fills guardados en {out_file} ({len(fills)} simulaciones)")
    return fills

def summarize(fills):
    f = fills.filter(pl.col("filled") & pl.col("edge5net").is_not_null())
    base = fills.group_by(["symbol", "model"]).agg([
        pl.len().alias("norders"),
        pl.col("filled").mean().alias("fillrate"),
    ])
    naive = f.group_by(["symbol", "model"]).agg([
        pl.len().alias("nfills"),
        pl.col("mo0s").mean().alias("mo0s"),
        pl.col("mo1s").mean().alias("mo1s"),
        pl.col("mo5s").mean().alias("mo5s"),
        pl.col("mo30s").mean().alias("mo30s"),
        pl.col("edge5net").mean().alias("edge5net"),
        (pl.col("edge5net").mean() / (pl.col("edge5net").std() / pl.len().sqrt())).alias("tnaive"),
    ])
    clus = (f.with_columns((pl.col("tfill") // 60000).alias("minute"))
            .group_by(["symbol", "model", "minute"]).agg(pl.col("edge5net").mean().alias("e"))
            .group_by(["symbol", "model"]).agg([
                pl.len().alias("nmin"),
                (pl.col("e").mean() / (pl.col("e").std() / pl.len().sqrt())).alias("tcluster1m"),
            ]))
    return base.join(naive, on=["symbol", "model"]).join(clus, on=["symbol", "model"]).sort(["symbol", "model"])

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="parquet")
    p.add_argument("--out", default="fills")
    p.add_argument("--date", required=True)
    p.add_argument("--symbols", nargs="+", default=["USUSDT", "ATOMUSDT", "XLMUSDT", "TRUMPUSDT", "ADAUSDT"])
    p.add_argument("--step-ms", type=int, default=5000)
    p.add_argument("--live-ms", type=int, default=5000)
    p.add_argument("--lat-ms", type=int, default=50)
    p.add_argument("--fee-bps", type=float, default=2.0)
    p.add_argument("--qord", type=float, default=1e-9)
    p.add_argument("--kexp-grid", type=float, nargs="*", default=[0.5, 2.0, 3.0])
    a = p.parse_args()

    dfs = []
    for s in a.symbols:
        path = f"{a.root}/symbol={s}/date={a.date}"
        if not os.path.exists(path):
            print(f"[-] Omitiendo {s}: no existen parquets en {path}")
            continue
        dfs.append(process(a.root, a.out, s, a.date, a))

    if not dfs:
        print("[-] No se procesaron símbolos.")
        return

    allf = pl.concat(dfs)
    summ = summarize(allf)
    os.makedirs(a.out, exist_ok=True)
    summ.write_parquet(f"{a.out}/summary_{a.date}.parquet")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with pl.Config(tbl_cols=-1, tbl_rows=-1, ascii_tables=True):
        print("\n" + "=" * 90)
        print(f"RESUMEN DE FILLS Y MARKOUTS (FECHA {a.date})")
        print("=" * 90)
        print(summ)

if __name__ == "__main__":
    main()
