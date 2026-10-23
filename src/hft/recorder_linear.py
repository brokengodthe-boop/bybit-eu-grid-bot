#!/usr/bin/env python3
"""
recorder_linear.py — Grabador multiplexado Bybit v5 Linear Perpetuals.

Uso:
    python src/hft/recorder_linear.py                      # universo por defecto
    python src/hft/recorder_linear.py USUSDT ATOMUSDT ...  # 1 proceso por símbolo

Salida:
    data_linear/md_YYYYMMDD_HH_connX.jsonl.gz
    Cada línea: {"recvns":int,"conn":int,"raw":<frame Bybit sin tocar>}
             o  {"recvns":int,"conn":int,"evt":"...", ...}  (eventos de control)
"""
import asyncio
import gc
import gzip
import json
import multiprocessing as mp
import os
import queue
import signal
import sys
import threading
import time
from collections import deque

try:
    import orjson
    loads = orjson.loads
except ImportError:
    loads = json.loads

import websockets

# ----------------------------- Configuración -----------------------------
WSURL = "wss://stream.bybit.com/v5/public/linear"
OUTDIR = os.environ.get("RECDIR", "data_linear")

DEFAULTSYMBOLS = ["USUSDT", "ATOMUSDT", "XLMUSDT", "TRUMPUSDT", "ADAUSDT"]

TOPICTEMPLATES = [
    "orderbook.1.{s}",    # BBO de alta frecuencia (cola / microestructura)
    "orderbook.50.{s}",   # profundidad L2
    "publicTrade.{s}",    # agresor + tamaño
    "tickers.{s}",        # mark, index, funding, OI
]

STRICTSEQ = False         # Empezamos en False para diagnosticar si Bybit tiene gaps naturales en u sin forzar tormentas de resync
PINGEVERYS = 20.0
WATCHDOGS = 30.0
FLUSHEVERYS = 5.0        # flush periódico del gzip
RESUBCOOLDOWNS = 1.0
MAXGAPSPERMIN = 20       # por encima: reconexión completa
STATSEVERYS = 60.0
GZLEVEL = 4              # Nivel de compresión balanceado (bajo uso de CPU)
HOURNS = 3_600_000_000_000  # 1 hora en nanosegundos

def log(connid, msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts}Z][conn{connid}] {msg}", file=sys.stderr, flush=True)

# ----------------------- Endurecimiento del sistema -----------------------
TIMERSET = False

def preventsleep():
    """Evita suspensión del SO y sube la resolución del timer a 1 ms (Windows)."""
    global TIMERSET
    if sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_AWAYMODE_REQUIRED = 0x00000040
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
    )
    if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
        TIMERSET = True

def restoretimer():
    global TIMERSET
    if sys.platform == "win32" and TIMERSET:
        import ctypes
        ctypes.windll.winmm.timeEndPeriod(1)
        TIMERSET = False

def gcharden():
    """Congela objetos de arranque y reduce la frecuencia de pausas del GC."""
    gc.collect()
    gc.freeze()
    gc.set_threshold(100000, 50, 100)

# ----------------------------- Reloj anclado -----------------------------
class Clock:
    __slots__ = ("wall0", "pc0")

    def __init__(self):
        self.pc0 = time.perf_counter_ns()
        self.wall0 = time.time_ns()

    def now(self) -> int:
        return self.wall0 + (time.perf_counter_ns() - self.pc0)

