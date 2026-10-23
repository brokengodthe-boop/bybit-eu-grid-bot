import numpy as np
import pandas as pd
from itertools import product
import argparse

# --- Carga: un registro por CANDIDATO (con o sin fill) ---
# En un entorno real se llamaría df = pd.read_parquet("data/ledgerdump.parquet")

def run_wfo(parquet_path: str):
    print(f"Leyendo dataset de candidatos: {parquet_path}")
    df = pd.read_parquet(parquet_path)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    H = "mid10s"                      # horizonte de salida
    df["pnl"] = np.where(
        df["filled"],
        df["sign"] * (df[H] - df["fillpx"]) * df["qty"] - df["fee"],
        0.0,
    )
    df["adv10s"] = df["sign"] * (df["mid10s"] - df["midatfill"])  # selección adversa pura
    df["halfspread"] = df["spreadatfill"] / 2

    GRID = {
        "obi":   np.round(np.arange(0.10, 0.61, 0.05), 2),
        "vol":   np.round(np.arange(1.0, 3.01, 0.25), 2),
        "trend": np.round(np.arange(-1.0, 1.01, 0.25), 2),
    }
    KEYS = list(GRID)
    MIN_FILLS = 200
    EMBARGO = pd.Timedelta("45min")

    def mask_for(d, p):
        return (d["obi"].abs() >= p["obi"]) & (d["volratio"] >= p["vol"]) & \
               (d["sign"] * d["trend"] >= p["trend"])

    def blockboot_lcb(hourly, n=500, q=0.10, block=6, rng=np.random.default_rng(0)):
        x = hourly.values
        if len(x) < block * 2:
            return -np.inf
        nb = int(np.ceil(len(x) / block))
        starts = rng.integers(0, len(x) - block + 1, size=(n, nb))
        means = np.array([np.concatenate([x[s:s+block] for s in row])[:len(x)].mean()
                          for row in starts])
        return np.quantile(means, q)

    def fitness(d, p):
        sel = d[mask_for(d, p) & d["filled"]]
        if len(sel) < MIN_FILLS:
            return -np.inf
        if sel["adv10s"].mean() < -sel["halfspread"].mean():
            return -np.inf
        hourly = sel.set_index("ts")["pnl"].resample("1h").sum()
        hourly = hourly.reindex(
            pd.date_range(d["ts"].min().floor("h"), d["ts"].max().ceil("h"),
                          freq="1h", tz="UTC"), fill_value=0.0)
        return blockboot_lcb(hourly)

    def smooth_plateau(scores: np.ndarray) -> np.ndarray:
        """Media de vecinos ±1 en cada eje; -inf contamina = celda inválida."""
        s = np.where(np.isfinite(scores), scores, np.nan)
        out = np.full_like(s, np.nan)
        it = np.ndindex(s.shape)
        for idx in it:
            sl = tuple(slice(max(i-1, 0), i+2) for i in idx)
            w = s[sl]
            if np.isnan(w).mean() <= 0.3:          # tolera algún vecino inválido
                out[idx] = np.nanmean(w)
        return out

    def folds(d, train_days=6, test_days=1):
        days = d["ts"].dt.floor("D").unique()
        for i in range(train_days, len(days) - test_days + 1):
            tr_start, te_start = days[i-train_days], days[i]
            te_end = te_start + pd.Timedelta(days=test_days)
            train = d[(d["ts"] >= tr_start) & (d["ts"] < te_start - EMBARGO)]   # purga
            test  = d[(d["ts"] >= te_start) & (d["ts"] < te_end)]
            yield te_start, train, test

    shape = tuple(len(GRID[k]) for k in KEYS)
    results, all_is_scores = [], []

    print(f"Dimensiones de Grid: {shape} = {np.prod(shape)} combinaciones por fold")
    for te_start, train, test in folds(df):
        print(f"Evaluando Fold Test={te_start}")
        scores = np.full(shape, -np.inf)
        for idx in np.ndindex(shape):
            p = {k: GRID[k][i] for k, i in zip(KEYS, idx)}
            scores[idx] = fitness(train, p)
        all_is_scores.append(scores.ravel())

        sm = smooth_plateau(scores)
        if np.all(np.isnan(sm)):
            results.append({"test_day": te_start, "traded": False})
            continue
        best = np.unravel_index(np.nanargmax(sm), shape)
        p = {k: GRID[k][i] for k, i in zip(KEYS, best)}

        sel = test[mask_for(test, p) & test["filled"]]
        results.append({
            "test_day": te_start, "traded": True, **p,
            "is_fit": scores[best], "oos_pnl": sel["pnl"].sum(),
            "oos_fills": len(sel), "oos_adv10s": sel["adv10s"].mean(),
        })

    wf = pd.DataFrame(results)
    print("\n=== Resultados Walk-Forward ===")
    print(wf.to_string())
    
    if "oos_pnl" in wf.columns:
        print(f"\nEquity Curve Final OOS: {wf['oos_pnl'].cumsum().iloc[-1]:.2f}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, default="data/ledgerdump.parquet")
    args = parser.parse_args()
    try:
        run_wfo(args.data)
    except FileNotFoundError:
        print(f"No se pudo encontrar {args.data}. Genera primero las simulaciones!")
