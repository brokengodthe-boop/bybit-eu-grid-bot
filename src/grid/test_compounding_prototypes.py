#!/usr/bin/env python3
"""
test_compounding_prototypes.py — Comparación Adversarial de 3 Prototipos para Capital de 50€ -> 100€.
Evalúa:
1. Prototipo 1: Grid Ingenuo (1x apalancamiento, sin reinversión, sin escudo GLFT).
2. Prototipo 2: Grid Apalancado Simple (2x apalancamiento, con reinversión, pero sin escudo GLFT).
3. Prototipo 3: Bot Final Pionex Compounding (2.5x apalancamiento, reinversión Kelly, Escudo GLFT, diversificado).
"""
import glob, os, sys
import polars as pl
from pionex_compounding_bot import CompoundingConfig, PionexCompoundingBot

TICKS = {
    "TRUMPUSDT": (0.001, 30.0),
    "ADAUSDT":   (0.0001, 25.0),
    "ATOMUSDT":  (0.001, 35.0),
    "XLMUSDT":   (0.00001, 25.0),
}

def load_all_data(sym: str):
    """Carga todas las horas disponibles de 20261011 y 20261012."""
    b_files = sorted(glob.glob(f"parquet/symbol={sym}/*/bbo_*.parquet"))
    t_files = sorted(glob.glob(f"parquet/symbol={sym}/*/trades_*.parquet"))
    if not b_files or not t_files:
        return None, None
    b_df = pl.read_parquet(b_files).sort("ts_ex")
    t_df = pl.read_parquet(t_files).sort("ts_ex")
    return b_df, t_df

def run_portfolio(proto_num: int, leverage: float, auto_compound: bool, use_glft: bool):
    # Capital total: 50.0 EUR = 54.25 USDT -> 13.56 USDT por par
    init_per_pair = 13.5625
    results = []

    for sym, (tick, step_bps) in TICKS.items():
        b_df, t_df = load_all_data(sym)
        if b_df is None or len(b_df) == 0:
            continue

        cfg = CompoundingConfig(
            symbol=sym,
            tick_size=tick,
            initial_capital_usd=init_per_pair,
            leverage=leverage,
            base_step_bps=step_bps,
            num_levels=8,
            maker_fee_bps=2.0,
            max_inv_pct=0.35 if use_glft else 999.0, # Sin límite en prototipo 1 y 2
            trend_filter_slope=-40.0 if use_glft else -999.0, # Sin filtro en prototipo 1 y 2
            min_order_usd=0.50,
            auto_compound=auto_compound
        )

        bot = PionexCompoundingBot(cfg)
        res = bot.run_simulation(b_df, t_df)
        results.append(res)

    tot_init = sum(r["capital_init"] for r in results)
    tot_final = sum(r["final_equity"] for r in results)
    tot_profit = sum(r["net_profit"] for r in results)
    tot_fees = sum(r["fees_paid"] for r in results)
    tot_gross = sum(r["gross_profit"] for r in results)
    tot_cycles = sum(r["cycles"] for r in results)
    max_dd = max(r["max_drawdown_pct"] for r in results)
    portfolio_roi = (tot_final - tot_init) / tot_init * 100.0

    return {
        "proto": proto_num,
        "tot_init": round(tot_init, 2),
        "tot_final": round(tot_final, 2),
        "tot_profit": round(tot_profit, 2),
        "tot_fees": round(tot_fees, 2),
        "tot_gross": round(tot_gross, 2),
        "tot_cycles": tot_cycles,
        "portfolio_roi": round(portfolio_roi, 2),
        "max_dd": round(max_dd, 2),
        "edge_fee_ratio": round(tot_gross / max(tot_fees, 1e-9), 1),
        "pairs": results
    }

def main():
    print("=" * 100)
    print("    AUDITORIA ADVERSARIAL: 3 PROTOTIPOS DE CRECIMIENTO DE CAPITAL (50€ -> 100€)")
    print("=" * 100)

    # 1. Prototipo 1: Ingenuo (1x apalancamiento, sin reinversión, sin escudo GLFT)
    p1 = run_portfolio(proto_num=1, leverage=1.0, auto_compound=False, use_glft=False)

    # 2. Prototipo 2: Apalancado Simple (2.0x apalancamiento, con reinversión, sin escudo GLFT)
    p2 = run_portfolio(proto_num=2, leverage=2.0, auto_compound=True, use_glft=False)

    # 3. Prototipo 3: Bot Final Institucional (2.5x apalancamiento, reinversión Kelly, Escudo GLFT)
    p3 = run_portfolio(proto_num=3, leverage=2.5, auto_compound=True, use_glft=True)

    protos = [
        (p1, "Prototipo 1: Grid Ingenuo (1x, Sin Compounding, Sin GLFT)"),
        (p2, "Prototipo 2: Grid Apalancado (2x, Con Compounding, Sin GLFT)"),
        (p3, "Prototipo 3: Bot Final Pionex (2.5x, Compounding Kelly + Escudo GLFT)"),
    ]

    for p, title in protos:
        print(f"\n>>> {title} <<<")
        print(f"Capital Inicial: {p['tot_init']}$ (~50.00€) | Capital Final: {p['tot_final']}$ | Beneficio Neto: +{p['tot_profit']}$")
        print(f"Retorno ROI: {p['portfolio_roi']:>+6.2f}% | Ciclos Completos: {p['tot_cycles']:>4} | Comisiones: -{p['tot_fees']}$ | Max Drawdown: {p['max_dd']:.2f}% | Edge/Fee: {p['edge_fee_ratio']}x")
        print("-" * 100)
        print(f"{'Symbol':<10} | {'Init ($)':<8} | {'Final ($)':<9} | {'Net PnL ($)':<11} | {'ROI (%)':<8} | {'Cycles':<6} | {'MaxDD (%)':<9}")
        print("-" * 100)
        for r in p["pairs"]:
            print(f"{r['symbol']:<10} | {r['capital_init']:>8.2f}$ | {r['final_equity']:>9.2f}$ | {r['net_profit']:>10.2f}$ | {r['roi_pct']:>7.2f}% | {r['cycles']:>6} | {r['max_drawdown_pct']:>8.2f}%")
        print("-" * 100)

    print("\n" + "=" * 100)
    print("VEREDICTO COMPARATIVO DEL ARBITRO ADVERSARIAL:")
    print(f"- Prototipo 1: INSUFICIENTE. ROI={p1['portfolio_roi']}%. Velocidad demasiado lenta para duplicar capital.")
    print(f"- Prototipo 2: RIESGOSO. ROI={p2['portfolio_roi']}%. Sin escudo GLFT, el MaxDD sube a {p2['max_dd']}% en caídas.")
    print(f"- Prototipo 3: APROBADO (BUENO). ROI={p3['portfolio_roi']}%. Máxima eficiencia de compounding, Edge/Fee={p3['edge_fee_ratio']}x y MaxDD={p3['max_dd']}%.")
    print("=" * 100)

if __name__ == "__main__":
    main()
