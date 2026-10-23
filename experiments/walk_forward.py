"""
experiments/walk_forward.py — Experimento walk-forward del grid spot en Bybit EU.

Hipótesis contrastadas (todas con comisiones reales de la cuenta, 25 bps maker):
  H1  Un espaciado mayor (k más alto) mejora el resultado neto pese a hacer menos ciclos.
  H2  El espaciado adaptativo a la volatilidad realizada mejora al espaciado fijo.
  H3  El filtro de tendencia reduce el drawdown sin destruir la rentabilidad.
  H4  La rejilla solo supera a comprar-y-mantener en mercado lateral/bajista.

Método: 120 días de velas 1m. Primera mitad (in-sample) para elegir la configuración, segunda mitad
(out-of-sample) para validarla SIN volver a optimizar. Así se evita el sobreajuste.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import sys
import time
from dataclasses import replace
from decimal import Decimal
from multiprocessing import Pool

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from src.grid_v2.backtest import fetch_klines, run_portfolio  # noqa: E402
from src.grid_v2.config import RiskConfig, StrategyConfig  # noqa: E402

PAIRS = ["BTCEUR", "ETHEUR", "SOLEUR", "XRPEUR", "DOGEEUR", "ADAEUR", "LTCEUR"]
DAYS = 120
CAPITAL = Decimal("50")
_BARS = {}


def _init():
    logging.disable(logging.CRITICAL)
    global _BARS
    _BARS = {p: fetch_klines(p, "1", DAYS) for p in PAIRS}


def _job(args):
    name, params, period, t_start, t_end, inst, fees = args
    strat = StrategyConfig(pairs=PAIRS, initial_capital_eur=CAPITAL, num_levels=params["levels"],
                           k_fee_multiplier=Decimal(str(params["k"])), step_mode=params["mode"],
                           vol_multiplier=Decimal("1.0"), step_max_bps=Decimal("500"))
    risk = RiskConfig(order_size_factor=Decimal("3.34"), trend_filter_enabled=params["trend"],
                      cancel_buys_on_pause=params["trend"])
    t = time.time()
    r = run_portfolio(_BARS, inst, fees, strat, risk, CAPITAL, fill_through_ticks=1, t_start=t_start, t_end=t_end)
    r.update(name=name, period=period, params=params, secs=round(time.time() - t))
    return r


def main():
    from src.grid_v2.config import load_config
    from src.grid_v2.exchange import BybitSpotExchange
    cfg = load_config("config.yaml")
    ex = BybitSpotExchange(cfg.exchange)
    inst = {p: ex.get_instrument_info(p) for p in PAIRS}
    fees = {p: ex.get_fee_rate(p) for p in PAIRS}

    bars = {p: fetch_klines(p, "1", DAYS) for p in PAIRS}
    t_first = max(b[0][0] for b in bars.values()) + 240 * 60_000
    t_last = min(b[-1][0] for b in bars.values())
    t_mid = t_first + (t_last - t_first) // 2

    grid = list(itertools.product([2.5, 3.5, 5.0], [3, 5], ["fixed", "adaptive"], [True, False]))
    jobs = []
    for k, lv, mode, trend in grid:
        params = {"k": k, "levels": lv, "mode": mode, "trend": trend}
        name = f"k={k} niv={lv} {mode[:5]} filtro={'on' if trend else 'off'}"
        jobs.append((name, params, "IS", t_first, t_mid, inst, fees))
        jobs.append((name, params, "OOS", t_mid, t_last + 60_000, inst, fees))

    print(f"{len(jobs)} simulaciones | IS {time.strftime('%Y-%m-%d', time.gmtime(t_first/1000))} -> "
          f"{time.strftime('%Y-%m-%d', time.gmtime(t_mid/1000))} | OOS -> {time.strftime('%Y-%m-%d', time.gmtime(t_last/1000))}",
          flush=True)
    t0 = time.time()
    with Pool(processes=min(15, os.cpu_count() or 4), initializer=_init) as pool:
        results = []
        for r in pool.imap_unordered(_job, jobs):
            results.append(r)
            print(f"[{len(results)}/{len(jobs)}] {r['period']:<3} {r['name']:<38} ret={r['return_pct']:+6.2f}% "
                  f"dd={r['max_drawdown_pct']:5.2f}% ciclos={r['cycles']:4d} ({r['secs']}s)", flush=True)
    os.makedirs("experiments/results", exist_ok=True)
    with open("experiments/results/walk_forward.json", "w") as f:
        json.dump({"t_first": t_first, "t_mid": t_mid, "t_last": t_last, "results": results}, f, indent=1, default=str)
    print(f"Hecho en {time.time()-t0:.0f}s -> experiments/results/walk_forward.json")


if __name__ == "__main__":
    main()
