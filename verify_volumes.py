import json
from pybit.unified_trading import HTTP
import sys

# Necesitamos las lineas de log para extraer la narrativa
with open(r'C:\Users\estre\.gemini\antigravity-ide\brain\cdb64dd9-dbab-4f14-a711-ed6185774349\.system_generated\tasks\task-3105.log', 'r', encoding='utf-8') as f:
    log_lines = f.readlines()

def get_narrative(symbol):
    # Buscar "[!] VETO: operación en {symbol} cancelada" y sacar la línea anterior
    for i, line in enumerate(log_lines):
        if f"operación en {symbol} cancelada" in line:
            if i > 0 and "[*] IA: VETO" in log_lines[i-1]:
                return log_lines[i-1].strip()
    return "Narrativa no encontrada"

try:
    with open('logs/sombra.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    v3 = [x for x in data if x.get('version') == 'v3.3_shadow_cot']
    
    # Excluir símbolos que sabemos que fallan en la API por alguna razón
    samples = [x for x in v3 if x['symbol'] not in ['PENGUUSDC']][-3:]
    
    session = HTTP(testnet=False)
    
    for s in samples:
        symbol = s['symbol']
        ts_ms = s['ts_ms']
        
        narrative = get_narrative(symbol)
                
        res = session.get_kline(
            category="spot",
            symbol=symbol,
            interval="15",
            end=ts_ms,
            limit=4
        )
        
        klines = res.get("result", {}).get("list", [])
        volumes = [float(k[5]) for k in klines]
        
        print(f"--- Análisis de {symbol} ---")
        print(f"Narrativa de Gemini: {narrative}")
        print(f"Volúmenes reales de las últimas 4 velas de 15m (Vela 0 es la más reciente):")
        for i, vol in enumerate(volumes):
            print(f"Vela -{i}: {vol:.2f}")
        
        print("")

except Exception as e:
    print(f"Error: {e}".encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding))
