import asyncio
import json
import os
import sys
from datetime import datetime

# Añadir el directorio raíz al path para importar módulos correctamente
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.validator import validate_environment
from src.cmc_client import CMCClient
from src.grvt_client import GRVTClient
from src.gemini_client import GeminiAgentClient
from src.telegram_alerts import TelegramNotifier
from estrategias.logica_anomalias import aplicar_alfas_formulaicos, calcular_tp_sl

async def ejecutar_ciclo_simulación():
    print("=" * 60)
    print("INICIANDO EJECUCIÓN DE UN CICLO DE SIMULACIÓN EN VIVO")
    print("=" * 60)
    
    # 0. Validar Entorno
    validate_environment()
    cmc = CMCClient()
    grvt = GRVTClient()
    gemini = GeminiAgentClient()
    telegram = TelegramNotifier()
    
    timestamp_actual = datetime.now().isoformat()
    
    # 1. Circuit Breaker
    print("\n[Paso 1/6] Verificando Circuit Breaker (Margin Ratio)...")
    margin_ratio = await grvt.get_margin_ratio()
    print(f" -> Margin Ratio actual: {margin_ratio*100:.2f}% (Límite máximo: 40%)")
    if margin_ratio > 0.40:
        print(f"[ALERTA CRÍTICA] Margin Ratio excede el 40%. Abortando ciclo.")
        await telegram.notify_circuit_breaker(margin_ratio)
        return

    # 2. Ingesta de Mercado en Vivo (CoinMarketCap)
    print("\n[Paso 2/6] Ingestando datos de mercado en vivo desde CoinMarketCap (Top 100)...")
    df_mercado = await cmc.get_gainers_losers(limit=100)
    if df_mercado.empty:
        print("[ERROR] No se obtuvieron datos de mercado.")
        return
        
    print(f" -> Procesados {len(df_mercado)} activos cripto sin stablecoins.")

    # 3. Razonamiento Cuantitativo (Alfas Formulaicos DeepSeek-R1)
    print("\n[Paso 3/6] Aplicando Alfas Formulaicos de Reversión a la Media (DeepSeek-R1)...")
    df_analizado = aplicar_alfas_formulaicos(df_mercado)
    mejor_activo = df_analizado.iloc[0]
    symbol = mejor_activo['symbol']
    precio = float(mejor_activo['close_price'])
    cambio_1h = float(mejor_activo.get('percent_change', 0.0))
    cambio_24h = float(mejor_activo.get('percent_change_24h', 0.0))
    volumen = float(mejor_activo.get('volume', 0.0))
    score_alfa = float(mejor_activo.get('alfa_score', 0.0))
    
    print(f" -> Oportunidad Cuantitativa Identificada: {symbol}")
    print(f"    • Precio actual: ${precio:.6f}")
    print(f"    • Variación 1h: {cambio_1h:+.2f}% | Variación 24h: {cambio_24h:+.2f}%")
    print(f"    • Volumen 24h: ${volumen:,.0f} USD")
    print(f"    • Score Alfa (DeepSeek): {score_alfa:.4f}")

    # 4. Evaluador Cognitivo IA (Gemini AI Flash)
    print("\n[Paso 4/6] Evaluando señal con Agente Cognitivo de IA (Gemini Flash)...")
    evaluacion_ai = await gemini.evaluate_market_signal(
        symbol=symbol,
        price=precio,
        change_1h=cambio_1h,
        change_24h=cambio_24h,
        volume=volumen
    )
    
    veredicto = evaluacion_ai.get("verdict", "APPROVED")
    narrativa = evaluacion_ai.get("narrative", "Validado cuantitativamente.")
    risk_score = evaluacion_ai.get("risk_score", 3)
    allow_trade = evaluacion_ai.get("allow_trade", True)
    
    print(f" -> Veredicto IA: {veredicto} | Score de Riesgo: {risk_score}/10")
    print(f" -> Explicación Narrativa: {narrativa}")
    
    if not allow_trade:
        print(f"[VETO DE IA] La operación en {symbol} ha sido denegada por seguridad.")
        return

    # 5. Ejecución en GRVT (Paper Trading / Simulación)
    print("\n[Paso 5/6] Ejecutando orden de simulación en GRVT (Isolated Margin 5x, Post-Only)...")
    await grvt.set_isolated_margin(symbol, leverage=5)
    await grvt.send_post_only_limit_order(symbol, price=precio, min_notional=100.0)
    
    tp, sl = calcular_tp_sl(precio)
    print(f" -> Niveles de Riesgo Paramétrico Calculados:")
    print(f"    • Take Profit (+6%): ${tp:.6f}")
    print(f"    • Stop Loss (-2%):   ${sl:.6f}")

    # 6. Alertas Telegram & Registro de Auditoría
    print("\n[Paso 6/6] Enviando alerta a Telegram y guardando registro persistente...")
    try:
        await telegram.notify_trade(
            symbol=symbol,
            price=precio,
            change_1h=cambio_1h,
            change_24h=cambio_24h,
            tp=tp,
            sl=sl,
            ai_verdict=veredicto,
            ai_narrative=narrativa
        )
        print(" -> Alerta enviada exitosamente a Telegram.")
    except Exception as e:
        print(f" -> Aviso Telegram: {e}")

    # Persistencia
    registro = {
        "timestamp": timestamp_actual,
        "symbol": symbol,
        "entry_price": precio,
        "percent_change_1h": cambio_1h,
        "percent_change_24h": cambio_24h,
        "margin_ratio": margin_ratio,
        "alfa_score": score_alfa,
        "take_profit": tp,
        "stop_loss": sl,
        "ai_verdict": veredicto,
        "ai_risk_score": risk_score,
        "ai_narrative": narrativa,
        "unrealized_pnl": 0.0
    }
    
    os.makedirs("logs", exist_ok=True)
    archivo_auditoria = "logs/auditoria.json"
    try:
        with open(archivo_auditoria, "r", encoding="utf-8") as f:
            auditoria = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        auditoria = []
        
    auditoria.append(registro)
    with open(archivo_auditoria, "w", encoding="utf-8") as f:
        json.dump(auditoria, f, indent=4, ensure_ascii=False)
        
    print(f" -> Registro guardado en '{archivo_auditoria}'. Total entradas en auditoría: {len(auditoria)}")
    print("\n" + "=" * 60)
    print("CICLO DE SIMULACIÓN COMPLETADO CON ÉXITO")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(ejecutar_ciclo_simulación())
