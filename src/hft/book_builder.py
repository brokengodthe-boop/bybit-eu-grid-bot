"""
book_builder.py — Parser de alta velocidad y reconstructor de libros L2 para Bybit Linear.
Parsea frames crudos .jsonl.gz y reconstruye libros con enteros de tick para máxima precisión.
"""
import gzip, os
from typing import Iterator, Tuple, Dict, Any, List, Optional
import orjson

def iter_frames(path: str) -> Iterator[Tuple[int, int, Dict[str, Any]]]:
    """Itera sobre las líneas crudas de un archivo comprimido de recorder_linear."""
    with gzip.open(path, "rb") as f:
        for line in f:
            if not line:
                continue
            rec = orjson.loads(line)
            # Solo frames de mercado tienen 'raw'
            if "raw" in rec:
                yield rec["recvns"], rec["conn"], rec["raw"]

class Book:
    __slots__ = ("tick", "bids", "asks", "u", "seq")

    def __init__(self, tick: float):
        self.tick = tick
        self.bids: Dict[int, float] = {}  # tick_int -> size
        self.asks: Dict[int, float] = {}
        self.u: Optional[int] = None
        self.seq: Optional[int] = None

    def apply(self, msg: Dict[str, Any]) -> str:
        d = msg.get("data")
        if not d:
            return "empty"
        typ = msg.get("type", "")
        
        # u == 1 en Bybit significa reinicio de servicio -> reset limpio
        if typ == "snapshot" or d.get("u") == 1:
            self.bids.clear()
            self.asks.clear()
        elif self.u is not None and d.get("u") != self.u + 1:
            return "gap"

        tick = self.tick
        for side, book in (("b", self.bids), ("a", self.asks)):
            for p, q in d.get(side, []):
                k = round(float(p) / tick)
                sz = float(q)
                if sz == 0.0:
                    book.pop(k, None)
                else:
                    book[k] = sz

        self.u = d.get("u")
        self.seq = d.get("seq")
        return "ok"

    def best_bid(self) -> Optional[Tuple[float, float]]:
        if not self.bids:
            return None
        k = max(self.bids.keys())
        return (k * self.tick, self.bids[k])

    def best_ask(self) -> Optional[Tuple[float, float]]:
        if not self.asks:
            return None
        k = min(self.asks.keys())
        return (k * self.tick, self.asks[k])

    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb and ba:
            return (bb[0] + ba[0]) / 2.0
        return None

    def top(self, n: int = 5) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        tick = self.tick
        b_sorted = sorted(self.bids.items(), reverse=True)[:n]
        a_sorted = sorted(self.asks.items())[:n]
        return ([(k * tick, sz) for k, sz in b_sorted],
                [(k * tick, sz) for k, sz in a_sorted])
