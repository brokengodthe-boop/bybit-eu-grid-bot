import asyncio, time, logging
import aiohttp, orjson, websockets
from domain import DepthDiff, Trade, Snapshot, Disconnect, Event
from recorder import Recorder

log = logging.getLogger("md")
WS_BASE = "wss://stream.bybit.com/v5/public/spot"

# ---------- Parsers puros (compartidos con replay.py) ----------
def _lv(rows) -> tuple:
    return tuple((float(p), float(q)) for p, q in rows)

def parse_ws(raw: bytes | str, recv_ns: int) -> list[Event]:
    msg = orjson.loads(raw)
    topic = msg.get("topic", "")
    events = []
    
    if topic.startswith("orderbook"):
        d = msg.get("data", {})
        if not d: return []
        symbol = d.get("s")
        seq = d.get("seq", 0)
        ts = msg.get("ts", 0)
        bids = _lv(d.get("b", []))
        asks = _lv(d.get("a", []))
        
        if msg.get("type") == "snapshot":
            events.append(Snapshot(symbol, seq, bids, asks, recv_ns))
        else:
            events.append(DepthDiff(symbol, seq, seq, bids, asks, ts, recv_ns))
            
    elif topic.startswith("publicTrade"):
        data = msg.get("data", [])
        for t in data:
            # S is side of the aggressor. "Buy" means buyer was taker, so maker was seller.
            buyer_maker = True if t.get("S") == "Sell" else False
            # Bybit trade ID is often a string UUID or big int. 
            # We hash it or just keep it as int if possible. Let's use hash() for simplicity.
            tid = hash(t.get("i", 0)) 
            events.append(Trade(t.get("s"), tid, float(t.get("p")), float(t.get("v")), buyer_maker, t.get("T"), recv_ns))
            
    return events

# ---------- E/S ----------
class BybitFeed:
    def __init__(self, symbols: list[str], events: asyncio.Queue,
                 control: asyncio.Queue, recorder: Recorder):
        self.symbols = [s.upper() for s in symbols]
        self.events, self.control, self.rec = events, control, recorder
        self.url = WS_BASE

    def _emit(self, ev: Event):
        self.events.put_nowait(ev)

    async def run_ws(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, max_size=2**23, ping_interval=20, ping_timeout=20) as ws:
                    log.info("WS conectado a Bybit")
                    backoff = 1
                    
                    book_args = [f"orderbook.50.{s}" for s in self.symbols]
                    trade_args = [f"publicTrade.{s}" for s in self.symbols]
                    
                    # Suscribir en lotes de 10
                    for i in range(0, len(book_args), 10):
                        await ws.send(orjson.dumps({"op": "subscribe", "args": book_args[i:i+10]}).decode())
                    for i in range(0, len(trade_args), 10):
                        await ws.send(orjson.dumps({"op": "subscribe", "args": trade_args[i:i+10]}).decode())

                    async for raw in ws:
                        t = time.time_ns()
                        self.rec.write("ws", t, raw)  # 1) grabar crudo
                        evs = parse_ws(raw, t)         # 2) interpretar
                        for ev in evs:
                            self._emit(ev)
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("WS caído: %s", e)
            
            t = time.time_ns()
            self.rec.write("disc", t, b"null")
            self._emit(Disconnect(t))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def run_control(self):
        """Atiende peticiones del motor (snapshots). En Bybit resuscribiremos más adelante si hay hueco."""
        while True:
            cmd, symbol = await self.control.get()
            if cmd != "snapshot":
                continue
            log.warning(f"Snapshot manual solicitado para {symbol}. Pendiente de implementar reconexión para forzar nuevo snapshot en Bybit.")
