import os
import time
import requests
import json
from dotenv import load_dotenv
import asyncio

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Pool verificado (07-10-2026) con la API key del usuario. Se puede sobrescribir con GEMINI_MODELS en .env
DEFAULT_POOL = "gemini-3.7-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-3.1-flash-lite"
COOLDOWN_429_SEG = 600   # un modelo sin cuota se salta durante 10 min
COOLDOWN_503_SEG = 120   # un modelo saturado se salta durante 2 min


class GeminiAgentClient:
    """
    Cliente de Inteligencia Artificial Principal basado en Google Gemini (Antigravity).
    Usa modelos de última generación con failover automático ante alta demanda (503/404/429).
    """
    
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("GEMINI_API_KEY")
        pool = os.getenv("GEMINI_MODELS") or DEFAULT_POOL
        self.models_pool = [m.strip() for m in pool.split(",") if m.strip()]
        self._muertos = set()       # 404: el modelo no existe para esta key (se descarta en la sesión)
        self._enfriando = {}        # modelo -> timestamp hasta el que se salta (429/503)
        self._session = requests.Session()   # keep-alive: ahorra el handshake TLS en cada llamada
        
    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("tu_"))

    def _modelos_disponibles(self) -> list:
        ahora = time.time()
        vivos = [m for m in self.models_pool if m not in self._muertos]
        listos = [m for m in vivos if self._enfriando.get(m, 0) <= ahora]
        # Si todos están enfriando, se intenta igualmente el que antes se libere
        return listos or sorted(vivos, key=lambda m: self._enfriando.get(m, 0))[:1]

    def _marcar_error(self, model: str, status: int):
        if status == 404:
            self._muertos.add(model)
            print(f"[Gemini] {model} no existe para esta API key: descartado.", flush=True)
        elif status == 429:
            self._enfriando[model] = time.time() + COOLDOWN_429_SEG
        elif status in (500, 503):
            self._enfriando[model] = time.time() + COOLDOWN_503_SEG

    async def _post(self, model: str, payload: dict, timeout: int):
        url = f"{API_BASE}/{model}:generateContent?key={self.api_key}"
        return await asyncio.to_thread(self._session.post, url, json=payload, timeout=timeout)

    async def ping(self) -> bool:
        """Prueba mínima y barata: True si algún modelo del pool responde 200."""
        if not self.is_configured:
            return False
        payload = {"contents": [{"parts": [{"text": "ok"}]}], "generationConfig": {"maxOutputTokens": 5}}
        for model in self._modelos_disponibles():
            try:
                res = await self._post(model, payload, 20)
                if res.status_code == 200:
                    self._enfriando.pop(model, None)
                    return True
                self._marcar_error(model, res.status_code)
            except Exception:
                continue
        return False
        
    async def ask_agent(self, question: str, market_context: str = "") -> str:
        """Responde a cualquier consulta del usuario desde Telegram con respuestas completas y detalladas."""
        if not self.is_configured:
            return "GEMINI_API_KEY no configurada en .env."
            
        system_instruction = (
            "Eres Antigravity AI, el Agente Cuantitativo Principal y copiloto de trading del usuario en Negrito Trading Bot. "
            "Eres experto en finanzas algorítmicas, riesgo, criptomonedas y la filosofía Outliners (KManuS88). "
            "Responde de forma clara, profesional, completa y rigurosa al operador en Telegram. "
            "Desarrolla tus explicaciones de principio a fin sin cortar tus argumentos."
        )
        
        full_prompt = (
            f"{system_instruction}\n\n"
            f"Contexto del mercado actual:\n{market_context}\n\n"
            f"Pregunta del operador:\n{question}"
        ) if market_context else f"{system_instruction}\n\nPregunta del operador:\n{question}"
        
        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 2500  # Ampliado para respuestas completas y sin cortes
            }
        }
        
        last_error = ""
        for model in self._modelos_disponibles():
            try:
                res = await self._post(model, payload, 60)
                if res.status_code == 200:
                    data = res.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "").strip()
                else:
                    self._marcar_error(model, res.status_code)
                    last_error = f"{model} status {res.status_code}"
            except Exception as e:
                last_error = str(e)
                
        return f"Error temporal de conexión con el cluster de IA ({last_error}). Por favor, reintenta en unos segundos."

    async def evaluate_market_signal(self, symbol: str, price: float, change_1h: float, change_24h: float, volume: float, klines_data: str = "", klines_1h_data: str = "", atr_pct: float = 0.0) -> dict:
        """Evalúa cuantitativa y cualitativamente una señal como Agente Principal con análisis multi-timeframe y Chain of Thought."""
        FALLBACK_SEGURO = {
            "verdict": "VETO",
            "recommended_action": "PASS",
            "risk_score": 7,
            "tp_pct": 1.5,
            "sl_pct": 1.0,
            "dca_levels": 0,
            "narrative": "Fallback de seguridad: sin confirmación de IA, no se abre posición.",
            "allow_trade": False,
        }

        if not self.is_configured:
            return dict(FALLBACK_SEGURO)
            
        prompt = (
            f"Eres un analista de riesgo para MICRO-SCALPING Spot (objetivo: +1.5% TP en 1-2 horas).\n"
            f"Tu trabajo es encontrar razones para RECHAZAR tanto como para aprobar. Un veto correcto "
            f"vale exactamente lo mismo que una aprobación correcta; no hay preferencia por ninguno "
            f"de los dos resultados.\n\n"
            f"DATOS DEL ACTIVO:\n"
            f"- Precio actual: ${price:.6f}\n"
            f"- Cambio 1h: {change_1h:+.2f}%\n"
            f"- Cambio 24h: {change_24h:+.2f}%\n"
            f"- Volumen 24h: ${volume:,.0f} USD\n"
            f"- Volatilidad Promedio (ATR 15m): {atr_pct:.2f}%\n\n"
        )
        if klines_data:
            prompt += f"VELAS 15 MINUTOS (micro-estructura):\n{klines_data}\n\n"
        if klines_1h_data:
            prompt += f"VELAS 1 HORA (tendencia macro):\n{klines_1h_data}\n\n"
            
        prompt += (
            "ANTES DE DECIDIR: escribe brevemente (dentro de 'narrative', no antes) la hipótesis "
            "alcista (por qué podría subir) Y la hipótesis bajista (por qué podría seguir cayendo) "
            "para este activo con estos datos. Aprueba solo si la hipótesis alcista es claramente "
            "más sólida que la bajista con la evidencia disponible, no simplemente posible.\n\n"
            "CRITERIOS PARA APROBAR (deben cumplirse al menos 5 de los 6 siguientes):\n"
            "1. Soporte visible y confirmado en 15m, con al menos una vela de reversión clara "
            "(no una sola mecha, sino cierre y volumen que la respalden).\n"
            "2. El volumen del posible rebote es igual o mayor al volumen promedio reciente, no "
            "decreciente.\n"
            "3. No hay una resistencia REAL comprobada a menos de +1.5% del precio actual (solo cuenta como 'resistencia' un nivel que el precio ya haya intentado romper al alza y haya rechazado múltiples veces; el simple punto máximo desde donde acaba de empezar la última caída NO cuenta como resistencia).\n"
            "4. El activo no subió más de +15% en 24h sin retroceso (evitar comprar en la cima).\n"
            "5. La caída, si la hay, muestra señales de frenado (velas de rango menor, mechas "
            "inferiores), no continuación vertical.\n"
            "6. ACLARACIÓN SOBRE TENDENCIA MACRO: Un contexto de 1h o 24h en negativo es ESPERADO y NORMAL "
            "cuando intentamos cazar un rebote (si no cayera, no habría rebote). Que la tendencia de 1h "
            "sea bajista o el cambio 24h sea negativo NO es motivo de descalificación ni veto por sí solo, "
            "siempre que la micro-estructura de 15m (soporte + reversión clara + volumen) se cumpla.\n\n"
            "VETO AUTOMÁTICO si cualquiera de estas ocurre:\n"
            "- Caída vertical activa sin ninguna vela de recuperación en 15m.\n"
            "- Volumen nulo o decreciente en el supuesto rebote.\n"
            "- Cambio 1h > +5% (ya despegó, entrada tardía).\n"
            "- Cambio 1h < -4% (momentum bajista demasiado fuerte para un scalp).\n"
            "- La evidencia contradice totalmente un rebote. Sin embargo, dado que esta señal superó "
            "un pre-filtro técnico, ya existe una vela verde de reversión. En caso de duda moderada, "
            "APROBAR: asume que el pre-filtro aporta la confirmación necesaria. Solo usa VETO si "
            "es evidente un colapso inminente.\n\n"
            "Si apruebas, define:\n"
            "- tp_pct: 1.0 a 3.0, según la distancia real hasta la siguiente resistencia visible "
            "en las velas (no un valor por defecto).\n"
            f"- sl_pct: ESTRICTAMENTE MAYOR al ATR ({atr_pct:.2f}%) para evitar ser barrido por el ruido intradía (apunta a ~1.2x a 1.5x el ATR según el soporte).\n"
            "- El ratio tp_pct/sl_pct debe ser >= 1.5; si no puedes justificar ese ratio con la "
            "estructura de precio real, VETO en vez de forzar los números.\n"
            "- dca_levels: 0 (no promediar en este sistema; ignora cualquier instinto de sugerir DCA).\n\n"
            "Devuelve ÚNICAMENTE un JSON con: verdict ('APPROVED' o 'VETO'), recommended_action "
            "('BUY' o 'PASS'), risk_score (1-10, donde 10 es más riesgoso), tp_pct (float), "
            "sl_pct (float), dca_levels (int, siempre 0), y narrative (2-3 frases: hipótesis alcista, "
            "hipótesis bajista, y por qué ganó una sobre la otra)."
        )
        
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "maxOutputTokens": 4096   # los modelos 3.x usan tokens de razonamiento: 800 truncaba el JSON
            }
        }
        
        got_429 = False
        got_503 = False
        for model in self._modelos_disponibles():
            try:
                res = await self._post(model, payload, 60)
                if res.status_code != 200:
                    print(f"[Gemini] {model} devolvió {res.status_code}: {res.text[:160]}", flush=True)
                    self._marcar_error(model, res.status_code)
                    if res.status_code == 429:
                        got_429 = True
                    elif res.status_code in (500, 503):
                        got_503 = True
                    continue
                    
                res_json = res.json()
                candidates = res_json.get("candidates") or []
                if not candidates:
                    print(f"[Gemini] Respuesta sin candidatos (posible bloqueo).", flush=True)
                    continue
                    
                cand = candidates[0]
                finish_reason = cand.get("finishReason")
                if finish_reason != "STOP":
                    print(f"[Gemini] Respuesta rechazada: finishReason es {finish_reason} (se esperaba STOP)", flush=True)
                    continue
                    
                parts = cand.get("content", {}).get("parts") or []
                if not parts or "text" not in parts[0]:
                    print("[Gemini] Respuesta sin texto", flush=True)
                    continue

                raw_text = parts[0]["text"].strip()
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    print(f"[Gemini] Error al parsear JSON: {raw_text}", flush=True)
                    continue

                # Validación estricta de esquema y valores (sugerencia de Codex)
                campos_requeridos = ("verdict", "recommended_action", "risk_score", "tp_pct", "sl_pct", "dca_levels", "narrative")
                faltantes = [c for c in campos_requeridos if c not in data]
                if faltantes:
                    print(f"[Gemini] Faltan campos en la respuesta: {faltantes}", flush=True)
                    continue
                    
                v = data.get("verdict")
                ra = data.get("recommended_action")
                
                if v not in ("APPROVED", "VETO") or ra not in ("BUY", "PASS"):
                    print(f"[Gemini] Campos Enum inválidos: verdict={v}, action={ra}", flush=True)
                    continue
                    
                try:
                    tp = float(data["tp_pct"])
                    sl = float(data["sl_pct"])
                    rs = int(data["risk_score"])
                except (ValueError, TypeError):
                    print(f"[Gemini] Tipos numéricos inválidos en TP/SL/Risk.", flush=True)
                    continue
                
                # Solo se validan rangos si APRUEBA (en un VETO es normal que TP/SL vengan a 0)
                if v == "APPROVED" and (not (1.0 <= tp <= 5.0) or not (0.1 <= sl <= 3.0) or not (1 <= rs <= 10)):
                    data["verdict"] = "VETO"
                    data["narrative"] = f"[SCHEMA_ERROR] El LLM devolvió valores fuera de rango: TP={tp}%, SL={sl}%, Risk={rs}. Veto forzado."
                    data["allow_trade"] = False
                elif v == "APPROVED" and sl < atr_pct:
                    data["sl_pct"] = round(max(atr_pct * 1.2, sl), 2)
                    print(f"[ATR_OVERRIDE] SL ajustado de {sl}% a {data['sl_pct']}% por seguridad (ATR es {atr_pct:.2f}%).", flush=True)

                data["allow_trade"] = (data.get("verdict") == "APPROVED")
                data["dca_levels"] = 0
                data["model"] = model
                return data

            except Exception as e:
                print(f"[Gemini] Fallo de red o parseo ({model}): {e}", flush=True)
                continue
                
        fallback = dict(FALLBACK_SEGURO)
        fallback["fallback"] = True
        if got_429:
            fallback["fallback_reason"] = "quota_exhausted"
        elif got_503:
            fallback["fallback_reason"] = "service_unavailable"
        else:
            fallback["fallback_reason"] = "network_or_parse_error"
        return fallback

if __name__ == "__main__":
    agent = GeminiAgentClient()
    res = asyncio.run(agent.ask_agent("¿Qué opinas de Bitcoin?"))
    print("Respuesta:", res)
