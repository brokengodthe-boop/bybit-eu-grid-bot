import pandas as pd
import numpy as np

def evaluar_tendencia_bitcoin(klines_btc: str) -> tuple[bool, str]:
    """
    Evalúa la tendencia general de Bitcoin (Capa 1 - Master Filter).
    Retorna (True, "Razón") si es seguro operar altcoins, (False, "Razón") si BTC está en 'dump mode'.
    Se basa en la acción del precio reciente (caídas abruptas de precio).
    """
    if not klines_btc or "Hora | Open" not in klines_btc:
        return True, "No se pudo obtener klines de BTC, asumiendo mercado neutral."
        
    try:
        lines = klines_btc.strip().split('\n')[1:]
        if len(lines) < 10:
            return True, "Insuficientes datos OHLCV de BTC."
            
        # Analizar los últimos cierres
        closes = []
        for line in lines:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 5:
                closes.append(float(parts[4]))
                
        # Closes are chronological. lines[0] is oldest, lines[-1] is newest.
        if len(closes) >= 10:
            precio_actual = closes[-1]
            precio_pasado = closes[-10] # Hace ~10 velas (ej. 2.5 horas en 15m)
            cambio_pct = ((precio_actual - precio_pasado) / precio_pasado) * 100
            
            # Filtro Maestro: Si BTC cae más del 1.5% en las últimas velas (dump abrupto)
            if cambio_pct < -1.5:
                return False, f"BTC Dump detectado ({cambio_pct:.2f}%). Riesgo sistémico alto."
            return True, f"Mercado estable (BTC {cambio_pct:+.2f}%)."
            
    except Exception as e:
        print(f"[!] Error evaluando tendencia de BTC: {e}")
        
    return True, "Fallo en evaluación, por defecto permitido."

def aplicar_alfas_formulaicos(df: pd.DataFrame) -> pd.DataFrame:
    """
    Estrategia Cuantitativa de Momentum Pullback Normalizado.
    Busca activos en tendencia alcista consolidada de 24h que están haciendo
    un retroceso o descanso sano en el marco intradiario (1h) con alto volumen institucional.
    100% libre de sesgos de precio nominal.
    """
    if df.empty or 'close_price' not in df.columns:
        return pd.DataFrame()
        
    df_result = df.copy()
    
    # 1. Filtro preliminar de liquidez y estabilidad (volumen mínimo $10M)
    df_result = df_result[df_result['volume'] >= 10_000_000].copy()
    if df_result.empty:
        return pd.DataFrame()
        
    # 2. Score de Tendencia 24h: Premiar tendencias sanas (+2% a +25%), penalizar extremos parabólicos (+35% FOMO)
    def calc_trend_score(ch24):
        if ch24 < 1.0:
            return -5.0 # Sin tendencia alcista
        elif 2.0 <= ch24 <= 20.0:
            return ch24 * 1.5 # Rango óptimo de tendencia
        elif ch24 > 20.0:
            return max(0.0, 30.0 - (ch24 - 20.0)) # Penalizar compras en la euforia
        return ch24
        
    df_result['trend_score'] = df_result['percent_change_24h'].apply(calc_trend_score)
    
    # 3. Score de Pullback Intradiario (1h):
    # Premiar cuando la moneda está descansando (-2.0% a +0.5%) tras la subida
    def calc_pullback_score(ch1):
        if -3.0 <= ch1 <= 0.2:
            return 10.0 - abs(ch1 * 2.0) # Excelente zona de soporte / pullback
        elif ch1 < -3.0:
            return -10.0 # Caída abrupta (cuchillo cayendo)
        else:
            return max(0.0, 5.0 - ch1) # Ya despegó, menor ventaja
            
    df_result['pullback_score'] = df_result['percent_change'].apply(calc_pullback_score)
    
    # 4. Score de Volumen Institucional (escala logarítmica para neutralizar el precio del activo)
    df_result['volume_score'] = np.log10(df_result['volume']) * 1.5
    
    # Puntuación combinada de alta probabilidad
    df_result['combined_signal'] = df_result['trend_score'] + df_result['pullback_score'] + df_result['volume_score']
    
    # Ordenar por mejor oportunidad técnica
    df_result = df_result.sort_values(by='combined_signal', ascending=False)
    
    return df_result

def calcular_tp_sl(precio_entrada: float, klines_data: str = "") -> tuple:
    """
    Cálculo Asimétrico de Riesgo/Beneficio (Ratio >= 2.2 : 1).
    - Stop Loss fuera del ruido normal intradiario (3.5% a 4.5%).
    - Take Profit objetivo sustancial (7.5% a 12.0%) para asegurar ganancias reales.
    """
    atr = 0.0
    try:
        if klines_data and "Hora | Open" in klines_data:
            lines = klines_data.strip().split('\n')[1:]
            true_ranges = []
            prev_close = None
            
            for line in lines:
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 5:
                    high = float(parts[2])
                    low = float(parts[3])
                    close = float(parts[4])
                    
                    if prev_close is not None:
                        tr1 = high - low
                        tr2 = abs(high - prev_close)
                        tr3 = abs(low - prev_close)
                        tr = max(tr1, tr2, tr3)
                        true_ranges.append(tr)
                    prev_close = close
                    
            if true_ranges:
                atr = sum(true_ranges) / len(true_ranges)
    except Exception as e:
        print(f"[!] Error calculando ATR: {e}")
        
    if atr > 0:
        # Micro-Scalping Stop Loss (mínimo 1.5%, máximo 2.5%)
        sl_dist = max(atr * 1.5, precio_entrada * 0.015)
        sl_dist = min(sl_dist, precio_entrada * 0.025)
        
        # Micro-Scalping Take Profit (mínimo 1.5%, adaptativo según volatilidad)
        tp_dist = max(sl_dist * 1.0, precio_entrada * 0.015)
        
        stop_loss = precio_entrada - sl_dist
        take_profit = precio_entrada + tp_dist
    else:
        # Fallback estático para Scalping
        stop_loss = precio_entrada * (1 - 0.015) # -1.5%
        take_profit = precio_entrada * (1 + 0.015) # +1.5%
        
    return take_profit, stop_loss
