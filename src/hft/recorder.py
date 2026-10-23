import gzip, queue, threading, time, os
import orjson

class Recorder:
    """Escribe mensajes crudos en JSONL gzip, rotando cada hora. Corre en su propio hilo."""
    def __init__(self, out_dir: str = "data", maxsize: int = 1_000_000):
        self.out_dir = out_dir
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, name="recorder", daemon=True)
        os.makedirs(out_dir, exist_ok=True)

    def start(self): self._th.start()

    def write(self, kind: str, recv_ns: int, raw: bytes | str):
        # Seguro entre hilos, no bloquea. Si se pierden mensajes, el replay ya no es fiel: vigílalo.
        try:
            self.q.put_nowait((kind, recv_ns, raw))
        except queue.Full:
            self.dropped += 1

    def _path(self, hour_key: str) -> str:
        return os.path.join(self.out_dir, f"md_{hour_key}.jsonl.gz")

    def _loop(self):
        f, cur_key = None, None
        try:
            while not (self._stop.is_set() and self.q.empty()):
                try:
                    kind, recv_ns, raw = self.q.get(timeout=0.5)
                except queue.Empty:
                    continue
                key = time.strftime("%Y%m%d_%H", time.gmtime(recv_ns / 1e9))
                if key != cur_key:
                    if f: f.close()
                    f, cur_key = gzip.open(self._path(key), "ab", compresslevel=3), key
                if isinstance(raw, str): raw = raw.encode()
                # Formato: {"k":tipo,"t":recv_ns,"d":<json crudo tal cual>}
                f.write(b'{"k":"' + kind.encode() + b'","t":' + str(recv_ns).encode()
                        + b',"d":' + raw + b"}\n")
        except Exception as e:
            print(f"Recorder thread crashed: {e}")
        finally:
            if f: f.close()

    def stop(self):
        self._stop.set()
        self._th.join()
