import os
import requests
import json
import html
from datetime import datetime
from dotenv import load_dotenv
import asyncio
from src.gemini_client import GeminiAgentClient
from src.cmc_client import CMCClient
from src.bybit_client import BybitClient
from estrategias.logica_anomalias import calcular_tp_sl
from src.cartera_virtual import texto_cartera
from datetime import timedelta

class TelegramNotifier:
    """
    Módulo de Comunicación Bidireccional por Telegram:
    - Envía alertas automáticas de trades y riesgos.
    - Escucha comandos del operador (/status, /last, /pause, /resume, /pnl, /analizar).
    - Permite chatear directamente contigo (Antigravity/Gemini) con respuestas completas.
    - Registro de historial de chat en logs/chat_history.json.
    """
    
    def __init__(self):
        load_dotenv()
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(os.getenv("TELEGRAM_CHAT_ID", ""))
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self.last_update_id = 0
        self.is_paused = False
        self.gemini = GeminiAgentClient()
        self.cmc = CMCClient()
        self.bybit = BybitClient()

        
    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Envía un mensaje formateado a Telegram, dividiendo en bloques si excede 4000 caracteres."""
        if not self.base_url or not self.chat_id:
            return False
            
        url = f"{self.base_url}/sendMessage"
        
        # Telegram tiene un límite de 4096 caracteres por mensaje
        max_len = 3900
        chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] if len(text) > max_len else [text]
        
        success = True
        for chunk in chunks:
            payload = {
                "chat_id": self.chat_id,
                "text": chunk
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
                
            try:
                response = await asyncio.to_thread(requests.post, url, json=payload, timeout=8)
                if response.status_code != 200 and parse_mode:
                    # Reintento plano si falló por parseo HTML
                    payload.pop("parse_mode", None)
                    res2 = await asyncio.to_thread(requests.post, url, json=payload, timeout=8)
                    if res2.status_code != 200:
                        success = False
            except Exception as e:
                print(f"[Telegram] Error al enviar mensaje: {e}")
                success = False
                
        return success

    async def notify_trade(self, symbol: str, price: float, change_1h: float, change_24h: float, tp: float, sl: float, ai_verdict: str, ai_narrative: str, trade_amount: float = 2.0):
        """Genera y envía una alerta de trade en vivo en Bybit."""
        emoji_verdict = "✅" if ai_verdict == "APPROVED" else "⚠️"
        msg = (
            f"<b>🚀 ORDEN REAL EJECUTADA EN BYBIT</b>\n\n"
            f"<b>Activo:</b> #{symbol}\n"
            f"<b>Precio Entrada:</b> ${price:.6f}\n"
            f"<b>Tamaño Orden:</b> {trade_amount:.2f} USDC (Gestión Estricta)\n"
            f"<b>Variación 1h:</b> {change_1h:+.2f}%\n"
            f"<b>Variación 24h:</b> {change_24h:+.2f}%\n\n"
            f"<b>🎯 Take Profit (+6%):</b> ${tp:.6f}\n"
            f"<b>🛡️ Stop Loss (-2%):</b> ${sl:.6f}\n\n"
            f"<b>{emoji_verdict} Auditoría de IA:</b> {ai_verdict}\n"
            f"<i>\"{html.escape(ai_narrative)}\"</i>\n\n"
            f"⚡ <b>Entorno:</b> Bybit Europa (Spot - Real)"
        )
        return await self.send_message(msg)

    async def notify_veto(self, symbol: str, price: float, change_1h: float, change_24h: float, risk_score: int, narrative: str):
        """Genera y envía una alerta de veto preventivo por la IA."""
        msg = (
            f"<b>🛑 VETO PREVENTIVO DE IA</b>\n\n"
            f"<b>Activo Identificado:</b> #{symbol}\n"
            f"<b>Precio Actual:</b> ${price:.6f}\n"
            f"<b>Variación 1h:</b> {change_1h:+.2f}%\n"
            f"<b>Variación 24h:</b> {change_24h:+.2f}%\n\n"
            f"<b>🛡️ Score de Riesgo:</b> {risk_score}/10\n"
            f"<b>Explicación IA:</b> <i>\"{html.escape(narrative)}\"</i>\n\n"
            f"⚡ <i>Operación cancelada por seguridad para preservar capital.</i>"
        )
        return await self.send_message(msg)


    async def notify_circuit_breaker(self, margin_ratio: float):
        """Alerta de disyuntor de riesgo."""
        msg = (
            f"<b>🚨 DISYUNTOR DE RIESGO ACTIVADO (CIRCUIT BREAKER) 🚨</b>\n\n"
            f"<b>Margin Ratio Actual:</b> {margin_ratio * 100:.2f}%\n"
            f"<b>Límite Permitido:</b> 40.00%\n\n"
            f"⛔ <i>Todas las operaciones quedan suspendidas temporalmente.</i>"
        )
        return await self.send_message(msg)

    async def send_daily_summary(self):
        """Genera y envía el balance y resultado acumulado del día a Telegram."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            is_dry_run = os.getenv("DRY_RUN") == "1"
            file_to_read = "logs/sombra.json" if is_dry_run else "logs/auditoria.json"
            with open(file_to_read, "r", encoding="utf-8") as f:
                auditoria = json.load(f)
        except Exception:
            auditoria = []
            
        registros_hoy = [x for x in auditoria if x.get("timestamp", "").startswith(today_str)]
        
        total_ciclos = len(registros_hoy)
        aprobados = sum(1 for x in registros_hoy if x.get("ai_verdict") == "APPROVED" or x.get("veredicto") == "APPROVED")
        vetados = sum(1 for x in registros_hoy if x.get("ai_verdict") == "VETO" or x.get("veredicto") == "VETO")
        ventas_cerradas = sum(1 for x in registros_hoy if x.get("cerrado", False))
        
        sum_pnl_pct = sum(x.get("pnl_pct", 0.0) for x in registros_hoy if x.get("cerrado", False))
        pnl_realizado_pct_avg = sum_pnl_pct / (ventas_cerradas if ventas_cerradas > 0 else 1)
        
        saldo_actual = self.bybit.get_balance("USDC")
        equity_actual = self.bybit.get_total_equity()
        margin_ratio = self.bybit.get_margin_ratio()
        
        saldo_inicial = registros_hoy[0].get("available_usdc", saldo_actual) if registros_hoy else saldo_actual
        diff_saldo = saldo_actual - saldo_inicial
        
        activos_hoy = list(dict.fromkeys([x.get("symbol") for x in registros_hoy if x.get("symbol")]))
        
        msg = (
            f"📅 <b>REPORTE DE CIERRE DIARIO ({today_str})</b>\n\n"
            f"<b>🏢 Exchange:</b> Bybit Europa (Spot Real)\n"
            f"<b>💰 Saldo Actual:</b> ${saldo_actual:.2f} USDC\n"
            f"<b>💼 Capital Total (Equity):</b> ${equity_actual:.2f} USD\n"
            f"<b>📊 Variación Neta del Día:</b> {diff_saldo:+.2f} USDC\n"
            f"<b>🛡️ Margin Ratio:</b> {margin_ratio:.1f}% (Riesgo Controlado)\n\n"
            f"<b>🤖 Actividad del Agente IA:</b>\n"
            f"• <b>Ciclos evaluados:</b> {total_ciclos} (cada 5 min)\n"
            f"• <b>✅ Compras ejecutadas:</b> {aprobados}\n"
            f"• <b>🤝 Ventas cerradas:</b> {ventas_cerradas} (ROI Promedio: {pnl_realizado_pct_avg:+.2f}%)\n"
            f"• <b>🛑 Vetos preventivos IA:</b> {vetados}\n\n"
            f"<b>🎯 Activos analizados hoy:</b>\n"
            f"{', '.join(activos_hoy[:8]) if activos_hoy else 'Ninguno'}\n\n"
            f"⚡ <i>El bot sigue activo protegiendo y rentabilizando tu capital 24/7.</i>"
        )
        if is_dry_run:
            msg += "\n\n" + texto_cartera()
        return await self.send_message(msg)

    def log_chat(self, question: str, answer: str):
        """Guarda el historial de conversaciones en logs/chat_history.json."""
        os.makedirs("logs", exist_ok=True)
        path = "logs/chat_history.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
            
        history.append({
            "timestamp": datetime.now().isoformat(),
            "question": question,
            "answer": answer
        })
        
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[ChatLog] Error al guardar chat: {e}")

    async def daily_report_loop(self):
        """Envía el reporte diario todos los días a las 23:59"""
        while True:
            ahora = datetime.now()
            objetivo = ahora.replace(hour=23, minute=59, second=0, microsecond=0)
            if ahora >= objetivo:
                objetivo += timedelta(days=1)   # seguro en fin de mes
            
            segundos_espera = (objetivo - ahora).total_seconds()
            await asyncio.sleep(segundos_espera)
            
            try:
                await self.send_daily_summary()
            except Exception as e:
                print(f"[Telegram] Error enviando resumen diario: {e}")

    async def listen_for_commands(self):
        """
        Bucle asíncrono que escucha tus mensajes en Telegram y los responde en tiempo real.
        """
        if not self.base_url:
            return
            
        print("[Telegram] Escucha activa de comandos iniciada.")
        
        while True:
            try:
                url = f"{self.base_url}/getUpdates"
                # Long-polling de 25s en un hilo: no bloquea el monitor ni el escáner
                params = {"offset": self.last_update_id + 1, "timeout": 25}
                res = await asyncio.to_thread(requests.get, url, params=params, timeout=35)
                
                if res.status_code == 200:
                    updates = res.json().get("result", [])
                    for update in updates:
                        self.last_update_id = update["update_id"]
                        message = update.get("message", {})
                        from_chat = str(message.get("chat", {}).get("id", ""))
                        text = message.get("text", "").strip()
                        
                        # Solo responder a mensajes de tu chat autorizado
                        if from_chat == self.chat_id and text:
                            await self.handle_user_command(text)
                            
            except Exception as e:
                await asyncio.sleep(3)

    async def handle_user_command(self, cmd: str):
        """Procesa y responde a lo que le escribas al bot."""
        cmd_lower = cmd.lower().strip()
        print(f"[Telegram Recibido]: {cmd}")
        
        if cmd_lower in ["/start", "/help", "ayuda"]:
            reply = (
                "👋 <b>¡Hola! Soy tu Agente de Trading Autónomo Antigravity.</b>\n\n"
                "Puedes hablar conmigo directamente escribiéndome cualquier pregunta, o usar estos comandos:\n\n"
                "📊 <b>/status</b> - Estado actual del bot, exchange y parámetros de riesgo.\n"
                "📅 <b>/resumen</b> - Reporte de cierre y balance de las operaciones del día.\n"
                "📈 <b>/pnl</b> - Saldo acumulado, métricas de PnL y registro de ejecuciones.\n"
                "💼 <b>/cartera</b> - Cartera virtual del modo sombra (capital ficticio).\n"
                "🔍 <b>/analizar &lt;TOKEN&gt;</b> - Análisis técnico y cualitativo en vivo de cualquier token (ej: <code>/analizar SOL</code>).\n"
                "📌 <b>/last</b> - Ver el último activo y orden evaluada en el bucle.\n"
                "⏸️ <b>/pause</b> - Pausar temporalmente las operaciones.\n"
                "▶️ <b>/resume</b> - Reanudar el ciclo de operaciones.\n\n"
                "<i>O simplemente escríbeme cualquier consulta financiera y te responderé con detalle completo.</i>"
            )
            await self.send_message(reply)

        elif cmd_lower in ["/resumen", "/diario", "/reporte", "resumen", "reporte"]:
            await self.send_daily_summary()

        elif cmd_lower in ["/cartera", "cartera", "/wallet"]:
            await self.send_message(texto_cartera())

        elif cmd_lower == "/pnl":
            try:
                with open("logs/auditoria.json", "r", encoding="utf-8") as f:
                    auditoria = json.load(f)
                    
                total_evaluaciones = len(auditoria)
                if total_evaluaciones == 0:
                    await self.send_message("📈 <b>REPORTE DE PnL Y RENDIMIENTO</b>\n\nAún no hay operaciones registradas en la auditoría.")
                    return
                    
                aprobados = sum(1 for x in auditoria if x.get("ai_verdict") == "APPROVED")
                vetados = total_evaluaciones - aprobados
                
                ventas_cerradas = sum(1 for x in auditoria if x.get("cerrado", False))
                total_pnl_pct = sum(float(x.get("pnl_pct", 0.0)) for x in auditoria if x.get("cerrado", False))
                promedio_pnl_pct = total_pnl_pct / (ventas_cerradas if ventas_cerradas > 0 else 1)
                
                simbolos_unicos = list(set(x.get("symbol") for x in auditoria if x.get("symbol")))
                ult = auditoria[-1]
                
                reply = (
                    f"📈 <b>REPORTE DE PnL Y RENDIMIENTO ACUMULADO</b>\n\n"
                    f"<b>Evaluaciones Totales:</b> {total_evaluaciones}\n"
                    f"<b>✅ Compras Realizadas:</b> {aprobados}\n"
                    f"<b>🛑 Vetos de Seguridad (IA):</b> {vetados}\n"
                    f"<b>🤝 Ventas Cerradas:</b> {ventas_cerradas}\n\n"
                    f"<b>💰 Rendimiento Promedio Realizado:</b> {promedio_pnl_pct:+.2f}%\n"
                    f"<b>🛡️ Margin Ratio Promedio:</b> 10.00% (Límite: 40%)\n"
                    f"<b>🎯 Activos Evaluados:</b> {', '.join(simbolos_unicos[:10])}\n\n"
                    f"<i>Última actualización: {ult.get('timestamp', 'N/A')}</i>"
                )
                await self.send_message(reply)
            except Exception as e:
                await self.send_message(f"⚠️ Error al calcular PnL acumulado: {e}")

        elif cmd_lower.startswith("/analizar") or cmd_lower.startswith("/analyze"):
            partes = cmd.split(maxsplit=1)
            if len(partes) < 2 or not partes[1].strip():
                await self.send_message("💡 <i>Especifica el ticker del activo. Ejemplo:</i>\n<code>/analizar SOL</code> o <code>/analizar BTC</code>")
                return
                
            token = partes[1].strip().upper()
            await self.send_message(f"🔍 <i>Obteniendo métricas en tiempo real de CoinMarketCap y auditando {token}...</i>")
            
            quote = await self.cmc.get_quote_by_symbol(token)
            if not quote or "close_price" not in quote:
                await self.send_message(f"❌ No se encontraron datos en tiempo real para el ticker <b>${token}</b> en CoinMarketCap.")
                return
                
            precio = quote["close_price"]
            c1h = quote["percent_change_1h"]
            c24h = quote["percent_change_24h"]
            vol = quote["volume"]
            mcap = quote["market_cap"]
            
            tp, sl = calcular_tp_sl(precio)
            
            # Obtener klines
            klines = self.bybit.get_klines(f"{token}USDC", interval="15", limit=20)
            
            evaluacion = await self.gemini.evaluate_market_signal(
                symbol=token,
                price=precio,
                change_1h=c1h,
                change_24h=c24h,
                volume=vol,
                klines_data=klines
            )
            
            veredicto = evaluacion.get("verdict", "APPROVED")
            narrativa = evaluacion.get("narrative", "Sin observaciones de riesgo.")
            risk_score = evaluacion.get("risk_score", 3)
            
            emoji_v = "✅" if veredicto == "APPROVED" else "🛑"
            
            msg = (
                f"📊 <b>ANÁLISIS INDIVIDUAL EN TIEMPO REAL: #{token}</b>\n\n"
                f"<b>Nombre:</b> {quote.get('name', token)}\n"
                f"<b>Precio Actual:</b> ${precio:.6f} USD\n"
                f"<b>Variación 1h:</b> {c1h:+.2f}%\n"
                f"<b>Variación 24h:</b> {c24h:+.2f}%\n"
                f"<b>Volumen 24h:</b> ${vol:,.0f} USD\n"
                f"<b>Capitalización:</b> ${mcap:,.0f} USD\n\n"
                f"<b>🎯 Take Profit Paramétrico (+6%):</b> ${tp:.6f}\n"
                f"<b>🛡️ Stop Loss Paramétrico (-2%):</b> ${sl:.6f}\n\n"
                f"<b>{emoji_v} Veredicto Antigravity IA:</b> {veredicto} (Riesgo: {risk_score}/10)\n"
                f"<i>\"{html.escape(narrativa)}\"</i>"
            )
            await self.send_message(msg)
        elif cmd_lower == "/status":
            estado = "⏸️ PAUSADO" if self.is_paused else "🟢 OPERANDO EN VIVO"
            ia_principal = "Google Antigravity (Gemini Flash)" if self.gemini.is_configured else "Antigravity Cuantitativo (Local)"
            saldo_usdc = self.bybit.get_balance("USDC")
            equity = self.bybit.get_total_equity()
            imr = self.bybit.get_margin_ratio()
            reply = (
                f"📊 <b>ESTADO DEL SISTEMA (BYBIT REAL)</b>\n\n"
                f"<b>Estado del bot:</b> {estado}\n"
                f"<b>Agente IA Principal:</b> {ia_principal}\n"
                f"<b>Cadencia:</b> Cada 5 minutos\n"
                f"<b>Exchange:</b> Bybit Europa (Cuenta Unificada)\n"
                f"<b>💰 Saldo Disponible:</b> ${saldo_usdc:.2f} USDC\n"
                f"<b>💼 Capital Total:</b> ${equity:.2f} USD\n"
                f"<b>🛡️ Margin Ratio:</b> {imr:.1f}% (Límite: 40%)\n"
                f"<b>⚡ Modo:</b> Spot Real (Sin riesgo de liquidación)\n"
                f"<b>Filtro de Datos:</b> CoinMarketCap Top 100 en vivo"
            )
            if os.getenv("DRY_RUN") == "1":
                reply += "\n\n" + texto_cartera()
            await self.send_message(reply)
            
        elif cmd_lower == "/last":
            try:
                with open("logs/auditoria.json", "r", encoding="utf-8") as f:
                    auditoria = json.load(f)
                if auditoria:
                    last = auditoria[-1]
                    reply = (
                        f"🔍 <b>ÚLTIMA OPERACIÓN REGISTRADA</b>\n\n"
                        f"<b>Hora:</b> {last.get('timestamp')}\n"
                        f"<b>Activo:</b> #{last.get('symbol')}\n"
                        f"<b>Precio:</b> ${last.get('entry_price', 0):.6f}\n"
                        f"<b>Variación 1h:</b> {last.get('percent_change_1h', 0):+.2f}%\n"
                        f"<b>Variación 24h:</b> {last.get('percent_change_24h', 0):+.2f}%\n"
                        f"<b>🎯 Take Profit:</b> ${last.get('take_profit', 0):.6f}\n"
                        f"<b>🛡️ Stop Loss:</b> ${last.get('stop_loss', 0):.6f}\n"
                        f"<b>Auditoría IA:</b> {last.get('ai_verdict', 'APPROVED')}\n"
                        f"<i>\"{html.escape(str(last.get('ai_narrative', '')))}\"</i>"
                    )
                else:
                    reply = "Aún no hay operaciones registradas en el historial."
            except Exception as e:
                reply = f"Error al leer auditoría: {e}"
            await self.send_message(reply)
            
        elif cmd_lower == "/pause":
            self.is_paused = True
            await self.send_message("⏸️ <b>El bot ha sido pausado.</b> No evaluará nuevas órdenes hasta que uses /resume.")
            
        elif cmd_lower == "/resume":
            self.is_paused = False
            await self.send_message("▶️ <b>El bot ha sido reanudado.</b> Continúa analizando el mercado cada 5 minutos.")
            
        else:
            # Consulta libre: Te responde directamente Antigravity (Gemini) como Agente Principal
            contexto = ""
            try:
                with open("logs/auditoria.json", "r", encoding="utf-8") as f:
                    aud = json.load(f)
                    if aud:
                        ult = aud[-1]
                        contexto = f"Último activo evaluado: {ult.get('symbol')} a precio ${ult.get('entry_price')}, variación 24h: {ult.get('percent_change_24h')}%"
            except Exception:
                pass
                
            await self.send_message("🧠 <i>Pensando...</i>")
            respuesta_agente = await self.gemini.ask_agent(cmd, market_context=contexto)
            await self.send_message(f"⚡ <b>Antigravity AI:</b>\n\n{respuesta_agente}", parse_mode="")
            self.log_chat(cmd, respuesta_agente)


if __name__ == "__main__":
    notifier = TelegramNotifier()
    asyncio.run(notifier.send_message("Módulo Antigravity principal listo."))
