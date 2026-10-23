import glob, gzip, sys
import orjson
from market_data import parse_ws
from domain import Disconnect
from engine import Engine

def iter_events(pattern: str):
    for path in sorted(glob.glob(pattern)):
        with gzip.open(path, "rb") as f:
            try:
                for line in f:
                    rec = orjson.loads(line)
                    raw, t = orjson.dumps(rec["d"]), rec["t"]
                    k = rec["k"]
                    if k == "ws":
                        evs = parse_ws(raw, t)
                        for ev in evs:
                            yield ev
                    elif k == "disc":
                        yield Disconnect(t)
            except EOFError:
                pass # Archivo probablemente en uso/escritura


def replay(pattern: str, symbols: list[str]):
    # En replay se ignoran las peticiones de snapshot: los snapshots grabados ya vienen en el flujo
    engine = Engine(symbols, request_snapshot=lambda s: None)
    for ev in iter_events(pattern):
        engine.on_event(ev)              # síncrono, a máxima velocidad
    print("eventos:", engine.n_events, "BTC:", engine.books["BTCUSDT"].best_bid())

if __name__ == "__main__":
    replay(sys.argv[1] if len(sys.argv) > 1 else "data/md_*.jsonl.gz", ["BTCUSDT", "SOLUSDT"])
