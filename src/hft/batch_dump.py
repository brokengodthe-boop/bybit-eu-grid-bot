#!/usr/bin/env python3
"""
batch_dump.py — Procesa por lotes todos los archivos .jsonl.gz completados en data_linear/
y los convierte a Parquet particionados usando dump_tables.py.
"""
import glob, sys, os
from pathlib import Path
from dump_tables import run

TICKS = {
    "USUSDT": 0.00001,
    "ATOMUSDT": 0.001,
    "XLMUSDT": 0.00001,
    "TRUMPUSDT": 0.001,
    "ADAUSDT": 0.0001,
}

CONN_MAP = {
    "conn0": "USUSDT",
    "conn1": "ATOMUSDT",
    "conn2": "XLMUSDT",
    "conn3": "TRUMPUSDT",
    "conn4": "ADAUSDT",
}

def main(src_dir="data_linear", out_dir="parquet"):
    files = sorted(glob.glob(os.path.join(src_dir, "md_*_conn*.jsonl.gz")))
    print(f"[*] Encontrados {len(files)} archivos en {src_dir}")

    # Excluir el archivo de la hora actual más reciente si está en uso
    for f in files:
        fname = os.path.basename(f)
        parts = fname.replace(".jsonl.gz", "").split("_")
        if len(parts) < 4:
            continue
        conn = parts[3]
        sym = CONN_MAP.get(conn)
        if not sym or sym not in TICKS:
            continue
        tick = TICKS[sym]
        date, hour = parts[1], parts[2]
        target_check = Path(out_dir) / f"symbol={sym}" / f"date={date}" / f"bbo_{hour}.parquet"
        if target_check.exists():
            continue
        # Evitar archivo en escritura activa de la hora más reciente
        if f in files[-5:]:
            continue
        try:
            run(Path(f), Path(out_dir), sym, tick)
        except Exception as e:
            print(f"[!] Error procesando {f}: {e}")

if __name__ == "__main__":
    main()
