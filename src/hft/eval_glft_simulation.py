#!/usr/bin/env python3
"""
eval_glft_simulation.py — Pasada 3: órdenes a profundidad k, GLFT, FlowExhaustion vs placebo, calibración A/kappa.
Auditoría adversarial completa con split train/test OOS, block-bootstrap CI y criterio Go/No-Go institucional.
"""
import argparse, json, os, hashlib, glob
import numpy as np
import polars as pl
import numba as nb
from numba import types
from numba.typed import Dict

FEEBPS = 2.0
TSPERS = 1000
HZ = (0, 1, 5, 30)
QMODELS = {"optimista": -1.0, "pesimista": -2.0, "proratak1": 1.0}

# ---------------------------------------------------------------- carga
@nb.njit
def extract_deltas(ts, bk, bsz, ak, asz):
    n = ts.shape[0]
    max_ev = n * 40
    out_ts = np.empty(max_ev, np.int64)
    out_side = np.empty(max_ev, np.int8)
    out_px = np.empty(max_ev, np.int64)
    out_sz = np.empty(max_ev, np.float64)
    cnt = 0
    t0 = ts[0]
    for j in range(10):
        if bsz[0, j] > 0:
            out_ts[cnt] = t0; out_side[cnt] = 1; out_px[cnt] = bk[0, j]; out_sz[cnt] = bsz[0, j]; cnt += 1
        if asz[0, j] > 0:
            out_ts[cnt] = t0; out_side[cnt] = -1; out_px[cnt] = ak[0, j]; out_sz[cnt] = asz[0, j]; cnt += 1
    for r in range(1, n):
        t = ts[r]
        for j in range(10):
            p = bk[r, j]; s = bsz[r, j]
            if p == 0: continue
            matched = False
            for prev_j in range(10):
                if bk[r-1, prev_j] == p:
                    if bsz[r-1, prev_j] == s: matched = True
                    break
            if not matched:
                out_ts[cnt] = t; out_side[cnt] = 1; out_px[cnt] = p; out_sz[cnt] = s; cnt += 1
        for prev_j in range(10):
            p_old = bk[r-1, prev_j]
            if p_old == 0: continue
            found = False
            for j in range(10):
                if bk[r, j] == p_old: found = True; break
            if not found:
                out_ts[cnt] = t; out_side[cnt] = 1; out_px[cnt] = p_old; out_sz[cnt] = 0.0; cnt += 1
        for j in range(10):
            p = ak[r, j]; s = asz[r, j]
            if p == 0: continue
            matched = False
            for prev_j in range(10):
                if ak[r-1, prev_j] == p:
                    if asz[r-1, prev_j] == s: matched = True
                    break
            if not matched:
                out_ts[cnt] = t; out_side[cnt] = -1; out_px[cnt] = p; out_sz[cnt] = s; cnt += 1
        for prev_j in range(10):
            p_old = ak[r-1, prev_j]
            if p_old == 0: continue
            found = False
            for j in range(10):
                if ak[r, j] == p_old: found = True; break
            if not found:
                out_ts[cnt] = t; out_side[cnt] = -1; out_px[cnt] = p_old; out_sz[cnt] = 0.0; cnt += 1
    return out_ts[:cnt], out_side[:cnt], out_px[:cnt], out_sz[:cnt]

