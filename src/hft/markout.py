from collections import deque

class MarkoutTracker:
    __slots__ = ("horizons", "queues", "results")

    def __init__(self, horizons_ns=(1000000000, 10000000000, 600000000000)):
        self.horizons = horizons_ns
        self.queues = [deque() for _ in horizons_ns]
        self.results = []   # [(fill_id, h_ns, markout_bps, censored)]

    def on_fill(self, fill_id: str, ts: int, side_sign: int, fill_px: float, mid_at_fill: float):
        # side_sign: +1 compra, -1 venta
        for h, q in zip(self.horizons, self.queues):
            q.append((ts + h, fill_id, side_sign, fill_px, mid_at_fill))

    def advance(self, next_ts: int, current_mid: float):
        """Llamar ANTES de aplicar el evento con timestamp next_ts.
        current_mid = mid del estado vigente (as-of)."""
        if current_mid is None or current_mid <= 0:
            return
            
        for h, q in zip(self.horizons, self.queues):
            while q and q[0][0] <= next_ts:
                _, fid, s, px, _ = q.popleft()
                bps = s * (current_mid - px) / px * 1e4
                self.results.append((fid, h, bps, False))

    def finalize(self, last_mid: float):
        """Fin de datos: los markouts pendientes quedan censurados. Márcalos y no los mezcles."""
        if last_mid is None or last_mid <= 0:
            return
            
        for h, q in zip(self.horizons, self.queues):
            while q:
                _, fid, s, px, _ = q.popleft()
                self.results.append((fid, h, s * (last_mid - px) / px * 1e4, True))
