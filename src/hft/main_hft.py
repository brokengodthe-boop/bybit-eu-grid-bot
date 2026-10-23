import asyncio, logging, signal
from recorder import Recorder
from market_data import BybitFeed
from engine import Engine

SYMBOLS = ["BTCUSDT", "SOLUSDT"]

async def stats(engine, rec):
    while True:
        await asyncio.sleep(10)
        b = engine.books["BTCUSDT"]
        logging.info("ev=%d lag_max=%.1fms drop=%d recq=%d BTC bid=%s ask=%s",
                     engine.n_events, engine.max_lag_ms, rec.dropped,
                     rec.q.qsize(), b.best_bid(), b.best_ask())
        engine.max_lag_ms = 0.0

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    rec = Recorder("data"); rec.start()
    events, control = asyncio.Queue(), asyncio.Queue()
    engine = Engine(SYMBOLS, lambda s: control.put_nowait(("snapshot", s)))
    feed = BybitFeed(SYMBOLS, events, control, rec)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError): pass  # Windows can raise ValueError for some signals

    tasks = [asyncio.create_task(c) for c in
             (feed.run_ws(), feed.run_control(), engine.run(events), stats(engine, rec))]
    await stop.wait()
    for t in tasks: t.cancel()
    rec.stop()

if __name__ == "__main__":
    try:
        import uvloop; uvloop.install()
    except ImportError:
        pass
    asyncio.run(main())
