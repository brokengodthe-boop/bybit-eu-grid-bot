import asyncio, sys
sys.path.append('src/hft')
from recorder_linear import Clock, Writer, Conn

async def run_test():
    clock = Clock()
    writer = Writer(0, clock)
    writer.start()
    stopevt = asyncio.Event()
    conn = Conn(0, ["ATOMUSDT"], stopevt, clock, writer)
    
    task = asyncio.create_task(conn.run())
    await asyncio.sleep(5)
    stopevt.set()
    task.cancel()
    writer.stopevt.set()
    writer.join(timeout=5)
    print(f"Test finalizado. Mensajes recibidos en 5s: {conn.msgs}, Gaps: {conn.gaps}, Lineas escritas: {writer.lines}")

if __name__ == "__main__":
    asyncio.run(run_test())
