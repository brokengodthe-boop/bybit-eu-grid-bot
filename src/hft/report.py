"""
report.py - Fricción real por par y por hora a partir del Ledger de Antigravity.

Uso:
    python report.py ledgerdump.parquet --tp 150 --sl 100 --out reports/
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PCTS = [0.50, 0.90, 0.99]
MINSAMPLES = 30  # por debajo de esto, un p99 no significa nada

# 1) Adaptador: ÚNICO punto acoplado a tu LedgerEntry. Ajusta los nombres.

def entries_to_df(entries) -> pd.DataFrame:
    """
    Esquema asumido por entrada (un fill agresivo):
      ts:int, symbol:str, side:'buy'|'sell', leg:'entry'|'exit',
      positionid:str, qty:Decimal, vwap:Decimal, mid:Decimal,
      fee:Decimal
    """
    rows = []
    for e in entries:
        notional = e.qty * e.vwap
        rows.append({
            "ts": pd.to_datetime(e.ts, unit="ms", utc=True),
            "symbol": e.symbol,
            "side": e.side,
            "leg": e.leg,
            "positionid": e.positionid,
            "qty": e.qty,
            "vwap": e.vwap,
            "mid": e.mid,
            "fee": e.fee,
            "notional": notional,
        })
    return pd.DataFrame(rows)

def load(path: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df

# 2) Métricas por pierna. Decimal para el cálculo, float solo para estadística.

def bps(num: Decimal, den: Decimal) -> float:
    return float((Decimal(num) / Decimal(den)) * Decimal(10000))

def add_leg_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    sign = np.where(df["side"] == "buy", 1, -1)
    df["slipbps"] = [
        s * bps(Decimal(str(v)) - Decimal(str(m)), Decimal(str(m)))
        for s, v, m in zip(sign, df["vwap"], df["mid"])
    ]
    df["feebps"] = [
        bps(Decimal(str(f)), Decimal(str(n)))
        for f, n in zip(df["fee"], df["notional"])
    ]
    df["totalbps"] = df["slipbps"] + df["feebps"]
    df["hourutc"] = df["ts"].dt.hour
    return df

# 3) Round-trips: emparejar entry+exit por positionid

def build_roundtrips(legs: pd.DataFrame) -> pd.DataFrame:
    g = legs.groupby(["positionid", "leg"]).agg(
        symbol=("symbol", "first"),
        ts=("ts", "min"),
        totalbps=("totalbps", "sum"),   # suma si una pierna tiene varios fills
        notional=("notional", "sum"),
    ).reset_index()
    wide = g.pivot(index="positionid", columns="leg")
    closed = wide.dropna(subset=[("totalbps", "entry"), ("totalbps", "exit")])
    rt = pd.DataFrame({
        "symbol": closed[("symbol", "entry")],
        "tsentry": closed[("ts", "entry")],
        "rtbps": closed[("totalbps", "entry")] + closed[("totalbps", "exit")],
    })
    rt["hourutc"] = pd.to_datetime(rt["tsentry"], utc=True).dt.hour
    return rt.reset_index()

# 4) Tablas de percentiles

def pct_table(df: pd.DataFrame, value: str, by: list[str]) -> pd.DataFrame:
    q = df.groupby(by)[value].quantile(PCTS).unstack()
    q.columns = [f"p{int(p*100)}" for p in PCTS]
    q["n"] = df.groupby(by)[value].size()
    q["mean"] = df.groupby(by)[value].mean()
    q.loc[q["n"] < MINSAMPLES, ["p99"]] = np.nan  # no inventar colas
    return q.round(2)

def verdict(rtbysymbol: pd.DataFrame, tp: float, sl: float) -> pd.DataFrame:
    v = rtbysymbol.copy()
    for p in ["p50", "p90", "p99"]:
        v[f"nettp_{p}"] = tp - v[p]                    # bps que te quedan al acertar
        v[f"winratemin_{p}"] = (sl + v[p]) / (tp + sl) # p de equilibrio
    v["friccion%_deltp_p90"] = 100 * v["p90"] / tp
    v["veredicto"] = np.select(
        [v["p99"] >= tp * 0.5, v["p90"] >= tp * 0.25],
        ["MUERTO: la cola se come el TP", "FRAGIL: depende del win rate"],
        default="VIABLE en friccion",
    )
    return v.round(3)

# 5) Visualización

def plot_all(legs, rt, tp, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    data = rt if len(rt) else legs
    col = "rtbps" if len(rt) else "totalbps"

    # Histograma por par con líneas de percentil y el TP
    for sym, d in data.groupby("symbol"):
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.hist(d[col], bins=60, alpha=0.75)
        for p, ls in zip(PCTS, [":", "--", "-"]):
            ax.axvline(d[col].quantile(p), ls=ls, color="k", label=f"p{int(p*100)}")
        ax.axvline(tp, color="red", lw=2, label=f"TP {tp:.0f} bps")
        ax.set_title(f"{sym} - distribucion de {col}")
        ax.set_xlabel("bps")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / f"hist_{sym}.png", dpi=120)
        plt.close(fig)

    # Heatmap p90 hora × par: dónde/cuándo NO operar
    hm = data.groupby(["symbol", "hourutc"])[col].quantile(0.90).unstack()
    fig, ax = plt.subplots(figsize=(12, 0.6 * len(hm) + 2))
    im = ax.imshow(hm.values, aspect="auto", cmap="RdYlGn_r")
    ax.set_yticks(range(len(hm)))
    ax.set_yticklabels(hm.index)
    ax.set_xticks(range(hm.shape[1]))
    ax.set_xticklabels(hm.columns)
    ax.set_xlabel("hora UTC")
    ax.set_title(f"p90 {col} por hora y par")
    fig.colorbar(im, ax=ax, label="bps")
    fig.tight_layout()
    fig.savefig(out / "heatmap_p90.png", dpi=120)
    plt.close(fig)

    # Descomposición: ¿el problema es fee o slippage?
    comp = legs.groupby("symbol")[["feebps", "slipbps"]].median()
    ax = comp.plot.bar(stacked=True, figsize=(8, 4), title="Mediana por pierna: fee vs slippage")
    ax.set_ylabel("bps")
    plt.tight_layout()
    plt.savefig(out / "fee_vs_slip.png", dpi=120)
    plt.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tp", type=float, default=150.0)
    ap.add_argument("--sl", type=float, default=100.0)
    ap.add_argument("--out", default="reports")
    a = ap.parse_args()
    out = Path(a.out)

    legs = add_leg_metrics(load(a.path))
    rt = build_roundtrips(legs)

    pd.set_option("display.width", 200)
    print("\n=== Friccion por PIERNA, por par ===")
    print(pct_table(legs, "totalbps", ["symbol"]))
    print("\n=== Friccion por PIERNA, por par y hora UTC ===")
    print(pct_table(legs, "totalbps", ["symbol", "hourutc"]))

    if len(rt):
        rtsym = pct_table(rt, "rtbps", ["symbol"])
        print("\n=== ROUND-TRIP por par ===")
        print(rtsym)
        print("\n=== VEREDICTO ===")
        print(verdict(rtsym, a.tp, a.sl).T)
        if out.exists():
            rt.to_parquet(out / "roundtrips.parquet")
    else:
        print("\n[!] Sin posiciones cerradas: estimacion conservadora RT ≈ 2 × pierna")
        est = pct_table(legs, "totalbps", ["symbol"])[["p50", "p90", "p99", "n"]]
        est[["p50", "p90", "p99"]] *= 2
        print(verdict(est.assign(mean=np.nan), a.tp, a.sl).T)

    plot_all(legs, rt, a.tp, out)
    print(f"\nGraficos en {out.resolve()}")

if __name__ == "__main__":
    main()
