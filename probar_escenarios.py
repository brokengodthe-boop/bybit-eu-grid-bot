"""
Manda los 3 escenarios sintéticos (escenarios_sinteticos.py) a TU gemini_client.py
real y muestra el veredicto completo. Corre esto TÚ, en tu máquina, donde están
las credenciales de Gemini — desde aquí no puedo llamar a la API.

Uso (desde la raíz del proyecto del bot, para que los imports de src/ funcionen):
    python probar_escenarios.py

Qué prueba:
- Escenario A ("de libro"): cumple textualmente el checklist de aprobación del
  prompt. Si esto NUNCA da APPROVED en varios intentos, el problema es
  estructural en el prompt (el checklist es, en la práctica, irrealizable),
  no que el mercado real haya sido malo estos días.
- Escenario B (control negativo): caída vertical sin ninguna vela de
  recuperación. Debe seguir dando VETO. Si esto diera APPROVED, el problema
  sería más grave (el filtro no protege ni en el caso obvio).
- Escenario C (intermedio): rebote plausible pero con volumen plano, no
  creciente. Sirve para ver dónde traza la línea el modelo entre B y A.

IMPORTANTE sobre validez: los datos son 100% sintéticos, así que esto prueba
el PROMPT (¿puede aprobar algo, alguna vez, ante evidencia favorable?), no la
ESTRATEGIA (eso solo lo dice el modo sombra con mercado real). Son preguntas
distintas y este script solo responde la primera.
"""
import asyncio

from escenarios_sinteticos import ESCENARIOS

# Ajusta este import a como se llama de verdad tu cliente. Ejemplos típicos:
#   from src.gemini_client import GeminiAgentClient
try:
    from src.gemini_client import GeminiAgentClient
except ImportError as e:
    raise SystemExit(
        "No se pudo importar GeminiAgentClient desde src.gemini_client. "
        "Ajusta la línea de import de este script al nombre real de tu clase/módulo.\n"
        f"Error original: {e}"
    )


async def correr_escenario(gemini, escenario_fn, repeticiones=1):
    e = escenario_fn()
    print(f"\n{'=' * 70}\n{e['nombre']}  ({e['symbol']})")
    print(f"precio={e['price']:.4f}  cambio_1h={e['change_1h']:+.2f}%  "
          f"cambio_24h={e['change_24h']:+.2f}%  volumen=${e['volume']:,.0f}")

    for intento in range(1, repeticiones + 1):
        etiqueta = f" (intento {intento}/{repeticiones})" if repeticiones > 1 else ""
        try:
            res = await gemini.evaluate_market_signal(
                symbol=e["symbol"], price=e["price"], change_1h=e["change_1h"],
                change_24h=e["change_24h"], volume=e["volume"],
                klines_data=e["klines_15m"], klines_1h_data=e["klines_1h"])
        except Exception as ex:
            print(f"  [!] Error llamando a Gemini{etiqueta}: {ex}")
            continue

        veredicto = res.get("verdict", "?")
        allow = res.get("allow_trade")
        risk = res.get("risk_score", "?")
        narrativa = res.get("narrative", "(sin narrativa)")
        marca = "✅" if allow else "🛑"
        print(f"\n  {marca} Veredicto{etiqueta}: {veredicto} | allow_trade={allow} | risk_score={risk}")
        print(f"     tp_pct={res.get('tp_pct')} sl_pct={res.get('sl_pct')} "
              f"dca_levels={res.get('dca_levels')}")
        print(f"     Narrativa: {narrativa}")


async def main():
    gemini = GeminiAgentClient()
    # Repite el escenario A varias veces: los LLM no son 100% deterministas incluso
    # con temperatura baja. Un solo intento no basta para decir "nunca aprueba".
    await correr_escenario(gemini, ESCENARIOS[0], repeticiones=3)  # de libro
    await correr_escenario(gemini, ESCENARIOS[1], repeticiones=1)  # control negativo
    await correr_escenario(gemini, ESCENARIOS[2], repeticiones=1)  # intermedio
    await correr_escenario(gemini, ESCENARIOS[3], repeticiones=1)  # intermedio malo

    print(f"\n{'=' * 70}")
    print("Lectura:")
    print("- Si el escenario A NUNCA aprobó en sus 3 intentos: el checklist del prompt")
    print("  es, en la práctica, irrealizable. Hay que suavizar los criterios, no esperar")
    print("  más al mercado.")
    print("- Si A aprobó al menos una vez: el prompt SÍ puede aprobar ante evidencia")
    print("  favorable clara. El 0/246 real puede deberse a que el mercado de estos días")
    print("  no ofreció nada tan limpio como el escenario A (posible, pero revisa el")
    print("  Diagnóstico de vez en cuando con nuevas señales reales).")
    print("- B debe seguir dando VETO siempre. Si no, hay un problema más grave que el")
    print("  de estar 'demasiado estricto'.")


if __name__ == "__main__":
    asyncio.run(main())
