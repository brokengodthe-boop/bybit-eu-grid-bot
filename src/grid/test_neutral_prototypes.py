#!/usr/bin/env python3
"""
src/grid/test_neutral_prototypes.py — Evaluación y Auditoría Adversarial de Pionex Neutral Grid.
Prueba sobre los 4 activos empíricos de Bybit (TRUMPUSDT, ADAUSDT, ATOMUSDT, XLMUSDT)
en las sesiones completas de 20261011 y 20261012.
"""
import glob, os, sys
import polars as pl
from pionex_neutral_grid import NeutralGridConfig, PionexNeutralGridBot

TICKS = {
    "TRUMPUSDT": (0.001, 30.0),
    "ADAUSDT":   (0.0001, 25.0),
    "ATOMUSDT":  (0.001, 35.0),
    "XLMUSDT":   (0.00001, 25.0),
}

def load_all_data(sym: str):
    b_files = sorted(glob.glob(f"parquet/symbol={sym}/*/bbo_*.parquet"))
    t_files = sorted(glob.glob(f"parquet/symbol={sym}/*/trades_*.parquet"))
    if not b_files or not t_files:
        return None, None
    b_df = pl.read_parquet(b_files).sort("ts_ex")
    t_df = pl.read_parquet(t_files).sort("ts_ex")
    return b_df, t_df

def run_portfolio(trailing_center: bool, auto_compound: bool, leverage: float = 2.0):
    init_per_pair = 13.5625 # 50€ / 4 = 12.50€ (~13.56 USDT)
    results = []

    for sym, (tick, step_bps) in TICKS.items():
        b_df, t_df = load_all_data(sym)
        if b_df is None or len(b_df) == 0:
            continue

        cfg = NeutralGridConfig(
            symbol=sym,
            tick_size=tick,
            initial_capital_usd=init_per_pair,
            leverage=leverage,
            step_bps=step_bps,
            num_levels=8,
            maker_fee_bps=2.0,
            trailing_center=trailing_center,
            min_order_usd=0.50,
            auto_compound=auto_compound
        )

        bot = PionexNeutralGridBot(cfg)
        res = bot.run_simulation(b_df, t_df)
        results.append(res)

    tot_init = sum(r["capital_init"] for r in results)
    tot_final = sum(r["final_equity"] for r in results)
    tot_profit = sum(r["net_profit"] for r in results)
    tot_fees = sum(r["fees_paid"] for r in results)
    tot_gross = sum(r["gross_profit"] for r in results)
    tot_cycles = sum(r["cycles"] for r in results)
    tot_unrl = sum(r["unrealized_pnl"] for r in results)
    max_dd = max(r["max_drawdown_pct"] for r in results)
    portfolio_roi = (tot_final - tot_init) / tot_init * 100.0

    return {
        "trailing": trailing_center,
        "compound": auto_compound,
        "leverage": leverage,
        "tot_init": round(tot_init, 2),
        "tot_final": round(tot_final, 2),
        "tot_profit": round(tot_profit, 2),
        "tot_fees": round(tot_fees, 2),
        "tot_gross": round(tot_gross, 2),
        "tot_cycles": tot_cycles,
        "tot_unrl": round(tot_unrl, 2),
        "portfolio_roi": round(portfolio_roi, 2),
        "max_dd": round(max_dd, 2),
        "edge_fee_ratio": round(tot_gross / max(tot_fees, 1e-9), 1),
        "pairs": results
    }

def main():
    print("=" * 110)
    print("      AUDITORIA ADVERSARIAL: PROTOTIPOS PIONEX FUTURES NEUTRAL GRID (50€ -> 100€)")
    print("=" * 110)

    # 1. Neutral Estático (sin trailing, 2x)
    p_static = run_portfolio(trailing_center=False, auto_compound=True, leverage=2.0)

    # 2. Neutral Trailing (con trailing center dinámico, 2x)
    p_trailing = run_portfolio(trailing_center=True, auto_compound=True, leverage=2.0)

    # 3. Neutral Trailing Optimizado (2.5x apalancamiento)
    p_trailing_25 = run_portfolio(trailing_center=True, auto_compound=True, leverage=2.5)

    protos = [
        (p_static, "Prototipo 4A: Pionex Neutral Grid Estático (2.0x, Sin Trailing)"),
        (p_trailing, "Prototipo 4B: Pionex Neutral Trailing Grid (2.0x, Trailing Center Dinámico)"),
        (p_trailing_25, "Prototipo 4C: Pionex Neutral Trailing Grid (2.5x, Compounding + Trailing)"),
    ]

    for p, title in protos:
        print(f"\n>>> {title} <<<")
        print(f"Capital Inicial: {p['tot_init']}$ (~50.00€) | Capital Final: {p['tot_final']}$ | Beneficio Neto Total: {p['tot_profit']:>+6.2f}$")
        print(f"Retorno ROI: {p['portfolio_roi']:>+6.2f}% | Ciclos Completos: {p['tot_cycles']:>4} | Flotante: {p['tot_unrl']:>+5.2f}$ | Comisiones: -{p['tot_fees']}$ | Max DD: {p['max_dd']:.2f}% | Edge/Fee: {p['edge_fee_ratio']}x")
        print("-" * 110)
        print(f"{'Symbol':<10} | {'Init ($)':<8} | {'Final ($)':<9} | {'Net PnL ($)':<11} | {'ROI (%)':<8} | {'Cycles':<6} | {'Unrealized':<10} | {'MaxDD (%)':<9}")
        print("-" * 110)
        for r in p["pairs"]:
            print(f"{r['symbol']:<10} | {r['capital_init']:>8.2f}$ | {r['final_equity']:>9.2f}$ | {r['net_profit']:>10.2f}$ | {r['roi_pct']:>7.2f}% | {r['cycles']:>6} | {r['unrealized_pnl']:>9.2f}$ | {r['max_drawdown_pct']:>8.2f}%")
        print("-" * 110)

if __name__ == "__main__":
    main()
