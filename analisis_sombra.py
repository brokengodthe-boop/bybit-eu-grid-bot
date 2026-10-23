import json
import sys
import random
from collections import defaultdict

def get_percentile(data, percentile):
    if not data: return 0
    size = len(data)
    return sorted(data)[int((size * percentile) / 100) if size > 1 else 0]

def run_analysis(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            operaciones = json.load(f)
    except FileNotFoundError:
        print(f"El archivo {filepath} no existe.")
        return

    target_version = None
    for arg in sys.argv:
        if arg.startswith("--version="):
            target_version = arg.split("=")[1]
            break
            
    if target_version:
        print(f"Filtrando por versión: {target_version}")
        operaciones = [op for op in operaciones if op.get("version") == target_version]

    resueltas = [op for op in operaciones if op.get("resuelto") == True]
    
    if not resueltas:
        print("No hay operaciones resueltas en el archivo para esa versión.")
        return
        
    aprobadas = [op for op in resueltas if op.get("allow_trade") == True]
    
    n_total = len(resueltas)
    n_aprobadas = len(aprobadas)
    
    if n_aprobadas == 0:
        print("No hay operaciones aprobadas resueltas para analizar.")
        return

    def calc_metrics(ops):
        if not ops: return 0, 0, 0, 0, 0, 0, 0
        tp_count = sum(1 for op in ops if op.get("outcome") == "TP")
        sl_count = sum(1 for op in ops if op.get("outcome") == "SL")
        time_count = sum(1 for op in ops if op.get("outcome") == "TIME")
        
        winrate_estricto = (tp_count / len(ops)) * 100
        ganadoras_amplio = tp_count + sum(1 for op in ops if op.get("outcome") == "TIME" and op.get("pnl_neto_pct", 0) > 0)
        winrate_amplio = (ganadoras_amplio / len(ops)) * 100
        
        pnl_neto = sum(op.get("pnl_neto_pct", 0) for op in ops)
        
        # Costos fijos según main.py para calcular R modelado (riesgo presupuestado)
        FEE_LADO_PCT = 0.10
        STOP_SLIPPAGE_PCT = 0.05
        GAP_BUFFER_PCT = 0.20
        
        def calc_riesgo_presupuestado(sl_pct):
            return sl_pct + (2 * FEE_LADO_PCT) + STOP_SLIPPAGE_PCT + GAP_BUFFER_PCT

        # R por distancia bruta al stop
        sum_r_distancia = sum(op.get("pnl_neto_pct", 0) / op.get("sl_pct", 1.0) for op in ops if op.get("sl_pct", 0) > 0)
        # R por riesgo presupuestado (incluye costos de ejecución y colchón)
        sum_r_presupuestado = sum(op.get("pnl_neto_pct", 0) / calc_riesgo_presupuestado(op.get("sl_pct", 1.0)) for op in ops if op.get("sl_pct", 0) > 0)
        
        avg_r_distancia = sum_r_distancia / len(ops)
        avg_r_presupuestado = sum_r_presupuestado / len(ops)
        
        return tp_count, sl_count, time_count, winrate_estricto, winrate_amplio, pnl_neto, avg_r_distancia, avg_r_presupuestado

    tp, sl, tc, wr_estricto, wr_amplio, pnl, avg_r_dist, avg_r_pres = calc_metrics(aprobadas)
    
    print("\n" + "="*50)
    print("ANALISIS DE SENALES APROBADAS POR IA")
    print("="*50)
    print(f"Muestra: {n_aprobadas} operaciones (de {n_total} totales resueltas)")
    
    if n_aprobadas < 30:
        print("[!] ADVERTENCIA: Muestra estadisticamente INSIGNIFICANTE (n < 30). No concluir nada.")
    elif n_aprobadas < 100:
        print("[!] ADVERTENCIA: Muestra util solo para INDICIOS (30 <= n < 100). No definitiva.")
    else:
        print("[✓] Muestra estadisticamente ROBUSTA (n >= 100).")
        
    print(f"\nDesglose: {tp} TP | {sl} SL | {tc} Cierre por Tiempo")
    print(f"Winrate (Estricto - solo TP): {wr_estricto:.2f}%")
    print(f"Winrate (Amplio - TP + Time positivo): {wr_amplio:.2f}%")
    print(f"PnL Neto Acumulado No Ponderado: {pnl:.4f}%")
    print(f"R-Multiplo (Distancia al SL): {avg_r_dist:.3f}R por operacion")
    print(f"R-Multiplo (Riesgo Presupuestado): {avg_r_pres:.3f}R por operacion (Alineado con Sizing de main.py)")
    print("="*50)
    
    # ---------------------------------------------------------
    # COMPARACION EXPLORATORIA (Frente a la Cohorte Actual)
    # ---------------------------------------------------------
    print("\nCOMPARACION EXPLORATORIA FRENTE A LA COHORTE (Permutacion Aleatoria)")
    iters = 10000
    random_rs = []
    
    for _ in range(iters):
        muestra = random.sample(resueltas, n_aprobadas)
        _, _, _, _, _, _, _, m_r_pres = calc_metrics(muestra)
        random_rs.append(m_r_pres)
        
    p_value = sum(1 for r in random_rs if r >= avg_r_pres) / len(random_rs)
    percentil_superado = (1 - p_value) * 100
    
    r_5 = get_percentile(random_rs, 5)
    r_95 = get_percentile(random_rs, 95)
    
    print(f"Veredicto: El R-Multiplo de la IA ({avg_r_pres:.3f}R) supera al {percentil_superado:.2f}% de extracciones aleatorias (N={n_aprobadas}) de esta misma cohorte.")
    print(f"Rango del 90% central esperado al azar: [{r_5:.3f}R  a  {r_95:.3f}R]\n")

    # ---------------------------------------------------------
    # ANALISIS DE MFE / MAE (Maximum Favorable / Adverse Excursion)
    # ---------------------------------------------------------
    print("="*50)
    print("ANALISIS AVANZADO DE EXCURSION (MFE / MAE)")
    print("="*50)
    
    sl_ops = [op for op in aprobadas if op.get("outcome") == "SL"]
    tp_ops = [op for op in aprobadas if op.get("outcome") == "TP"]
    
    if sl_ops:
        sl_mfes = [op["mfe_pct"] for op in sl_ops if "mfe_pct" in op]
        if sl_mfes:
            avg_mfe_sl = sum(sl_mfes) / len(sl_mfes)
            print(f"Para los {len(sl_ops)} SL tocados ({len(sl_mfes)} con datos validos), el precio subio en promedio +{avg_mfe_sl:.2f}% antes de caer.")
            print("  -> (Si este numero es alto, el TP podria estar muy lejos o se necesita un trailing stop anticipado).")
    
    if tp_ops:
        tp_maes = [op["mae_pct"] for op in tp_ops if "mae_pct" in op]
        if tp_maes:
            avg_mae_tp = sum(tp_maes) / len(tp_maes)
            print(f"Para los {len(tp_ops)} TP tocados ({len(tp_maes)} con datos validos), el precio cayo en promedio {avg_mae_tp:.2f}% antes de subir al TP.")
            print("  -> (Advertencia: apretar mucho el SL podria cortarte estas operaciones ganadoras prematuramente).")

    # ---------------------------------------------------------
    # ANALISIS POR COMBINED_SIGNAL (CUANTITATIVO)
    # ---------------------------------------------------------
    print("\n" + "="*50)
    print("PnL POR NIVEL DE 'COMBINED_SIGNAL' (Puntaje Cuantitativo)")
    print("="*50)
    
    buckets_quant = {"0-10 (Bajo)": [], "10-15 (Medio)": [], "15-20 (Alto)": [], "20+ (Muy Alto)": []}
    for op in resueltas: # Analizamos todas las resueltas para ver la validez de la senal pura
        cs = op.get("combined_signal", 0)
        if cs < 10: buckets_quant["0-10 (Bajo)"].append(op)
        elif cs < 15: buckets_quant["10-15 (Medio)"].append(op)
        elif cs < 20: buckets_quant["15-20 (Alto)"].append(op)
        else: buckets_quant["20+ (Muy Alto)"].append(op)
        
    for name, ops in buckets_quant.items():
        if not ops: continue
        avg_pnl = sum(op.get("pnl_neto_pct", 0) for op in ops) / len(ops)
        print(f"Rango {name:15}: {len(ops):>4} ops | PnL Promedio: {avg_pnl:+.4f}%")
    print("-> Si el PnL no mejora en los rangos altos, la formula cuantitativa no aporta ventaja por si sola.")

    # ---------------------------------------------------------
    # ANALISIS POR RISK_SCORE (GEMINI IA)
    # ---------------------------------------------------------
    print("\n" + "="*50)
    print("PnL POR 'RISK_SCORE' DE GEMINI (En todas las evaluadas)")
    print("="*50)
    
    buckets_risk = {"0-3 (Bajo Riesgo)": [], "4-6 (Medio Riesgo)": [], "7-10 (Alto Riesgo)": []}
    for op in resueltas:
        rs = op.get("risk_score", 10) # 10 por defecto si no lo puso
        if rs <= 3: buckets_risk["0-3 (Bajo Riesgo)"].append(op)
        elif rs <= 6: buckets_risk["4-6 (Medio Riesgo)"].append(op)
        else: buckets_risk["7-10 (Alto Riesgo)"].append(op)
        
    for name, ops in buckets_risk.items():
        if not ops: continue
        avg_pnl = sum(op.get("pnl_neto_pct", 0) for op in ops) / len(ops)
        print(f"Rango {name:18}: {len(ops):>4} ops | PnL Promedio: {avg_pnl:+.4f}%")
    print("-> Si el riesgo evaluado por IA no correlaciona con mejor PnL, la puntuacion de 1 a 10 es solo ruido algoritmico.")
    
    print("\n[NOTA] Recuerda correr este script SOLO cuando tengas mas de ~100 operaciones aprobadas.")

if __name__ == "__main__":
    filepath = "logs/sombra.json"
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            filepath = arg
    run_analysis(filepath)