def load(root, sym, tick):
    base = f"{root}/symbol={sym}/date=20261011"
    b_files = sorted(glob.glob(f"{base}/bbo_*.parquet"))
    t_files = sorted(glob.glob(f"{base}/trades_*.parquet"))
    k_files = sorted(glob.glob(f"{base}/book_*.parquet"))

    b_df = pl.read_parquet(b_files).sort("ts_ex")
    bbo = {
        "ts": b_df["ts_ex"].cast(pl.Int64).to_numpy(),
        "bid": b_df["bid_k"].to_numpy().astype(float),
        "ask": b_df["ask_k"].to_numpy().astype(float),
    }
    bbo["mid"] = 0.5 * (bbo["bid"] + bbo["ask"])

    t_df = pl.read_parquet(t_files).sort("ts_ex")
    t_ts = t_df["ts_ex"].cast(pl.Int64).to_numpy()
    t_side = t_df["side"].cast(pl.Int8).to_numpy()
    t_px = t_df["px_k"].cast(pl.Int64).to_numpy()
    t_sz = t_df["qty"].cast(pl.Float64).to_numpy()
    t_typ = np.ones(len(t_ts), dtype=np.int8)

    k_df = pl.read_parquet(k_files).sort("ts_ex")
    k_ts = k_df["ts_ex"].cast(pl.Int64).to_numpy()
    bk = np.column_stack([k_df[f"b{i}_k"].to_numpy() for i in range(10)])
    bsz = np.column_stack([k_df[f"b{i}_sz"].to_numpy() for i in range(10)])
    ak = np.column_stack([k_df[f"a{i}_k"].to_numpy() for i in range(10)])
    asz = np.column_stack([k_df[f"a{i}_sz"].to_numpy() for i in range(10)])

    k_dts, k_dside, k_dpx, k_dsz = extract_deltas(k_ts, bk, bsz, ak, asz)
    k_dtyp = np.zeros(len(k_dts), dtype=np.int8)

    all_ts = np.concatenate([t_ts, k_dts])
    all_typ = np.concatenate([t_typ, k_dtyp])
    all_side = np.concatenate([t_side, k_dside])
    all_px = np.concatenate([t_px, k_dpx])
    all_sz = np.concatenate([t_sz, k_dsz])

    order = np.lexsort((-all_typ, all_ts))
    ev = {
        "ts": all_ts[order],
        "typ": all_typ[order],
        "side": all_side[order],
        "px": all_px[order],
        "sz": all_sz[order],
    }
    return bbo, ev

# ---------------------------------------------------------------- simulador
@nb.njit(cache=True)
def bestworst(bk, isb):
    best = 0; worst = 0; first = True
    for p in bk.keys():
        if first:
            best = p; worst = p; first = False
        elif isb:
            best = max(best, p); worst = min(worst, p)
        else:
            best = min(best, p); worst = max(worst, p)
    return best, worst

@nb.njit(cache=True)
def simdeeporders(evts, evtyp, evside, evpx, evsz,
                  ot, oside, ooff, ttl, lat, kpow):
    n = ot.shape[0]
    bids = Dict.empty(key_type=types.int64, value_type=types.float64)
    asks = Dict.empty(key_type=types.int64, value_type=types.float64)
    pq = np.zeros(n, np.int64)
    ahead = np.zeros(n, np.float64)
    lvl = np.zeros(n, np.float64)
    pend = np.zeros(n, np.float64)
    q0 = np.full(n, np.nan, np.float64)
    state = np.zeros(n, np.int8)   # 1 activa, 2 fill, 3 expira, 4 sin libro
    fts = np.full(n, -1, np.int64)
    kind = np.zeros(n, np.int8)
    act = np.empty(n, np.int64)
    na = 0
    nxt = 0

    for e in range(evts.shape[0]):
        ts = evts[e]
        while nxt < n and ot[nxt] + lat <= ts:
            i = nxt; nxt += 1
            if len(bids) == 0 or len(asks) == 0:
                state[i] = 4; continue
            isb = oside[i] == 1
            bk = bids if isb else asks
            best, worst = bestworst(bk, isb)
            p = best - ooff[i] if isb else best + ooff[i]
            pq[i] = p
            if p in bk:
                ahead[i] = bk[p]; lvl[i] = bk[p]
            elif (isb and p < worst) or ((not isb) and p > worst):
                ahead[i] = np.inf; lvl[i] = 0.0
            else:
                ahead[i] = 0.0; lvl[i] = 0.0
            q0[i] = ahead[i]; state[i] = 1; act[na] = i; na += 1

        typ = evtyp[e]; s = evside[e]; px = evpx[e]; sz = evsz[e]
        if typ == 0:
            bk = bids if s == 1 else asks
            if sz <= 0.0:
                if px in bk:
                    del bk[px]
            else:
                bk[px] = sz

        j = 0
        while j < na:
            i = act[j]
            if ts > ot[i] + lat + ttl:
                state[i] = 3; act[j] = act[na - 1]; na -= 1; continue
            isb = oside[i] == 1
            if typ == 1 and s == -oside[i]:
                if (isb and px < pq[i]) or ((not isb) and px > pq[i]):
                    state[i] = 2; fts[i] = ts; kind[i] = 1           # trade-through
                elif px == pq[i] and ahead[i] != np.inf:
                    if sz <= ahead[i]:
                        ahead[i] -= sz; pend[i] += sz
                    else:
                        state[i] = 2; fts[i] = ts; kind[i] = 2       # cola agotada
            elif typ == 0 and s == oside[i] and px == pq[i]:
                S = sz if sz > 0.0 else 0.0
                prev = lvl[i]
                if ahead[i] == np.inf:
                    ahead[i] = S
                elif S < prev:
                    d = prev - S
                    c = min(d, pend[i]); pend[i] -= c; d -= c
                    if d > 0.0:
                        if kpow == -1.0:
                            share = 1.0
                        elif kpow == -2.0:
                            share = 0.0
                        else:
                            behind = max(prev - ahead[i], 0.0)
                            wa = ahead[i] ** kpow; wb = behind ** kpow
                            share = wa / (wa + wb) if wa + wb > 0 else 0.0
                        ahead[i] -= d * share
                if ahead[i] != np.inf:
                    ahead[i] = min(max(ahead[i], 0.0), S)
                lvl[i] = S
            if state[i] == 2:
                act[j] = act[na - 1]; na -= 1; continue
            j += 1
    return pq, fts, kind, q0, state

