import json
from pathlib import Path
import os

def analyze_sombra(filepath):
    if not os.path.exists(filepath):
        print("El archivo de sombra no existe.")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        data = [json.loads(line) for line in f if line.strip()]

    total = len(data)
    aprobadas = [d for d in data if d.get("allow_trade")]
    vetadas = [d for d in data if not d.get("allow_trade")]

    print("="*50)
    print("📊 ANÁLISIS DE MODO SOMBRA (BACKTESTING)")
    print("="*50)
    print(f"Total señales procesadas: {total}")
    print(f"✅ Aprobadas por IA: {len(aprobadas)} ({(len(aprobadas)/total*100) if total else 0:.1f}%)")
    print(f"❌ Vetadas por IA: {len(vetadas)} ({(len(vetadas)/total*100) if total else 0:.1f}%)")

    # Análisis de rentabilidad teórica de las aprobadas
    print("\n📈 RENTABILIDAD TEÓRICA DE APROBADAS (Requiere cruce con precios futuros):")
    for a in aprobadas[-10:]: # Mostrar las últimas 10
        par = a.get("symbol")
        precio = a.get("precio_ref")
        tp = a.get("tp_pct")
        sl = a.get("sl_pct")
        risk = a.get("risk_score")
        print(f" - {par} | Precio: ${precio:.4f} | R/R: {tp}% / -{sl}% | Riesgo: {risk}/10")
        
    print("\n💡 Para lograr la rentabilidad total, se necesita cruzar el 'precio_ref' de entrada con los precios máximos y mínimos alcanzados en las 2 horas siguientes para saber si tocó el TP o el SL.")

if __name__ == "__main__":
    analyze_sombra("logs/sombra.json")
