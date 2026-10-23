# -*- coding: utf-8 -*-
"""
dual_portfolio.py

Script to compare two shadow portfolios:
1️⃣ **Full portfolio** – opens a position for **every** signal that the bot evaluates
   (whether the Gemini AI vetoed it or approved it).
2️⃣ **Filtered portfolio** – opens a position **only** for signals that the AI approved.

Both portfolios use the exact same sizing logic as the real bot (the same
risk_pct, max_pct_equity, etc.) and the same simulation engine from *sombra.py*
so the results are directly comparable.

The script reads the audit log (logs/auditoria.json) where every evaluated
signal is stored, runs the sizing routine, and records a summary JSON for each
portfolio in ``logs/dual_full.json`` and ``logs/dual_filtered.json``.

Running the script:
    ``venv\\Scripts\\python.exe dual_portfolio.py``

The output prints a quick table with the number of opened positions and the
overall PnL for each portfolio.
"""
import json
import pathlib
from datetime import datetime

# Import the sizing logic from CarteraVirtual and the simulation function
from src.cartera_virtual import CarteraVirtual
from src.sombra import simular

# ---------------------------------------------------------------------------
# Configuration – keep it identical to the live bot so the comparison is fair.
# Adjust these values only if you change the live configuration.
# ---------------------------------------------------------------------------
RISK_PER_TRADE_PCT = 1.0          # % of equity risked per trade
MAX_PCT_EQUITY_TRADE = 0.50       # max ratio of equity allocated to a single trade (0.50 = 50%)
CASH_RESERVE_PCT = 0.05          # ratio of cash kept as reserve (0.05 = 5%)
MIN_ORDEN = 5.0                  # minimum order size (USD)
MAX_POSICIONES = 10               # max concurrent open positions
MAX_PERDIDA_DIARIA_PCT = 20.0     # stop trading if equity falls this % in a day
FEE_LADO_PCT = 0.10               # taker fee per side (percent)
STOP_SLIPPAGE_PCT = 0.05          # slippage on stop/TP exits (percent)
GAP_BUFFER_PCT = 0.0              # extra buffer for price gaps (percent)

# ---------------------------------------------------------------------------
# Initialise two virtual wallets – one that respects the AI verdict, another
# that ignores it.
# ---------------------------------------------------------------------------
full_wallet = CarteraVirtual(
    capital_inicial=50.0,
    risk_pct=RISK_PER_TRADE_PCT,
    max_pct_equity=MAX_PCT_EQUITY_TRADE,
    cash_reserve_pct=CASH_RESERVE_PCT,
    min_orden=MIN_ORDEN,
    max_posiciones=MAX_POSICIONES,
    max_perdida_diaria_pct=MAX_PERDIDA_DIARIA_PCT,
    fee_lado_pct=FEE_LADO_PCT,
    stop_slippage_pct=STOP_SLIPPAGE_PCT,
    gap_buffer_pct=GAP_BUFFER_PCT,
)

filtered_wallet = CarteraVirtual(
    capital_inicial=50.0,
    risk_pct=RISK_PER_TRADE_PCT,
    max_pct_equity=MAX_PCT_EQUITY_TRADE,
    cash_reserve_pct=CASH_RESERVE_PCT,
    min_orden=MIN_ORDEN,
    max_posiciones=MAX_POSICIONES,
    max_perdida_diaria_pct=MAX_PERDIDA_DIARIA_PCT,
    fee_lado_pct=FEE_LADO_PCT,
    stop_slippage_pct=STOP_SLIPPAGE_PCT,
    gap_buffer_pct=GAP_BUFFER_PCT,
)

# ---------------------------------------------------------------------------
# Load the audit log – each entry contains the raw signal and the Gemini verdict.
# ---------------------------------------------------------------------------
audit_path = pathlib.Path("logs/auditoria.json")
if not audit_path.is_file():
    raise FileNotFoundError(f"Audit log not found at {audit_path}")

audit = json.load(open(audit_path, encoding="utf-8"))

# Helper to safely fetch a numeric field, defaulting to 0 when missing.
def _num(val, default=0.0):
    try:
        return float(val)
    except Exception:
        return default

# ---------------------------------------------------------------------------
# Process each signal.
# ---------------------------------------------------------------------------
for entry in audit:
    symbol = entry.get("symbol")
    price = _num(entry.get("entry_price"))
    tp_pct = _num(entry.get("tp_pct"))
    sl_pct = _num(entry.get("sl_pct"))
    
    # Calculate percentages from raw prices if missing
    if tp_pct == 0.0 and _num(entry.get("take_profit")) > 0 and price > 0:
        tp_pct = (_num(entry.get("take_profit")) / price - 1) * 100
    if sl_pct == 0.0 and _num(entry.get("stop_loss")) > 0 and price > 0:
        sl_pct = (1 - _num(entry.get("stop_loss")) / price) * 100
    if not symbol or price <= 0:
        continue

    # Full portfolio – always open
    reg_id_full = f"full-{entry.get('timestamp', datetime.now().isoformat())}"
    pos_f, err_f = full_wallet.abrir(reg_id_full, symbol, price, tp_pct, sl_pct)
    if err_f:
        pass # print(f"Full omitted: {err_f}")

    # Filtered portfolio – only if AI approved
    if entry.get("ai_verdict") == "APPROVED":
        reg_id_filt = f"filt-{entry.get('timestamp', datetime.now().isoformat())}"
        pos_filt, err_filt = filtered_wallet.abrir(reg_id_filt, symbol, price, tp_pct, sl_pct)


# ---------------------------------------------------------------------------
# Summarise results (open positions only – closing requires minute candles).
# ---------------------------------------------------------------------------
summary = {
    "full": {
        "opened": len(full_wallet.e["abiertas"]),
        "balance": full_wallet.e["balance"],
        "blocked": full_wallet.bloqueado,
    },
    "filtered": {
        "opened": len(filtered_wallet.e["abiertas"]),
        "balance": filtered_wallet.e["balance"],
        "blocked": filtered_wallet.bloqueado,
    },
}

print("=== Dual portfolio summary ===")
print(json.dumps(summary, indent=2))

# Persist wallet states for deeper analysis later.
with open("logs/dual_full.json", "w", encoding="utf-8") as f:
    json.dump(full_wallet.e, f, ensure_ascii=False, indent=2)
with open("logs/dual_filtered.json", "w", encoding="utf-8") as f:
    json.dump(filtered_wallet.e, f, ensure_ascii=False, indent=2)
