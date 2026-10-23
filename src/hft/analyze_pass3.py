#!/usr/bin/env python3
import polars as pl

df = pl.read_csv("pass3_8800182e.csv")
print(f"Total filas evaluadas: {len(df)}")

test_df = df.filter((pl.col("split") == "test") & (pl.col("q") == "pesimista") & (pl.col("lat") == 100))
agg = (test_df.group_by(["sym", "off"])
       .agg([
           pl.col("nfills").mean().alias("avg_fills"),
           pl.col("fillrate").mean().alias("avg_fillrate"),
           pl.col("fracthrough").mean().alias("avg_fracthrough"),
           pl.col("mo1").mean().alias("avg_mo1"),
           pl.col("mo5").mean().alias("avg_mo5"),
           pl.col("edge5net").mean().alias("avg_edge5net"),
           pl.col("cilo").mean().alias("avg_cilo"),
       ]).sort(["sym", "off"]))

print("\n--- RESUMEN POR SIMBOLO Y OFFSET (Test OOS, Cola Pesimista, Lat 100ms) ---")
for r in agg.to_dicts():
    print(f"{r['sym']} off={r['off']}: fills={r['avg_fills']:.0f}, fillrate={r['avg_fillrate']*100:.1f}%, "
          f"fracthrough={r['avg_fracthrough']*100:.1f}%, mo1={r['avg_mo1']:.2f} bps, mo5={r['avg_mo5']:.2f} bps, "
          f"edge5net={r['avg_edge5net']:.2f} bps, cilo={r['avg_cilo']:.2f} bps")

print("\n--- COMPARATIVA DE BRAZOS: U (Incondicional) vs C (FlowExhaustion) vs P (Placebo) ---")
arm_agg = (test_df.filter(pl.col("off") == "0")
           .group_by(["sym", "arm"])
           .agg([
               pl.col("nfills").mean().alias("avg_fills"),
               pl.col("mo1").mean().alias("avg_mo1"),
               pl.col("mo5").mean().alias("avg_mo5"),
               pl.col("edge5net").mean().alias("avg_edge5net"),
           ]).sort(["sym", "arm"]))

for r in arm_agg.to_dicts():
    print(f"{r['sym']} [{r['arm']}]: fills={r['avg_fills']:.0f}, mo1={r['avg_mo1']:.2f} bps, "
          f"mo5={r['avg_mo5']:.2f} bps, edge5net={r['avg_edge5net']:.2f} bps")
