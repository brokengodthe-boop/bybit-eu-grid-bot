"""
Verifica, con datos duros, si las narrativas de Gemini que dicen "volumen
decreciente" corresponden a volumen realmente decreciente en las velas de
15m alrededor del momento de la señal.

No cambia nada del bot: solo lee logs/sombra.json y consulta klines
históricos de Bybit (de solo lectura) para el símbolo y el momento exactos
de cada señal.

Uso:
    python verificar_volumen.py [ruta_a_sombra.json] [n_muestras]

Qué hace:
1. Filtra vetos cuya narrativa menciona volumen y toma una muestra al azar
   (no las primeras N: eso sesgaría hacia un solo tramo de tiempo).
2. Para cada una, pide a Bybit las velas de 15m que Gemini tenía delante en
   ese momento (mismo intervalo y cantidad que usa main.py: limit=20).
3. Calcula un veredicto objetivo simple: pendiente del volumen en las
   últimas velas (regresión lineal básica, sin librerías extra) y si las
   últimas 3 velas promedian menos volumen que las 3 anteriores.
4. Muestra la narrativa junto al veredicto para que la compares tú mismo.

Esto NO prueba que Gemini "mienta": una racha corta de volumen puede leerse
de más de una forma razonable. Sirve para detectar el caso más grave: que
diga "decreciente" cuando los números muestran claramente lo contrario.
"""
import json
import random
import re
import sys

PALABRAS_VOLUMEN = re.compile(r"volumen|volume", re.IGNORECASE)


def pendiente(valores: list) -> float:
    """Pendiente de una regresión lineal simple (sin numpy)."""
    n = len(valores)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(valores) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, valores))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def veredicto_volumen(velas: list) -> str:
    """velas: [[ts, o, h, l, c, vol], ...] cronológicas. Devuelve un veredicto legible."""
    vols = [v[5] for v in velas]
    if len(vols) < 6:
        return "datos insuficientes"
    m = pendiente(vols)
    prom_total = sum(vols) / len(vols)
    pend_relativa = (m / prom_total * 100) if prom_total else 0.0

    ultimas3 = sum(vols[-3:]) / 3
    previas3 = sum(vols[-6:-3]) / 3
    caida_reciente = (ultimas3 / previas3 - 1) * 100 if previas3 else 0.0

    if pend_relativa < -3 and caida_reciente < -10:
        return f"DECRECIENTE claro (pendiente {pend_relativa:+.1f}%/vela, últimas 3 vs previas 3: {caida_reciente:+.1f}%)"
    if pend_relativa > 3 and caida_reciente > 10:
        return f"CRECIENTE claro (pendiente {pend_relativa:+.1f}%/vela, últimas 3 vs previas 3: {caida_reciente:+.1f}%)"
    return f"MIXTO / sin tendencia clara (pendiente {pend_relativa:+.1f}%/vela, últimas 3 vs previas 3: {caida_reciente:+.1f}%)"


def obtener_muestra(regs: list, n: int, semilla=None) -> list:
    candidatos = [
        r for r in regs
        if r.get("narrative")
        and PALABRAS_VOLUMEN.search(r["narrative"])
        and r.get("ts_ms") and r.get("symbol")
    ]
    if not candidatos:
        return []
    rnd = random.Random(semilla)
    return rnd.sample(candidatos, min(n, len(candidatos)))


def main():
    ruta = sys.argv[1] if len(sys.argv) > 1 else "logs/sombra.json"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    with open(ruta, "r", encoding="utf-8") as f:
        regs = json.load(f)

    muestra = obtener_muestra(regs, n)
    if not muestra:
        print("No hay vetos con mención a volumen y datos suficientes (ts_ms/symbol) para verificar.")
        return

    from src.bybit_client import BybitClient, BybitError
    bybit = BybitClient(testnet=False)

    print(f"Verificando {len(muestra)} vetos por 'volumen', elegidos al azar de {len(regs)} registros.\n")
    coincide, no_coincide, mixto = 0, 0, 0

    for r in muestra:
        symbol = r["symbol"]
        ts_ms = r.get("ts_ms_eval", r.get("ts_ms"))
        try:
            # Mismas velas de 15m que main.py le pasó a Gemini: 20 velas terminando en el momento de la señal
            velas = bybit.get_klines_raw(symbol, interval="15", end_ms=ts_ms, limit=20)
            velas.sort(key=lambda x: x[0]) # Asegurar orden cronológico estricto
        except BybitError as e:
            print(f"[!] No se pudieron obtener velas de {symbol}: {e}\n")
            continue

        v = veredicto_volumen(velas)
        dice_decreciente = bool(re.search(r"decrecient|nulo|d[eé]bil|falta de volumen|sin volumen", r["narrative"], re.I))
        dice_creciente = bool(re.search(r"crecient|alto volumen|volumen fuerte|respald", r["narrative"], re.I))

        if dice_decreciente and "DECRECIENTE" in v:
            marca, coincide = "✅ coincide", coincide + 1
        elif dice_creciente and "CRECIENTE" in v:
            marca, coincide = "✅ coincide", coincide + 1
        elif "MIXTO" in v:
            marca, mixto = "➖ mixto (no concluyente)", mixto + 1
        else:
            marca, no_coincide = "❌ NO coincide", no_coincide + 1

        print(f"--- {symbol} | {r.get('timestamp', '')} | {marca} ---")
        print(f"  Narrativa de Gemini: {r['narrative']}")
        print(f"  Volumen real (15m):  {v}")
        print()

    total = coincide + no_coincide + mixto
    if total:
        print(f"Resumen: {coincide}/{total} coinciden, {no_coincide}/{total} NO coinciden, "
              f"{mixto}/{total} mixtos/no concluyentes.")
        if no_coincide / total > 0.3:
            print("-> Bastantes desajustes: revisa esos casos a mano antes de confiar en la narrativa como prueba.")
        elif coincide / total >= 0.7:
            print("-> La mayoría coincide con los datos reales: buena señal de que la narrativa refleja "
                  "algo real, no solo una frase hecha.")


if __name__ == "__main__":
    main()
