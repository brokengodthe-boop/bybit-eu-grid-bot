#!/usr/bin/env python3
"""
dump_tables.py — Pasada 1: frames JSONL.gz -> Parquet (bbo / book / trades).
Parsea los archivos comprimidos de recorder_linear y los vuelca en tablas columnares optimizadas.
"""
import gzip, sys, os, orjson
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

DEPTH = 10              # niveles por lado que se guardan en la tabla 'book'
FLUSH_ROWS = 250_000     # volcar en row groups para limitar el consumo de RAM

def unwrap(line: bytes):
    o = orjson.loads(line)
    if "raw" in o:
        return o.get("recvns"), o["raw"]
    return o.get("recvns"), o

class Book:
    __slots__ = ("bids", "asks", "tick")

    def __init__(self, tick: float):
        self.bids = {}
        self.asks = {}
        self.tick = tick

    def k(self, p: float | str) -> int:
        return round(float(p) / self.tick)

    def apply(self, side: dict, levels: list):
        k_func = self.k
        for p, s in levels:
            kk, ss = k_func(p), float(s)
            if ss == 0.0:
                side.pop(kk, None)
            else:
                side[kk] = ss

    def top(self, n: int = DEPTH):
        b = sorted(self.bids.items(), reverse=True)[:n]
        a = sorted(self.asks.items())[:n]
        return b, a

class Sink:
    """Acumula columnas en memoria y escribe row groups en un único fichero Parquet con compresión zstd."""
    def __init__(self, path: Path, schema: pa.Schema):
        self.path = path
        self.schema = schema
        self.cols = {f.name: [] for f in schema}
        self.writer = None
        self.n = 0

    def add(self, **row):
        for k, v in row.items():
            self.cols[k].append(v)
        self.n += 1
        if self.n >= FLUSH_ROWS:
            self.flush()

    def flush(self):
        if self.n == 0:
            return
        t = pa.Table.from_pydict(self.cols, schema=self.schema)
        if self.writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.writer = pq.ParquetWriter(str(self.path), self.schema, compression="zstd")
        self.writer.write_table(t)
        self.cols = {f.name: [] for f in self.schema}
        self.n = 0

    def close(self):
        self.flush()
        if self.writer is not None:
            self.writer.close()

# ----------------- Esquemas Columnas PyArrow -----------------
BBO_SCHEMA = pa.schema([
    ("ts_ex", pa.int64()),
    ("recv_ns", pa.int64()),
    ("u", pa.int64()),
    ("seq", pa.int64()),
    ("bid_k", pa.int64()),
    ("bid_sz", pa.float64()),
    ("ask_k", pa.int64()),
    ("ask_sz", pa.float64()),
])

BOOK_FIELDS = [("ts_ex", pa.int64()), ("u", pa.int64())]
for s in ("b", "a"):
    for i in range(DEPTH):
        BOOK_FIELDS.append((f"{s}{i}_k", pa.int64()))
        BOOK_FIELDS.append((f"{s}{i}_sz", pa.float64()))
BOOK_SCHEMA = pa.schema(BOOK_FIELDS)

TRADES_SCHEMA = pa.schema([
    ("ts_ex", pa.int64()),
    ("recv_ns", pa.int64()),
    ("px_k", pa.int64()),
    ("qty", pa.float64()),
    ("side", pa.int8()),
    ("trade_id", pa.string()),
    ("block", pa.bool_()),
])

def run(src: Path, out_root: Path, symbol: str, tick: float):
    # Formato de nombre: md_YYYYMMDD_HH_connX.jsonl.gz
    parts = src.stem.split(".")[0].split("_")
    if len(parts) >= 3:
        date, hour = parts[1], parts[2]
    else:
        date, hour = "unknown", "00"

    base = out_root / f"symbol={symbol}" / f"date={date}"
    sb = Sink(base / f"bbo_{hour}.parquet", BBO_SCHEMA)
    sk = Sink(base / f"book_{hour}.parquet", BOOK_SCHEMA)
    st = Sink(base / f"trades_{hour}.parquet", TRADES_SCHEMA)

    book = Book(tick)
    print(f"[*] Procesando {src} para {symbol} (tick={tick})...")
    lines_count = 0

    with gzip.open(src, "rb") as f:
        for line in f:
            if not line:
                continue
            recv, m = unwrap(line)
            if not isinstance(m, dict):
                continue
            topic = m.get("topic", "")
            lines_count += 1

            if topic.startswith("orderbook.50"):
                d = m.get("data", {})
                if not d:
                    continue
                if m.get("type") == "snapshot" or d.get("u") == 1:
                    book.bids.clear()
                    book.asks.clear()

                book.apply(book.bids, d.get("b", []))
                book.apply(book.asks, d.get("a", []))

                b, a = book.top(DEPTH)
                if not b or not a:
                    continue

                ts = int(m.get("cts") or m.get("ts", 0))
                u = int(d.get("u", 0))
                seq = int(d.get("seq", 0))

                sb.add(
                    ts_ex=ts,
                    recv_ns=recv or 0,
                    u=u,
                    seq=seq,
                    bid_k=b[0][0],
                    bid_sz=b[0][1],
                    ask_k=a[0][0],
                    ask_sz=a[0][1],
                )

                row = {"ts_ex": ts, "u": u}
                for pref, lv in (("b", b), ("a", a)):
                    for i in range(DEPTH):
                        if i < len(lv):
                            kk, ss = lv[i]
                        else:
                            kk, ss = 0, 0.0
                        row[f"{pref}{i}_k"] = kk
                        row[f"{pref}{i}_sz"] = ss
                sk.add(**row)

            elif topic.startswith("publicTrade"):
                data = m.get("data", [])
                for t in data:
                    st.add(
                        ts_ex=int(t.get("T", 0)),
                        recv_ns=recv or 0,
                        px_k=book.k(t.get("p", 0.0)),
                        qty=float(t.get("v", 0.0)),
                        side=1 if t.get("S") == "Buy" else -1,
                        trade_id=str(t.get("i", "")),
                        block=bool(t.get("BT", False)),
                    )

    for s in (sb, sk, st):
        s.close()
    print(f"[+] Finalizado {src}: {lines_count} líneas convertidas a Parquet en {base}")

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Uso: python dump_tables.py <archivo.jsonl.gz> <out_dir> <symbol> <tick>")
        sys.exit(1)
    run(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], float(sys.argv[4]))
