import sys
import math
from collections import deque
from decimal import Decimal as D
from itertools import count

from engine import Engine
from replay import iter_events
from exec_model import ExecModel
from ledger import Ledger, FeeSchedule
from report import entries_to_df

# =============================================================================
# ROLLING Z (ESTRATEGIA CUANTITATIVA)
# =============================================================================
class RollingZ:
    def __init__(self, lookback: int, window: int):
        self.mids: deque[float] = deque(maxlen=lookback + 1)
        self.rets: deque[float] = deque(maxlen=window)
        self.s = 0.0
        self.s2 = 0.0
        self.window = window

    def update(self, mid: float) -> float | None:
        self.mids.append(mid)
        if len(self.mids) <= self.mids.maxlen - 1:
            return None
        r = math.log(mid / self.mids[0])
        if len(self.rets) == self.window:
            old = self.rets[0]
            self.s -= old
            self.s2 -= old * old
        self.rets.append(r)
        self.s += r
        self.s2 += r * r
        n = len(self.rets)
        if n < self.window:
            return None                               # warm-up
        mu = self.s / n
        var = max(self.s2 / n - mu * mu, 0.0)
        sd = math.sqrt(var)
        return None if sd < 1e-12 else (r - mu) / sd

# =============================================================================
# ADAPTADORES
# =============================================================================
class BookView:
    def __init__(self, engine: Engine, symbol: str):
        self.engine = engine
        self.symbol = symbol

    @property
    def book(self):
        return self.engine.books.get(self.symbol)

    def ready(self) -> bool:
        b = self.book
        return b is not None and b.mid() is not None

    def mid(self) -> float | None:
        b = self.book
        return b.mid() if b is not None else None

    def spread_bps(self) -> float | None:
        b = self.book
        if not b: return None
        bb = b.best_bid()
        ba = b.best_ask()
        if bb and ba:
            return (ba[0] - bb[0]) / ((ba[0] + bb[0]) / 2) * 10000
        return None

def ev_ts(ev) -> int:
    return getattr(ev, "ts", getattr(ev, "recv_ns", 0)) // 1000000 # Convert to ms

def ev_kind(ev) -> str:
    return type(ev).__name__

# =============================================================================
# BUCLE PRINCIPAL
# =============================================================================
def run(pattern: str, symbol: str):
    fees = FeeSchedule(maker=D("0.001"), taker=D("0.001"), fee_on_buy_in_base=True)
    miledger = Ledger(fees=fees, quote_free=D("1000"))
    
    engine = Engine([symbol], request_snapshot=lambda s: None)
    view = BookView(engine, symbol)
    oidseq = count(1)

    execmodel = None
    stats = {"events": 0, "orders": 0, "fills": 0, "vetoes": 0, "skipped_unsynced": 0}
    
    # Parámetros de estrategia
    BAR_MS = 1000
    LOOKBACK_BARS = 30
    Z_WINDOW = 600
    Z_ENTRY = -2.5
    MAX_SPREAD_BPS = 5.0
    QUOTE_PER_TRADE = D("50")
    LATENCY_MS = 50

    rz = RollingZ(LOOKBACK_BARS, Z_WINDOW)
    next_bar = 0
    pending_signal = None  
    pos_entry = None 
    
    for ev in iter_events(pattern):
        engine.on_event(ev)
        stats["events"] += 1

        if not hasattr(ev, "symbol") or ev.symbol != symbol:
            continue

        kind = ev_kind(ev)
        if kind == "Disconnect":
            continue

        if not view.ready():
            stats["skipped_unsynced"] += 1
            continue
            
        if execmodel is None or execmodel.book is not view.book:
            execmodel = ExecModel(miledger, view.book)

        ts = ev_ts(ev)
        mid = view.mid()
        spread = view.spread_bps()
        if mid is None or spread is None:
            continue

        # 2) Ejecutar señal pendiente tras latencia
        if pending_signal and ts >= pending_signal[0] and not pos_entry:
            _, z = pending_signal
            pending_signal = None
            if spread <= MAX_SPREAD_BPS:
                oid = f"{symbol}-{ts}-{next(oidseq)}"
                stats["orders"] += 1
                qty_base = QUOTE_PER_TRADE / D(str(mid))
                res = execmodel.market_buy(ts, oid, qty_base)
                if res is True:
                    stats["fills"] += 1
                    pos_entry = (ts, z, oid)
                else:
                    stats["vetoes"] += 1

        # 3) Evaluar Rolling Z cada BAR_MS
        if next_bar == 0:
            next_bar = ts - (ts % BAR_MS) + BAR_MS
        elif ts >= next_bar:
            next_bar = ts - (ts % BAR_MS) + BAR_MS
            z = rz.update(mid)
            if z is not None and z <= Z_ENTRY and spread <= MAX_SPREAD_BPS and not pos_entry and not pending_signal:
                pending_signal = (ts + LATENCY_MS, z)

    print(f"Stats: {stats}")
    print(f"Quote Free: {miledger.quote_free}")
    print(f"Base Free: {miledger.base_free}")
    df = entries_to_df(miledger.entries)
    if not df.empty:
        df.to_parquet("ledgerdump.parquet")
        print("Guardado ledgerdump.parquet")
    else:
        print("No se guardó el parquet porque no hubo fills.")

if __name__ == "__main__":
    pattern = sys.argv[1] if len(sys.argv) > 1 else "data/md_*.jsonl.gz"
    run(pattern, "BTCUSDT")