# ---------------------------------------------------------------- métricas
def markouts(oside, pq, fts, kind, bbo, tickbps):
    m = fts >= 0
    t, sd, p = fts[m], oside[m], pq[m].astype(float)
    out = {"t": t, "kind": kind[m]}
    mean_p = float(np.mean(p)) if len(p) > 0 else 1.0
    for h in HZ:
        tt = t + h * TSPERS
        idx = np.searchsorted(bbo["ts"], tt, side="right") - 1
        idx = np.clip(idx, 0, len(bbo["mid"]) - 1)
        moticks = sd * (bbo["mid"][idx] - p)
        mo = moticks * tickbps * mean_p / np.maximum(p, 1e-9)
        mo[tt > bbo["ts"][-1]] = np.nan
        moticks[tt > bbo["ts"][-1]] = np.nan
        out[f"mo{h}"] = mo
        out[f"mo{h}ticks"] = moticks
    return out

def bootci(x, t, nboot=2000, blocks=60, seed=0):
    ok = ~np.isnan(x)
    x, t = x[ok], t[ok]
    if len(x) < 30:
        return float(np.nan), float(np.nan), float(np.nan)
    _, inv = np.unique(t // (blocks * TSPERS), return_inverse=True)
    sums, cnt = np.bincount(inv, x), np.bincount(inv)
    if len(cnt) < 2:
        return float(x.mean()), float(x.mean()), float(x.mean())
    idx = np.random.default_rng(seed).integers(0, len(cnt), (nboot, len(cnt)))
    denom = np.maximum(cnt[idx].sum(1), 1)
    bs = sums[idx].sum(1) / denom
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return float(x.mean()), float(lo), float(hi)

# ---------------------------------------------------------------- calibración
def calibratetrades(ev, bbo, deltas=np.arange(0.5, 5.0, 1.0)):
    tr = ev["typ"] == 1
    ts, px, sd = ev["ts"][tr], ev["px"][tr], ev["side"][tr]
    j = np.searchsorted(bbo["ts"], ts, side="left") - 1
    ok = j >= 0
    if not np.any(ok):
        return {"A": np.nan, "kappa": np.nan}
    dist = sd[ok] * (px[ok] - bbo["mid"][j[ok]])
    d = (pl.DataFrame({"ts": ts[ok], "sd": sd[ok], "d": dist})
         .group_by(["ts", "sd"]).agg(pl.col("d").max())["d"].to_numpy())
    T = (bbo["ts"][-1] - bbo["ts"][0]) / TSPERS
    N = np.array([(d >= x - 1e-9).sum() for x in deltas], float)
    return fitexp(deltas, N, T * 2)

def fitexp(deltas, N, exposures):
    m = N >= 30
    if m.sum() < 2:
        return {"A": np.nan, "kappa": np.nan, "deltas": deltas.tolist(), "N": N.tolist()}
    b1, b0 = np.polyfit(deltas[m], np.log(N[m] / exposures), 1, w=np.sqrt(N[m]))
    return {"A": float(np.exp(b0)), "kappa": float(-b1),
            "deltas": deltas.tolist(), "N": N.tolist()}

def sigmaticks(bbo, steps=1):
    g = np.arange(bbo["ts"][0], bbo["ts"][-1], steps * TSPERS)
    m = bbo["mid"][np.searchsorted(bbo["ts"], g, "right") - 1]
    return float(np.std(np.diff(m)) / np.sqrt(steps))

def glft(q, A, kappa, sigma, gamma):
    c1 = np.log1p(gamma / kappa) / gamma
    c2 = np.sqrt(sigma**2 * gamma / (2 * kappa * A) * (1 + gamma / kappa)**(1 + kappa / gamma))
    return c1 + (2 * q + 1) / 2 * c2, c1 - (2 * q - 1) / 2 * c2

# ---------------------------------------------------------------- calendarios
def scheduncond(bbo, everys=5):
    t = np.arange(bbo["ts"][0] + 60000, bbo["ts"][-1] - 60000, everys * TSPERS)
    return np.concatenate([t, t]), np.concatenate([np.ones_like(t), -np.ones_like(t)])

def schedsignal(root, sym, refrs=5, shifts=0):
    f = f"{root}/symbol={sym}/signals.parquet"
    if not os.path.exists(f):
        return None
    s = pl.read_parquet(f).sort("ts")
    if len(s) == 0:
        return None
    T, S = [], []
    for col, side in (("sigbid", 1), ("sigask", -1)):
        on = s.filter(pl.col(col) & ~pl.col(col).shift(1).fill_null(False))["ts"].to_numpy()
        last = -10**18
        for x in on:
            if x - last >= refrs * TSPERS:
                T.append(x + shifts * TSPERS); S.append(side); last = x
    if len(T) == 0:
        return None
    return np.array(T, np.int64), np.array(S, np.int64)

def schedplacebo(sg, seed=1):
    t, sd = sg
    rng = np.random.default_rng(seed)
    h = t // 3600000
    out = np.array([rng.integers(hh * 3600000, (hh + 1) * 3600000) for hh in h], np.int64)
    return out, sd.copy()

# ---------------------------------------------------------------- main
def runarm(ev, bbo, t, sd, off, ttls, latms, kpow, tickbps):
    o = np.argsort(t, kind="stable")
    t, sd, off = t[o], sd[o], off[o]
    pq, fts, kind, q0, st = simdeeporders(ev["ts"], ev["typ"], ev["side"], ev["px"], ev["sz"],
                                          t, sd, off, ttls * TSPERS, latms, kpow)
    mk = markouts(sd, pq, fts, kind, bbo, tickbps)
    nf = len(mk["t"])
    norders = int((st != 4).sum())
    r = {
        "norders": norders,
        "nfills": nf,
        "fillrate": float(nf / max(1, norders)),
        "fracthrough": float((mk["kind"] == 1).mean()) if nf else float(np.nan)
    }
    for h in HZ:
        r[f"mo{h}"] = float(np.nanmean(mk[f"mo{h}"])) if nf else float(np.nan)
        r[f"mo{h}ticks"] = float(np.nanmean(mk[f"mo{h}ticks"])) if nf else float(np.nan)
    e5 = mk["mo5"] - FEEBPS if nf else np.array([])
    r["edge5net"], r["cilo"], r["cihi"] = bootci(e5, mk["t"]) if nf else (float(np.nan), float(np.nan), float(np.nan))
    return r, float(nf / max(1e-9, norders * ttls))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="parquet")
    ap.add_argument("--ticks", default='{"ATOMUSDT":0.001,"USUSDT":0.00001,"XLMUSDT":0.00001,"TRUMPUSDT":0.001,"ADAUSDT":0.0001}')
    ap.add_argument("--gamma", type=float, default=0.05)
    ap.add_argument("--splithour", type=int, default=21)
    a = ap.parse_args()
    ticks = json.loads(a.ticks)
    runid = hashlib.sha1(open(__file__, "rb").read()).hexdigest()[:8]
    print(f"[*] Iniciando Pasada 3 (runid={runid}) con split hour={a.splithour}...")
    rows, calib = [], {}

    for sym, tick in ticks.items():
        print(f"\n[=== Procesando {sym} ===]")
        bbo, ev = load(a.root, sym, tick)
        tickbps = float(tick / (np.nanmean(bbo["mid"]) * tick) * 1e4)
        hs = 0.5 * (bbo["ask"] - bbo["bid"])
        c = calibratetrades(ev, bbo)
        c["sigma"] = sigmaticks(bbo)
        c["tickbps"] = tickbps

        arms = {"U": scheduncond(bbo)}
        sg = schedsignal(a.root, sym)
        if sg is not None and len(sg[0]) > 0:
            arms["C"] = sg
            arms["P"] = schedplacebo(sg)
            sg_shift = schedsignal(a.root, sym, shifts=30)
            if sg_shift is not None and len(sg_shift[0]) > 0:
                arms["Cshift30"] = sg_shift

        lameff = {}
        for arm, (t, sd) in arms.items():
            for split in ("train", "test"):
                hr = (t // 3600000) % 24
                m = (hr < a.splithour) if split == "train" else (hr >= a.splithour)
                tt, ss = t[m], sd[m]
                if len(tt) < 10:
                    continue
                for offname in ("0", "1", "2", "3", "glft"):
                    if offname == "glft":
                        if not np.isfinite(c.get("kappa", np.nan)) or c.get("kappa", 0) <= 0:
                            continue
                        db, da = glft(0, c["A"], c["kappa"], c["sigma"], a.gamma)
                        h0 = hs[np.searchsorted(bbo["ts"], tt, "right") - 1]
                        off = np.clip(np.ceil(np.where(ss == 1, db, da) - h0), 0, 5).astype(np.int64)
                    else:
                        off = np.full(len(tt), int(offname), np.int64)
                    for qn, kp in QMODELS.items():
                        for ttl in (1, 2, 5, 10):
                            for lat in (0, 100):
                                r, lam = runarm(ev, bbo, tt, ss, off, ttl, lat, kp, tickbps)
                                if arm == "U" and split == "train" and qn == "pesimista" \
                                        and lat == 100 and ttl == 5 and offname != "glft":
                                    lameff[int(offname)] = (r["nfills"], r["norders"] * ttl)
                                row_data = {
                                    "run": runid, "sym": sym, "arm": arm, "split": split,
                                    "off": offname, "q": qn, "ttl": ttl, "lat": lat,
                                }
                                row_data.update(r)
                                rows.append(row_data)

        if lameff:
            ks = np.array(sorted(lameff.keys()))
            N = np.array([lameff[k][0] for k in ks], float)
            expo = np.array([lameff[k][1] for k in ks], float)
            m = N >= 30
            if m.sum() >= 2:
                d = ks + np.nanmean(hs)
                b1, b0 = np.polyfit(d[m], np.log(N[m] / expo[m]), 1, w=np.sqrt(N[m]))
                c["Aeff"], c["kappaeff"] = float(np.exp(b0)), float(-b1)
        calib[sym] = c
        print(sym, json.dumps({k: v for k, v in c.items() if not isinstance(v, list)}))

    df = pl.DataFrame(rows)
    csv_file = f"pass3_{runid}.csv"
    df.write_csv(csv_file)
    calib_file = f"calib_{runid}.json"
    json.dump(calib, open(calib_file, "w"), indent=1)
    print(f"\n[+] Resultados guardados en {csv_file} y {calib_file}")

    go = df.filter(
        (pl.col("split") == "test") &
        (pl.col("q") == "pesimista") &
        (pl.col("lat") == 100) &
        (pl.col("nfills") >= 200) &
        (pl.col("cilo") > 0)
    )
    print("\n================================================================================")
    print("CELDAS QUE PASAN EL GATE INSTITUCIONAL (test OOS, pesimista, 100ms, >=200 fills, IC95 > 0):")
    if go.height:
        print(go.select(["sym", "arm", "off", "ttl", "nfills", "fillrate", "fracthrough", "mo1s", "mo5s", "edge5net", "cilo", "cihi"]).sort("edge5net", descending=True))
    else:
        print("NINGUNA -> NO-GO ROTUNDO EN ESTA CONFIGURACION (HFT Maker Sub-Segundo)")
    print("================================================================================")

if __name__ == "__main__":
    main()