# ----------------------------- Hilo escritor -----------------------------
class Writer(threading.Thread):
    def __init__(self, connid: int, clock: Clock):
        super().__init__(daemon=True, name=f"writer-{connid}")
        self.q = queue.SimpleQueue()
        self.connid = connid
        self.clock = clock
        self.stopevt = threading.Event()
        self.fh = None
        self.curhour = None
        self.lines = 0

    def put(self, recvns: int, line: bytes):
        self.q.put((recvns, line))

    def event(self, recvns: int, evt: str, **kw):
        d = {"recvns": recvns, "conn": self.connid, "evt": evt}
        d.update(kw)
        self.q.put((recvns, (json.dumps(d, separators=(",", ":")) + "\n").encode()))

    def rotate(self, hourid: int, recvns: int):
        if self.fh is not None:
            self.fh.close()
        os.makedirs(OUTDIR, exist_ok=True)
        key = time.strftime("%Y%m%d_%H", time.gmtime(hourid * 3600))
        path = os.path.join(OUTDIR, f"md_{key}_conn{self.connid}.jsonl.gz")
        self.fh = gzip.open(path, "ab", compresslevel=GZLEVEL)
        self.curhour = hourid
        anchor = {
            "recvns": recvns, "conn": self.connid, "evt": "clock",
            "wall0": self.clock.wall0, "pc0": self.clock.pc0,
            "pcnow": time.perf_counter_ns(), "wallnow": time.time_ns(),
        }
        self.fh.write((json.dumps(anchor, separators=(",", ":")) + "\n").encode())
        log(self.connid, f"rotado -> {path}")

    def run(self):
        lastflush = time.monotonic()
        while True:
            try:
                recvns, line = self.q.get(timeout=0.5)
            except queue.Empty:
                recvns = None
            if recvns is not None:
                hourid = recvns // HOURNS
                if hourid != self.curhour:
                    self.rotate(hourid, recvns)
                self.fh.write(line)
                self.lines += 1
            elif self.stopevt.is_set():
                break
            now = time.monotonic()
            if self.fh is not None and now - lastflush >= FLUSHEVERYS:
                self.fh.flush()
                lastflush = now
        if self.fh is not None:
            self.fh.close()

# ----------------------------- Conexión WS -----------------------------
class Conn:
    def __init__(self, connid, symbols, stopevt, clock, writer):
        self.connid = connid
        self.symbols = symbols
        self.stopevt = stopevt
        self.clock = clock
        self.w = writer
        self.topics = [t.format(s=s) for s in symbols for t in TOPICTEMPLATES]
        self.prefix = b'{"recvns":%d,"conn":' + str(connid).encode() + b',"raw":'
        self.lastu = {}
        self.synced = {}
        self.lastresub = {}
        self.pending = set()
        self.gaptimes = deque()
        self.lastrx = time.monotonic()
        self.msgs = 0
        self.gaps = 0
        self.req = 0
        self.stopping = False

    def ev(self, evt, **kw):
        self.w.event(self.clock.now(), evt, **kw)

    async def send(self, ws, op, args):
        self.req += 1
        await ws.send(json.dumps({"op": op, "args": args, "req_id": f"{self.connid}-{self.req}"}))

    async def run(self):
        backoff = 1.0
        while not self.stopping:
            try:
                async with websockets.connect(
                    WSURL, ping_interval=None, max_size=2 ** 24,
                    open_timeout=10, close_timeout=2,
                ) as ws:
                    backoff = 1.0
                    self.lastu.clear()
                    self.pending.clear()
                    self.synced = {t: False for t in self.topics if t.startswith("orderbook.")}
                    self.ev("connect", topics=self.topics)
                    log(self.connid, f"conectado a Bybit Linear {self.symbols}")
                    # Suscribir en lotes de 10
                    for i in range(0, len(self.topics), 10):
                        await self.send(ws, "subscribe", self.topics[i:i+10])
                    await self.session(ws)
            except Exception as e:
                self.ev("disconnect", err=repr(e)[:300])
                log(self.connid, f"desconectado: {e!r}")
            if self.stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def session(self, ws):
        self.lastrx = time.monotonic()
        tick = asyncio.create_task(self.tick(ws))
        prefix = self.prefix
        put = self.w.put
        clocknow = self.clock.now
        try:
            async for raw in ws:
                recvns = clocknow()
                self.lastrx = time.monotonic()
                b = raw.encode() if isinstance(raw, str) else raw
                put(recvns, (prefix % recvns) + b + b"}\n")
                self.msgs += 1
                if b.startswith(b'{"topic":"orderbook') or not b.startswith(b'{"topic"'):
                    await self.inspect(ws, b)
        finally:
            tick.cancel()

    async def tick(self, ws):
        lastping = 0.0
        laststats = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if self.stopevt.is_set():
                self.stopping = True
                await ws.close()
                return
            if now - self.lastrx > WATCHDOGS:
                self.ev("watchdog", silents=round(now - self.lastrx, 1))
                log(self.connid, "watchdog: sin datos, reconectando")
                await ws.close()
                return
            if now - lastping >= PINGEVERYS:
                await ws.send('{"op":"ping"}')
                lastping = now
            for topic in list(self.pending):
                if now - self.lastresub.get(topic, 0.0) >= RESUBCOOLDOWNS:
                    self.pending.discard(topic)
                    await self.resub(ws, topic)
            if now - laststats >= STATSEVERYS:
                log(self.connid, f"msgs={self.msgs} gaps={self.gaps} "
                                  f"q={self.w.q.qsize()} lines={self.w.lines}")
                laststats = now

    async def inspect(self, ws, b):
        try:
            msg = loads(b)
        except Exception:
            self.ev("badframe", head=b[:120].decode("utf-8", "replace"))
            return
        topic = msg.get("topic")
        if topic is None:
            if msg.get("success") is False:
                self.ev("operror", resp=msg)
                log(self.connid, f"operror: {msg}")
            return
        data = msg.get("data") or {}
        u = data.get("u")
        if u is None:
            return
        if msg.get("type") == "snapshot":
            if not self.synced.get(topic, False):
                self.ev("synced", topic=topic, u=u)
            self.lastu[topic] = u
            self.synced[topic] = True
            return
        if not self.synced.get(topic, False):
            return
        prev = self.lastu.get(topic)
        if prev is not None and u == prev + 1:
            self.lastu[topic] = u
            return
        # GAP
        self.gaps += 1
        self.ev("gap", topic=topic, prev=prev, u=u)
        if not STRICTSEQ:
            self.lastu[topic] = u
            return
        self.synced[topic] = False
        await self.ongap(ws, topic)

    async def ongap(self, ws, topic):
        now = time.monotonic()
        gt = self.gaptimes
        gt.append(now)
        while gt and now - gt[0] > 60.0:
            gt.popleft()
        if len(gt) > MAXGAPSPERMIN:
            self.ev("gapstorm", n=len(gt))
            log(self.connid, "gap storm: reconexión completa")
            gt.clear()
            await ws.close()
            return
        if now - self.lastresub.get(topic, 0.0) < RESUBCOOLDOWNS:
            self.pending.add(topic)
        else:
            await self.resub(ws, topic)

    async def resub(self, ws, topic):
        self.lastresub[topic] = time.monotonic()
        self.ev("resub", topic=topic)
        await self.send(ws, "unsubscribe", [topic])
        await self.send(ws, "subscribe", [topic])

