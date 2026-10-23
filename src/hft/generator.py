import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from collections import deque
import heapq
import time
from engine import L2Book
from exec_model import ExecModel, Side, OStatus
from markout import MarkoutTracker

HORIZONS_NS = np.array([1, 5, 30, 60, 300]) * 1_000_000_000
EXEC_VARIANTS = [(0, int(2e9)), (1, int(2e9)), (0, int(10e9)), (1, int(10e9))]  # (offset_ticks, ttl_ns)
TICK_SIZE = 0.1 # BTCUSDT

class Level1Generator:
    def __init__(self, exec_model: ExecModel, z_loose=-1.5, rearm_step=0.5,
                 out_path="data/ledgerdump.parquet", flush_every=200000):
        self.ex = exec_model
        self.mk = MarkoutTracker(tuple(HORIZONS_NS))
        self.z_loose, self.rearm_step = z_loose, rearm_step
        self.armed, self.last_fire_z, self.episode = True, 0.0, 0
        self.next_id = 0
        self.live = {}        # candid -> dict (filas pendientes de completar)
        self.rows = []        # filas cerradas
        self.writer, self.out_path, self.flush_every = None, out_path, flush_every
        
        self.schema = pa.schema([
            ("candid", pa.int64()), ("episodeid", pa.int64()), ("variant", pa.int64()),
            ("tsignal", pa.int64()), ("z", pa.float64()), ("obi", pa.float64()),
            ("volratio", pa.float64()), ("trend", pa.float64()), ("spread", pa.float64()),
            ("limitpx", pa.float64()), ("queueahead", pa.float64()),
            ("tfill", pa.int64()), ("fillpx", pa.float64()), ("texit", pa.int64()),
            ("filled", pa.bool_()), ("mae", pa.float64()), ("mfe", pa.float64())
        ] + [(f"mo{i}", pa.float64()) for i in range(len(HORIZONS_NS))])

    def on_tick(self, ts: int, book: L2Book, feats: dict):
        z = feats.get("z", 0.0)

        # 1) Reclamar markouts listos
        self.mk.advance(ts, book.mid())
        if self.mk.results:
            for fid, h, bps, _ in self.mk.results:
                r = self.live.get(fid)
                if r:
                    hidx = list(self.mk.horizons).index(h)
                    r[f"mo{hidx}"] = bps
                    if hidx == len(HORIZONS_NS) - 1:
                        # Simulamos MAE y MFE rápido
                        r["mae"] = min([r[f"mo{i}"] for i in range(len(HORIZONS_NS))])
                        r["mfe"] = max([r[f"mo{i}"] for i in range(len(HORIZONS_NS))])
                        r["texit"], r["filled"] = ts, True
                        self.close(fid)
            self.mk.results.clear()

        # 2) Verificar órdenes virtuales
        # Ejecutamos el reloj de ExecModel para procesar TTL y latencias
        self.ex.on_time(ts)
        done_oids = []
        for oid, o in self.ex.orders.items():
            if o.status == OStatus.FILLED:
                cid = int(oid)
                if cid in self.live and self.live[cid]["tfill"] is None:
                    # Rellenar datos
                    fill = o.fills[0] # ts, px, qty
                    self.live[cid]["tfill"] = fill[0]
                    self.live[cid]["fillpx"] = fill[1]
                    self.mk.on_fill(cid, fill[0], +1, fill[1], book.mid())
                done_oids.append(oid)
            elif o.status == OStatus.CANCELED:
                cid = int(oid)
                if cid in self.live and self.live[cid]["tfill"] is None:
                    self.live[cid]["texit"], self.live[cid]["filled"] = ts, False
                    self.close(cid)
                done_oids.append(oid)
        
        for oid in done_oids:
            self.ex.orders.pop(oid, None)

        # 3) Lógica de disparo
        if z > self.z_loose:
            self.armed = True
            return
        fire = self.armed or (z <= self.last_fire_z - self.rearm_step)
        if not fire:
            return
        if self.armed:
            self.episode += 1
        self.armed, self.last_fire_z = False, z

        # 4) Generar variantes
        bb = book.best_bid()
        if not bb:
            return
            
        for vidx, (off, ttl) in enumerate(EXEC_VARIANTS):
            cid = self.next_id; self.next_id += 1
            px = bb[0] - off * TICK_SIZE
            qa = 0.0
            for p, q in book.bids_iter():
                if p == px: qa = q; break
                
            self.live[cid] = dict(
                candid=cid, episodeid=self.episode, variant=vidx,
                tsignal=ts, z=z, obi=feats.get("obi", 0.0), volratio=feats.get("volratio", 1.0),
                trend=feats.get("trend", 0.0), spread=feats.get("spread", 0.1),
                limitpx=px, queueahead=qa,
                tfill=None, fillpx=np.nan, texit=None, filled=False,
                mae=np.nan, mfe=np.nan,
                **{f"mo{i}": np.nan for i in range(len(HORIZONS_NS))}
            )
            oid_str = str(cid)
            self.ex.limit_buy(px, 0.01, ts, oid=oid_str)
            # Aplicar TTL manualmente al o
            o = self.ex.orders.get(oid_str)
            if o: o.tcancel_effective = ts + ttl

    def close(self, cid: int):
        self.rows.append(self.live.pop(cid))
        if len(self.rows) >= self.flush_every:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        tbl = pa.Table.from_pylist(self.rows, schema=self.schema)
        if self.writer is None:
            self.writer = pq.ParquetWriter(self.out_path, tbl.schema, compression="zstd")
        self.writer.write_table(tbl)
        self.rows.clear()

    def finalize(self):
        self.live.clear()
        self.flush()
        if self.writer:
            self.writer.close()
