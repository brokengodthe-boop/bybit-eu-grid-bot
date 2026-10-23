#!/usr/bin/env python3
"""
backtest_grid.py — Auditoría Adversarial y Backtest de Pionex Grid vs Naive Grid
sobre los datos reales de Bybit Linear (2026-10-11, 17:00 a 23:59 UTC).
"""
import glob, os, sys
import polars as pl
from pionex_institutional_grid import GridConfig, PionexInstitutionalGridBot

TICKS = {
    "ATOMUSDT": 0.001,
    "ADAUSDT": 0.0001,
    "TRUMPUSDT": 0.001,
    "XLMUSDT": 0.00001,
    "USUSDT": 0.00001,
}

def run_backtest_for_symbol(symbol: str, split: str = "all"):
    tick = TICKS[symbol]
    base = f"parquet/symbol={symbol}/date=20261011"
    b_files = sorted(glob.glob(f"{base}/bbo_*.parquet"))
    t_files = sorted(glob.glob(f"{base}/trades_*.parquet"))

    if not b_files or not t_files:
        print(f"[!] Faltan archivos para {symbol}")
        return None

    b_df = pl.read_parquet(b_files).sort("ts_ex")
    t_df = pl.read_parquet(t_files).sort("ts_ex")

    # Filtro train / test
    if split == "train":
        b_df = b_df.filter(((pl.col("ts_ex") // 3600000) % 24) < 21)
        t_df = t_df.filter(((pl.col("ts_ex") // 3600000) % 24) < 21)
    elif split == "test":
        b_df = b_df.filter(((pl.col("ts_ex") // 3600000) % 24) >= 21)
        t_df = t_df.filter(((pl.col("ts_ex") // 3600000) % 24) >= 21)

    # 1. Configuración Pionex Institucional (GLFT Skew + Dynamic Vol)
    cfg_inst = GridConfig(
        symbol=symbol,
        tick_size=tick,
        capital_usdt=1000.0,
        leverage=2.0,
        base_grid_bps=45.0,
        min_grid_bps=35.0,
        max_grid_bps=90.0,
        num_levels_per_side=5,
        maker_fee_bps=2.0,
        max_inventory_qty=200.0,
        gamma_risk=0.05,
        skew_intensity=1.8,
        trend_filter_lookback_s=300,
    )
    bot_inst = PionexInstitutionalGridBot(cfg_inst)
    res_inst = bot_inst.run_simulation(b_df, t_df)

    # 2. Configuración Naive Grid (Sin GLFT Skew, Sin Filtro de Régimen)
    cfg_naive = GridConfig(
        symbol=symbol,
        tick_size=tick,
        capital_usdt=1000.0,
        leverage=2.0,
        base_grid_bps=45.0,
        min_grid_bps=45.0,
        max_grid_bps=45.0,
        num_levels_per_side=5,
        maker_fee_bps=2.0,
        max_inventory_qty=1000.0,
        gamma_risk=0.0,
        skew_intensity=0.0, # Sin sesgo
        trend_filter_lookback_s=0,
    )
    bot_naive = PionexInstitutionalGridBot(cfg_naive)
    res_naive = bot_naive.run_simulation(b_df, t_df)

    return res_inst, res_naive

def main():
    print("=" * 80)
    print("AUDITORIA ADVERSARIAL: PIONEX INSTITUCIONAL (GLFT) VS NAIVE GRID (BYBIT LINEAR)")
    print("=" * 80)

    for split in ("train", "test", "all"):
        print(f"\n>>> SPLIT: {split.upper()} <<<")
        print(f"{'Symbol':<10} | {'Bot Type':<16} | {'Net PnL ($)':<12} | {'ROI (%)':<9} | {'Fills':<7} | {'Cycles':<7} | {'Fees ($)':<9} | {'MaxDD (%)':<9} | {'Final Inv':<10}")
        print("-" * 105)

        for sym in TICKS.keys():
            res = run_backtest_for_symbol(sym, split)
            if not res: continue
            inst, naive = res
            print(f"{sym:<10} | {'Pionex GLFT':<16} | {inst['net_profit_usdt']:>11.2f}$ | {inst['roi_pct']:>8.2f}% | {inst['total_fills']:>6} | {inst['completed_cycles']:>6} | {inst['total_fees_paid']:>8.2f}$ | {inst['max_drawdown_pct']:>8.2f}% | {inst['final_inventory_qty']:>9.1f}")
            print(f"{sym:<10} | {'Naive Grid':<16} | {naive['net_profit_usdt']:>11.2f}$ | {naive['roi_pct']:>8.2f}% | {naive['total_fills']:>6} | {naive['completed_cycles']:>6} | {naive['total_fees_paid']:>8.2f}$ | {naive['max_drawdown_pct']:>8.2f}% | {naive['final_inventory_qty']:>9.1f}")
            print("-" * 105)

if __name__ == "__main__":
    main()
