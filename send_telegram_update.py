import asyncio
from src.telegram_alerts import TelegramNotifier

async def main():
    telegram = TelegramNotifier()
    msg = (
        "✅ <b>ACTUALIZACIÓN DEL SISTEMA</b>\n\n"
        "El sistema está operando de forma impecable y protegiendo el capital, pero nos topamos con un candado técnico de Bybit:\n\n"
        "<b>El detalle de ALGO:</b>\n"
        "Compramos exactamente $5.0 USD en ALGO. Tras el retroceso y tocar Stop Loss, el valor cayó a ~$4.86. Al intentar vender, Bybit rechazó la orden porque su mínimo absoluto para operar es de $5.0. ALGO sigue en tu cuenta (ahora vale $4.96).\n\n"
        "<b>Solución Definitiva (Ya Aplicada):</b>\n"
        "He modificado el bot para que la inversión mínima por operación sea de <b>$5.5 USDC</b>. Así, si toca Stop Loss, el remanente rondará los $5.30 y podrá venderse sin problema.\n\n"
        "<b>Acción Requerida:</b>\n"
        "Como ALGO congeló $4.96 y tienes 1.12 USDC libres, el bot está en pausa por falta de saldo operativo. Ve a Bybit y usa <i>'Convertir pequeños saldos a MNT'</i> (y luego véndelos por USDC), o deposita $5 para destrabar la cuenta.\n\n"
        "---\n"
        "🛠️ <b>COMANDOS DE SEGUIMIENTO</b>\n"
        "Puedes escribirme cualquiera de estos comandos aquí en Telegram para ver el estado:\n"
        "📊 <b>/status</b> - Ver saldos, capital total y estado del bot.\n"
        "📈 <b>/pnl</b> - Ver el rendimiento acumulado (PnL) y operaciones realizadas.\n"
        "📅 <b>/resumen</b> - Generar un reporte del día.\n"
        "🔍 <b>/analizar [TOKEN]</b> - Solicitar análisis IA de un activo.\n"
        "📌 <b>/last</b> - Ver qué está analizando el bot ahora mismo.\n"
        "⏸️ <b>/pause</b> - Pausar operaciones temporales.\n"
        "▶️ <b>/resume</b> - Reanudar el bot."
    )
    await telegram.send_message(msg)

asyncio.run(main())
