import asyncio, time, logging
from typing import Callable
from sortedcontainers import SortedDict
from domain import DepthDiff, Trade, Snapshot, Disconnect, Event

log = logging.getLogger("engine")

BID, ASK = 0, 1

class L2Book:
    __slots__ = ("symbol", "bids", "asks", "changes", "watched", "last_id", "synced", "buffer", "_best_bid", "_best_ask")

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.reset()

    def reset(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.changes: list = []          # se reutiliza en cada delta
        self.watched: set[float] | None = None  # precios con órdenes vivas; None = emitir todo
        self.last_id, self.synced, self.buffer = 0, False, []
        self._best_bid = None
        self._best_ask = None

    def apply_snapshot(self, snap: Snapshot) -> bool:
        ch = self.changes
        ch.clear()
        for side, newlevels in ((BID, snap.bids), (ASK, snap.asks)):
            book = self.bids if side == BID else self.asks
            new = dict(newlevels)
            for px, old in book.items():
                if px not in new:
                    ch.append((side, px, old, 0.0))
            for px, sz in new.items():
                old = book.get(px, 0.0)
                if sz != old:
                    ch.append((side, px, old, sz))
            book.clear()
            book.update(new)
            
        if self.watched is not None:
            w = self.watched
            ch[:] = [c for c in ch if c[1] in w]
            
        self.last_id, self.synced = snap.last_update_id, True
        pending, self.buffer = self.buffer, []
        self._best_bid = None
        self._best_ask = None
        
        return all(self.apply_diff(d) for d in pending)

    def apply_diff(self, diff: DepthDiff) -> bool:
        if not self.synced:
            self.buffer.append(diff)
            return True
        if diff.final_id <= self.last_id:
            return True
            
        ch = self.changes
        ch.clear()
        watched = self.watched
        
        for side, levels in ((BID, diff.bids), (ASK, diff.asks)):
            book = self.bids if side == BID else self.asks
            get = book.get
            for px, sz in levels:
                old = get(px, 0.0)
                if sz == 0.0:
                    if old:
                        del book[px]
                    else:
                        continue
                else:
                    if sz == old:
                        continue
                    book[px] = sz
                if watched is None or px in watched:
                    ch.append((side, px, old, sz))
                    
        self.last_id = diff.final_id
        self._best_bid = None
        self._best_ask = None
        return True

    def best_bid(self) -> tuple[float, float] | None:
        if self._best_bid is None and self.bids:
            px = max(self.bids.keys())
            self._best_bid = (px, self.bids[px])
        return self._best_bid

    def best_ask(self) -> tuple[float, float] | None:
        if self._best_ask is None and self.asks:
            px = min(self.asks.keys())
            self._best_ask = (px, self.asks[px])
        return self._best_ask

    def asks_iter(self):
        return sorted(self.asks.items())

    def bids_iter(self):
        return sorted(self.bids.items(), reverse=True)

    def mid(self) -> float | None:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb and ba:
            return (bb[0] + ba[0]) / 2.0
        return None


class Engine:
    """Núcleo síncrono y determinista: un evento cada vez, sin E/S."""
    def __init__(self, symbols: list[str], request_snapshot: Callable[[str], None]):
        self.books = {s.upper(): L2Book(s.upper()) for s in symbols}
        self.request_snapshot = request_snapshot
        self.n_events = 0
        self.max_lag_ms = 0.0

    # ---- lógica pura (la misma en vivo y en replay) ----
    def on_event(self, ev: Event):
        self.n_events += 1
        if isinstance(ev, DepthDiff):
            b = self.books[ev.symbol]
            if not b.apply_diff(ev):
                log.warning("Hueco en %s: resync", ev.symbol)
                b.reset(); self.request_snapshot(ev.symbol)
        elif isinstance(ev, Trade):
            self.on_trade(ev)
        elif isinstance(ev, Snapshot):
            b = self.books[ev.symbol]
            if not b.apply_snapshot(ev):
                b.reset(); self.request_snapshot(ev.symbol)
        elif isinstance(ev, Disconnect):
            for b in self.books.values(): b.reset()

    def on_trade(self, t: Trade):
        pass  # aquí irán: tape, RollingZ, simulador de cola maker, etc.

    # ---- adaptador asíncrono en vivo ----
    async def run(self, q: asyncio.Queue):
        while True:
            ev = await q.get()
            self._handle(ev)
            while not q.empty():               # vacía la ráfaga sin ceder el loop
                self._handle(q.get_nowait())

    def _handle(self, ev: Event):
        self.on_event(ev)
        lag = (time.time_ns() - ev.recv_ns) / 1e6
        if lag > self.max_lag_ms: self.max_lag_ms = lag