# ----------------------------- Procesos -----------------------------
def worker(connid, symbols, stopevt):
    try:
        signal.signal(signal.SIGINT, signal.SIGIGN)
    except (ValueError, AttributeError):
        pass
    preventsleep()
    clock = Clock()
    writer = Writer(connid, clock)
    writer.start()
    gcharden()
    conn = Conn(connid, symbols, stopevt, clock, writer)
    try:
        asyncio.run(conn.run())
    finally:
        writer.event(clock.now(), "shutdown")
        writer.stopevt.set()
        writer.join(timeout=15)
        restoretimer()
        log(connid, "terminado")

def main():
    symbols = [s.upper() for s in sys.argv[1:]] or DEFAULTSYMBOLS
    groups = [[s] for s in symbols]  # 1 proceso = 1 símbolo = 1 socket independiente
    os.makedirs(OUTDIR, exist_ok=True)
    preventsleep()

    stopevt = mp.Event()
    procs = {}

    def spawn(i):
        p = mp.Process(target=worker, args=(i, groups[i], stopevt),
                       name=f"rec-{groups[i][0]}")
        p.start()
        procs[i] = p
        log("-", f"lanzado conn{i} {groups[i]} pid={p.pid}")

    for i in range(len(groups)):
        spawn(i)

    try:
        while True:
            time.sleep(2.0)
            for i, p in list(procs.items()):
                if not p.is_alive():
                    log("-", f"conn{i} murió (exit={p.exitcode}), relanzando")
                    spawn(i)
    except KeyboardInterrupt:
        log("-", "parando grabador multiplexado...")
        stopevt.set()
        for p in procs.values():
            p.join(timeout=10)
        for p in procs.values():
            if p.is_alive():
                p.terminate()
    finally:
        restoretimer()

if __name__ == "__main__":
    mp.freeze_support()
    main()
