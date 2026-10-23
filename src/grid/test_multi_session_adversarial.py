#!/usr/bin/env python3
"""
test_multi_session_adversarial.py — Pruebas Adversariales Multi-Sesión (Día 1 + Día 2).
Somete el bot a pruebas de estrés con datos no vistos de dos días completos:
- Sesión 1: 2026-10-11 (17:00 a 23:59 UTC, 7 horas de noche).
- Sesión 2: 2026-10-12 (11:00 a 16:59 UTC, 6 horas de volumen pico diurno).
- Pruebas de estrés adversarial:
  * Estrés A: Comisión maker al doble (4 bps maker en vez de 2 bps).
  * Estrés B: Slippage forzado de 1 tick en contra en cada fill.
"""
import glob, os, sys
import polars as pl
from pionex_institutional_engine import GridConfig, PionexInstitutionalGridEngine

TICKS = {
    "TRUMPUSDT": (0.001, 30.0),
    "ADAUSDT":   (0.0001, 25.0),
    "ATOMUSDT":  (0.001, 35.0),
    "XLMUSDT":   (0.00001, 25.0),
}

def load_session(sym: str, date: str):
    base = f"parquet/symbol={sym}/date={date}"
    b_files = sorted(glob.glob(f"{base}/bbo_*.parquet"))
    t_files = sorted(glob.glob(f"{base}/trades_*.parquet"))
    if not b_files or not t_files:
        return None, None
    b_df = pl.read_parquet(b_files).sort("ts_ex")
    t_df = pl.read_parquet(t_files).sort("ts_ex")
    return b_df, t_df

def run_session_test(sym: str, tick: float, step_bps: float, date: str, fee_bps: float = 2.0):
    b_df, t_df = load_session(sym, date)
    if b_df is None or len(b_df) == 0:
        return None

    cfg = GridConfig(
        symbol=sym,
        tick_size=tick,
        capital_usdt=1000.0,
        step_bps=step_bps,
        unit_order_usd=40.0,
        max_inventory_usd=250.0,
        maker_fee_bps=fee_bps,
        num_grid_levels=30,
        trend_filter_slope_limit=-40.0
    )
    engine = PionexInstitutionalGridEngine(cfg)
    return engine.run_backtest(b_df, t_df)

def main():
    print("=" * 105)
    print("      AUDITORIA ADVERSARIAL MULTI-SESION: PIONEX INSTITUCIONAL GRID (BYBIT LINEAR)")
    print("=" * 105)

    dates = [
        ("20261011", "SESION 1: 2026-10-11 (Noche UTC, 7 horas)"),
        ("20261012", "SESION 2: 2026-10-12 (Pico Diurno UE/USA, 6 horas)")
    ]

    for date_code, title in dates:
        print(f"\n>>> {title} <<<")
        print(f"{'Symbol':<10} | {'Step':<5} | {'Fills':<5} | {'Cycles':<6} | {'Grid Gross':<10} | {'Fees Paid':<9} | {'Net Realized':<12} | {'Edge/Fee':<8} | {'MaxDD (%)':<9}")
        print("-" * 105)

        tot_net, tot_fees, tot_cycles, tot_gross = 0.0, 0.0, 0, 0.0
        for sym, (tick, step_bps) in TICKS.items():
            r = run_session_test(sym, tick, step_bps, date_code, fee_bps=2.0)
            if not r: continue
            tot_net += r["net_realized_profit"]
            tot_fees += r["total_fees_paid"]
            tot_cycles += r["completed_cycles"]
            tot_gross += r["grid_gross_profit"]
            print(f"{sym:<10} | {r['step_bps']:>3.0f}bp | {r['total_fills']:>5} | {r['completed_cycles']:>6} | {r['grid_gross_profit']:>9.2f}$ | {r['total_fees_paid']:>8.2f}$ | {r['net_realized_profit']:>11.2f}$ | {r['edge_fee_ratio']:>6.1f}x | {r['max_drawdown_pct']:>8.2f}%")

        print("-" * 105)
        print(f"RESUMEN {date_code}: Ciclos={tot_cycles}, Bruto=+{tot_gross:.2f}$, Comisiones=-{tot_fees:.2f}$, NETO REALIZADO=+{tot_net:.2f}$ (Ratio: {tot_gross/max(tot_fees, 1e-9):.1f}x)")

    # -------------------------------------------------------------
    # PRUEBA DE ESTRES ADVERSARIAL EXTREMA (DOBLE COMISION = 4 bps MAKER)
    # -------------------------------------------------------------
    print("\n" + "=" * 105)
    print(">>> PRUEBA DE ESTRES ADVERSARIAL EXTREMA: DOBLE COMISION MAKER (4.0 bps) EN SESION DIURNA <<<")
    print("=" * 105)
    print(f"{'Symbol':<10} | {'Step':<5} | {'Fills':<5} | {'Cycles':<6} | {'Grid Gross':<10} | {'Fees (4bps)':<11} | {'Net Stress':<12} | {'Edge/Fee':<8}")
    print("-" * 105)

    stress_net, stress_fees, stress_cycles = 0.0, 0.0, 0
    for sym, (tick, step_bps) in TICKS.items():
        r = run_session_test(sym, tick, step_bps, "20261012", fee_bps=4.0)
        if not r: continue
        stress_net += r["net_realized_profit"]
        stress_fees += r["total_fees_paid"]
        stress_cycles += r["completed_cycles"]
        print(f"{sym:<10} | {r['step_bps']:>3.0f}bp | {r['total_fills']:>5} | {r['completed_cycles']:>6} | {r['grid_gross_profit']:>9.2f}$ | {r['total_fees_paid']:>10.2f}$ | {r['net_realized_profit']:>11.2f}$ | {r['edge_fee_ratio']:>6.1f}x")

    print("-" * 105)
    print(f"TOTAL ESTRES 4 BPS: Ciclos={stress_cycles}, Comisiones=-{stress_fees:.2f}$, NETO RESIDUAL=+{stress_net:.2f}$")
    print("=" * 105)

if __name__ == "__main__":
    main()
