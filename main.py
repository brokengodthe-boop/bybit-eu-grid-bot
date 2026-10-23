import asyncio
import os
import sys
import time
import uuid
import requests
from datetime import datetime

from config.validator import validate_environment
from src.cmc_client import CMCClient
from src.bybit_client import BybitClient, BybitError, OrderStateUnknown
from src.bybit_ws import BybitWebSocketClient
from src.gemini_client import GeminiAgentClient
from src.telegram_alerts import TelegramNotifier
from src.store import JsonListStore, AuditoriaCorrupta, clave, leer_estado, escribir_json_atomico
from src.sombra import Sombra
from src.cartera_virtual import CarteraVirtual
from estrategias.logica_anomalias import aplicar_alfas_formulaicos, evaluar_tendencia_bitcoin

from keep_alive import keep_alive
keep_alive()

# ============================================================
# MODO DE EJECUCIÓN
#   DRY_RUN=1 (por defecto): NO abre posiciones nuevas. Registra cada señal en logs/sombra.json.
#   DRY_RUN=0: opera con dinero real. El monitor siempre gestiona las posiciones ya abiertas.
# ============================================================
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"
ADOPTAR_HUERFANAS = os.getenv("ADOPTAR_HUERFANAS", "0") == "1"   # gestionar monedas sin registro
DUST_SWEEPER = os.getenv("DUST_SWEEPER", "0") == "1"
CAPITAL_VIRTUAL = float(os.getenv("CAPITAL_VIRTUAL", "50"))      # capital ficticio del modo sombra

# ============================================================
# CONFIGURACIÓN MICRO-SCALPING
# ============================================================
SCALP_TP_PCT = 1.5
SCALP_SL_PCT = 1.0
BREAKEVEN_TRIGGER_PCT = 0.4
TRAILING_STEP_PCT = 0.3
DCA_MAX_LEVELS = 0           # DCA APAGADO: comparte nivel con el SL y multiplica el riesgo
TIME_EXIT_HOURS = 2.0
TIME_EXIT_EARLY_HOURS = 1.0
SCAN_INTERVAL = 60
MONITOR_INTERVAL = 10
MIN_RR_RATIO = 1.5
SL_CONFIRM_CYCLES = 2

# Límites duros a lo que puede proponer la IA (fuera de rango = se recorta; R/R malo = se rechaza)
TP_PCT_MIN, TP_PCT_MAX = 1.0, 5.0
SL_PCT_MIN, SL_PCT_MAX = 0.5, 2.0

# Riesgo global
MAX_POSICIONES = 2
MAX_PCT_EQUITY_TRADE = 0.50   # tope por operación
MAX_PERDIDA_DIARIA_PCT = 5.0  # si el equity cae este % desde el inicio del día, no se abren más
RISK_PER_TRADE_PCT = 0.50     # 1R = 0.50% del equity total (Max pérdida base si toca stop loss)
STOP_SLIPPAGE_PCT = 0.05
GAP_BUFFER_PCT = 0.20
CASH_RESERVE_PCT = 0.02       # Guardar un poco de efectivo siempre
MIN_ORDEN_USDC = 5.5
MIN_VALOR_GESTION = 1.0       # por debajo de este valor (USD) el monitor deja de gestionar
MAX_DESVIO_PRECIO_PCT = 3.0   # descarta la señal si CMC y Bybit difieren más de esto

# Ciclo de vida de las entradas Limit
ENTRADA_TIMEOUT_SEG = 180
ENTRADA_NO_ENCONTRADA_SEG = 600
SIN_SALDO_CICLOS = 6

# Comisión estimada por lado (para PnL neto). Ajusta a tu tarifa real.
FEE_LADO_PCT = 0.10

# El escáner no abre posiciones si el monitor no completó un ciclo válido en este tiempo
MONITOR_MAX_EDAD_SEG = 90

sl_breach_counter = {}
sin_saldo = {}
_huerfanas_avisadas = set()


# ============================================================
# UTILIDADES
# ============================================================
def coin_de(par: str) -> str:
    return par[:-4] if par.endswith("USDC") else par


def r_precio(x: float) -> float:
    return float(f"{x:.8g}")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def cierre(rec: dict, exit_price: float, razon: str, ahora_iso: str) -> dict:
    entry = rec.get("entry_price") or exit_price
    bruto = (exit_price - entry) / entry * 100
    return {
        "cerrado": True, "estado": "CERRADA", "exit_price": exit_price,
        "exit_reason": razon, "exit_time": ahora_iso,
        "pnl_pct": bruto, "pnl_neto_pct": bruto - 2 * FEE_LADO_PCT,
    }


async def seguro(coro):
    """Ejecuta una corrutina (p. ej. Telegram) sin dejar que su fallo aborte nada."""
    try:
        return await coro
    except Exception as e:
        print(f"[!] Fallo no crítico: {e}", flush=True)
        return None


async def avisar(telegram, texto: str):
    await seguro(telegram.send_message(texto))


async def guardar(store, rec: dict, **cambios):
    """Actualiza el registro en memoria Y en disco, de inmediato."""
    rec.update(cambios)
    if not await store.actualizar(clave(rec), cambios):
        print(f"[!] No se encontró el registro {clave(rec)} para actualizar.", flush=True)


async def precio_seguro(ws, bybit, par: str) -> float:
    p = ws.get_price(par)
    if p and p > 0:
        return p
    try:
        return await asyncio.to_thread(bybit.get_current_price, par)
    except BybitError:
        return 0.0


async def precio_de_fill(bybit, par: str, res, fallback: float) -> float:
    """Precio medio real de una orden ya enviada; si no se puede leer, el de respaldo."""
    try:
        oid = res.get("orderId") if isinstance(res, dict) else None
        if not oid:
            return fallback
        await asyncio.sleep(1)
        orden = await asyncio.to_thread(bybit.find_order, par, None, oid)
        avg = float(orden.get("avgPrice") or 0) if orden else 0.0
        return avg if avg > 0 else fallback
    except Exception:
        return fallback


# ============================================================
# MONITOR DE POSICIONES
# ============================================================
async def gestionar_entrada_pendiente(rec, bybit, telegram, store):
    """Sigue una orden Limit de compra: la cancela por timeout y fija el precio real del fill."""
    b_pair = rec["symbol"]
    link_id = rec.get("entry_link_id")
    ahora = datetime.now()
    if not link_id:
        await guardar(store, rec, estado="ABIERTA")
        return

    edad = (ahora - datetime.fromisoformat(rec["timestamp"])).total_seconds()
    orden = await asyncio.to_thread(bybit.find_order, b_pair, link_id)
    if orden is None:
        if edad > ENTRADA_NO_ENCONTRADA_SEG:
            print(f"[*] Entrada de {b_pair} no existe en Bybit tras {edad:.0f}s. Registro cerrado.", flush=True)
            await guardar(store, rec, cerrado=True, estado="CANCELADA",
                          exit_reason="ENTRY_NOT_FOUND", exit_time=ahora.isoformat())
        return

    st = orden.get("orderStatus")
    cum = float(orden.get("cumExecQty") or 0)
    avg = float(orden.get("avgPrice") or 0)

    if st in ("New", "PartiallyFilled"):
        if edad >= ENTRADA_TIMEOUT_SEG:
            print(f"[*] Entrada de {b_pair} sin completar tras {edad:.0f}s. Cancelando...", flush=True)
            await asyncio.to_thread(bybit.cancel_all_orders, b_pair)
        return
    if st in ("Filled", "PartiallyFilledCanceled") and avg <= 0:
        return  # esperar a que el precio medio esté disponible

    if cum > 0 and avg > 0:
        tp_pct = rec.get("tp_pct", SCALP_TP_PCT)
        sl_pct = rec.get("sl_pct", SCALP_SL_PCT)
        await guardar(
            store, rec, estado="ABIERTA", entry_price=avg, qty_entrada=cum,
            fill_time=ahora.isoformat(),
            take_profit=r_precio(avg * (1 + tp_pct / 100)),
            stop_loss=r_precio(avg * (1 - sl_pct / 100)),
        )
        print(f"[+] Entrada ejecutada en {b_pair}: {cum} @ ${avg:.6f} | "
              f"TP ${rec['take_profit']:.6f} | SL ${rec['stop_loss']:.6f}", flush=True)
        await avisar(telegram, f"✅ <b>ENTRADA EJECUTADA</b>\n\nPar: {b_pair}\nPrecio real: ${avg:.6f}\n"
                               f"TP: ${rec['take_profit']:.6f} | SL: ${rec['stop_loss']:.6f}")
        return

    print(f"[*] Entrada de {b_pair} terminó sin ejecutarse ({st}). Registro cerrado.", flush=True)
    await guardar(store, rec, cerrado=True, estado="CANCELADA",
                  exit_reason="ENTRY_NOT_FILLED", exit_time=ahora.isoformat())


async def revisar_tp(rec, bybit, telegram, store) -> bool:
    """True si la orden TP se ejecutó de verdad (se comprueba su estado, no se infiere)."""
    tp_id = rec.get("tp_order_id")
    if not tp_id:
        return False
    b_pair = rec["symbol"]
    orden = await asyncio.to_thread(bybit.find_order, b_pair, None, tp_id)
    if orden is None:
        return False  # no concluyente: no asumir nada
    st = orden.get("orderStatus")
    if st == "Filled":
        avg = float(orden.get("avgPrice") or 0)
        exit_price = avg if avg > 0 else rec.get("take_profit", 0)
        c = cierre(rec, exit_price, "TAKE PROFIT (ZERO-LAG)", datetime.now().isoformat())
        await guardar(store, rec, **c)
        print(f"[+] Take Profit ejecutado en {b_pair} a ${exit_price:.6f}", flush=True)
        await avisar(telegram,
                     f"🎯 <b>TAKE PROFIT EJECUTADO</b>\n\nPar: {b_pair}\n"
                     f"Entrada: ${rec['entry_price']:.6f}\nSalida: ${exit_price:.6f}\n"
                     f"Rendimiento: {c['pnl_pct']:+.2f}% (neto est. {c['pnl_neto_pct']:+.2f}%)")
        return True
    if st in ("Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"):
        print(f"[!] La orden TP de {b_pair} quedó {st}. Se volverá a colocar.", flush=True)
        await guardar(store, rec, tp_order_id=None)
    return False


async def cerrar_a_mercado(rec, amount, precio, razon, bybit, store, ahora_iso) -> bool:
    b_pair = rec["symbol"]
    await asyncio.to_thread(bybit.cancel_all_orders, b_pair)
    await asyncio.sleep(1)
    res = await asyncio.to_thread(bybit.place_spot_order, symbol=b_pair, side="Sell", qty=amount,
                                  market_unit="baseCoin", order_type="Market")
    if not res:
        print(f"[!] No se pudo vender {b_pair} ({razon}). Se reintentará.", flush=True)
        return False
    salida = await precio_de_fill(bybit, b_pair, res, precio)
    await guardar(store, rec, **cierre(rec, salida, razon, ahora_iso))
    sl_breach_counter.pop(b_pair, None)
    return True


async def gestionar_registro(rec, balances, bybit, ws, telegram, store, valid_symbols):
    b_pair = rec.get("symbol")
    if not b_pair or b_pair not in valid_symbols:
        return
    ahora_iso = datetime.now().isoformat()

    if rec.get("estado") == "PENDIENTE_ENTRADA":
        await gestionar_entrada_pendiente(rec, bybit, telegram, store)
        return

    amount = balances.get(coin_de(b_pair), 0.0)
    precio = await precio_seguro(ws, bybit, b_pair)
    if precio <= 0:
        return
    valor = amount * precio

    # 1) ¿Se ejecutó el Take Profit?
    if await revisar_tp(rec, bybit, telegram, store):
        sl_breach_counter.pop(b_pair, None)
        return

    # 2) ¿La posición ya no existe? (esperar unos ciclos antes de darla por cerrada)
    if valor < 0.5:
        n = sin_saldo.get(b_pair, 0) + 1
        sin_saldo[b_pair] = n
        if n >= SIN_SALDO_CICLOS:
            ordenes = await asyncio.to_thread(bybit.get_open_orders, b_pair)
            if not ordenes:
                await guardar(store, rec, **cierre(rec, precio, "POSITION_CLOSED", ahora_iso))
                sin_saldo.pop(b_pair, None)
                print(f"[*] Reconciliada posición cerrada de {b_pair}.", flush=True)
        return
    sin_saldo.pop(b_pair, None)
    if valor < MIN_VALOR_GESTION:
        return

    tp = rec.get("take_profit", 0)
    sl = rec.get("stop_loss", 0)
    if tp <= 0 or sl <= 0:
        return
    entry = rec.get("entry_price") or precio

    # 3) Orden TP Limit (Zero-Lag) si no existe
    if not rec.get("tp_order_id"):
        existentes = await asyncio.to_thread(bybit.get_open_orders, b_pair)
        ventas = [o for o in existentes if o.get("side") == "Sell"]
        if ventas:
            await guardar(store, rec, tp_order_id=ventas[0].get("orderId"))
            print(f"[*] Sincronizada orden Limit Sell existente de {b_pair}", flush=True)
        else:
            print(f"[*] Colocando TP Limit para {b_pair} a ${tp:.6f}", flush=True)
            res = await asyncio.to_thread(bybit.place_spot_order, symbol=b_pair, side="Sell",
                                          qty=amount, order_type="Limit", price=tp)
            if res:
                await guardar(store, rec, tp_order_id=res.get("orderId"))

    pnl_pct = (precio - entry) / entry * 100

    # A) Break-even + trailing
    if pnl_pct >= BREAKEVEN_TRIGGER_PCT:
        trail_sl_pct = max(0.2, pnl_pct - TRAILING_STEP_PCT)
        trailing_price = r_precio(entry * (1 + trail_sl_pct / 100))
        if trailing_price > sl:
            old_sl = sl
            await guardar(store, rec, stop_loss=trailing_price)
            sl = trailing_price
            label = "TRAILING STOP" if pnl_pct > BREAKEVEN_TRIGGER_PCT + 0.3 else "BREAK-EVEN"
            print(f"[🛡️ {label}] {b_pair}: PnL={pnl_pct:+.2f}%. SL ${old_sl:.6f} -> ${trailing_price:.6f}", flush=True)
            await avisar(telegram, f"🛡️ <b>{label} ACTIVADO</b>\n\nPar: {b_pair} | Ganancia: {pnl_pct:+.2f}%\n"
                                   f"SL protegido en: ${trailing_price:.6f} (+{trail_sl_pct:.2f}%)")

    # B) DCA (apagado con DCA_MAX_LEVELS = 0)
    dca_count = rec.get("dca_count", 0)
    dca_max = min(rec.get("dca_max", DCA_MAX_LEVELS), DCA_MAX_LEVELS)
    if dca_max > 0 and dca_count < dca_max and pnl_pct <= -rec.get("sl_pct", SCALP_SL_PCT):
        disponible = await asyncio.to_thread(bybit.get_balance, "USDC")
        dca_amount = max(MIN_ORDEN_USDC, disponible * 0.20)
        if disponible >= dca_amount:
            print(f"[📊 DCA #{dca_count + 1}] {b_pair}: PnL={pnl_pct:+.2f}%. Promediando con ${dca_amount:.2f}", flush=True)
            res = await asyncio.to_thread(bybit.place_spot_order, symbol=b_pair, side="Buy",
                                          qty=dca_amount, order_type="Market", market_unit="quoteCoin")
            if res:
                new_avg = (amount * entry + dca_amount) / (amount + dca_amount / precio)
                dyn_tp = rec.get("tp_pct", SCALP_TP_PCT)
                dyn_sl = rec.get("sl_pct", SCALP_SL_PCT)
                await guardar(store, rec, entry_price=r_precio(new_avg), dca_count=dca_count + 1,
                              take_profit=r_precio(new_avg * (1 + dyn_tp / 100)),
                              stop_loss=r_precio(new_avg * (1 - dyn_sl / 100)), tp_order_id=None)
                await asyncio.to_thread(bybit.cancel_all_orders, b_pair)
                await avisar(telegram, f"📊 <b>DCA #{dca_count + 1} EJECUTADO</b>\n\nPar: {b_pair}\n"
                                       f"Nuevo promedio: ${new_avg:.6f}")
                return  # datos en memoria desactualizados: seguir en el próximo ciclo

    # C) Salida por tiempo
    try:
        inicio = datetime.fromisoformat(rec.get("fill_time") or rec["timestamp"])
        horas = (datetime.now() - inicio).total_seconds() / 3600.0
        max_loss = rec.get("sl_pct", SCALP_SL_PCT)
        salir = ((horas >= TIME_EXIT_EARLY_HOURS and pnl_pct < -1.0)
                 or (horas >= TIME_EXIT_HOURS and pnl_pct >= -max_loss))
    except (KeyError, ValueError) as e:
        print(f"[!] Error en time-exit de {b_pair}: {e}", flush=True)
        salir, horas = False, 0.0
    if salir:
        print(f"[⌛ TIME-EXIT] {b_pair} lleva {horas:.1f}h (PnL: {pnl_pct:+.2f}%). Rotando capital...", flush=True)
        if await cerrar_a_mercado(rec, amount, precio, f"TIME_EXIT ({horas:.0f}h)", bybit, store, ahora_iso):
            await avisar(telegram, f"⌛ <b>ROTACIÓN DE CAPITAL</b>\n\nPar: {b_pair} | {horas:.1f}h abierta\n"
                                   f"Rendimiento: {pnl_pct:+.2f}%")
        return

    # D) Stop Loss con confirmación (evita wicks)
    if precio <= sl:
        n = sl_breach_counter.get(b_pair, 0) + 1
        sl_breach_counter[b_pair] = n
        if n < SL_CONFIRM_CYCLES:
            print(f"[⚠️ SL BREACH {n}/{SL_CONFIRM_CYCLES}] {b_pair} a ${precio:.6f} (SL: ${sl:.6f})", flush=True)
        else:
            print(f"[🛑 STOP LOSS CONFIRMADO] {b_pair} a ${precio:.6f} (SL: ${sl:.6f})", flush=True)
            if await cerrar_a_mercado(rec, amount, precio, "STOP LOSS", bybit, store, ahora_iso):
                await avisar(telegram, f"🛑 <b>STOP LOSS EJECUTADO</b>\n\nPar: {b_pair}\nEntrada: ${entry:.6f}\n"
                                       f"Salida: ${rec['exit_price']:.6f}\nRendimiento: {rec['pnl_pct']:+.2f}%")
    elif b_pair in sl_breach_counter:
        sl_breach_counter.pop(b_pair, None)
        print(f"[✅ SL RESET] {b_pair} recuperó ${precio:.6f}. Wick filtrado.", flush=True)


async def ciclo_monitor(bybit, ws, telegram, store, valid_symbols, estado):
    balances = await asyncio.to_thread(bybit.get_all_balances)   # lanza BybitError -> se salta el ciclo
    auditoria = await store.leer()                                # lanza AuditoriaCorrupta si está roto
    abiertos = [r for r in auditoria if not r.get("cerrado", False)]
    errores = 0

    for rec in abiertos:
        try:
            await gestionar_registro(rec, balances, bybit, ws, telegram, store, valid_symbols)
        except OrderStateUnknown as e:
            errores += 1
            print(f"[!] Estado de orden incierto en {rec.get('symbol')}: {e}", flush=True)
            await avisar(telegram, f"⚠️ Estado de orden incierto en {rec.get('symbol')}. Revísalo en Bybit.")
        except Exception as e:
            errores += 1
            print(f"[!] Error gestionando {rec.get('symbol')}: {e}", flush=True)

    # Monedas en cuenta sin registro
    conocidos = {r.get("symbol") for r in abiertos}
    for coin, amount in balances.items():
        if coin in ("USDC", "USDT", "EUR"):
            continue
        par = f"{coin}USDC"
        if par not in valid_symbols or par in conocidos:
            continue
        precio = await precio_seguro(ws, bybit, par)
        if precio <= 0 or amount * precio < 5.1:
            continue
        if not ADOPTAR_HUERFANAS:
            if par not in _huerfanas_avisadas:
                _huerfanas_avisadas.add(par)
                print(f"[!] Saldo de {coin} (${amount * precio:.2f}) sin registro: NO se gestiona "
                      f"(ADOPTAR_HUERFANAS=0).", flush=True)
            continue
        ahora_iso = datetime.now().isoformat()
        registro = {
            "id": uuid.uuid4().hex[:12], "timestamp": ahora_iso, "symbol": par, "entry_price": precio,
            "take_profit": r_precio(precio * (1 + SCALP_TP_PCT / 100)),
            "stop_loss": r_precio(precio * (1 - SCALP_SL_PCT / 100)),
            "tp_pct": SCALP_TP_PCT, "sl_pct": SCALP_SL_PCT, "dca_max": 0, "dca_count": 0,
            "ai_verdict": "RECOVERED", "ai_risk_score": 0, "ai_narrative": "Posición adoptada automáticamente.",
            "estado": "ABIERTA", "fill_time": ahora_iso, "cerrado": False,
        }
        await store.agregar(registro)
        ws.subscribe(par)
        print(f"[+] Posición huérfana de {par} adoptada.", flush=True)

    if errores == 0:
        estado["monitor_ok"] = time.time()


async def monitor_posiciones_ws(bybit, ws, telegram, store, valid_symbols, estado):
    corrupto_avisado = False
    while True:
        try:
            await ciclo_monitor(bybit, ws, telegram, store, valid_symbols, estado)
            corrupto_avisado = False
        except AuditoriaCorrupta as e:
            print(f"[🚨] {e}", flush=True)
            if not corrupto_avisado:
                corrupto_avisado = True
                await avisar(telegram, f"🚨 auditoria.json corrupto: el monitor está detenido.\n{e}")
        except Exception as e:
            print(f"[!] Error en monitor de posiciones: {e}", flush=True)
        await asyncio.sleep(MONITOR_INTERVAL)


# ============================================================
# ESCÁNER DE SEÑALES
# ============================================================
def equity_inicio_del_dia(hoy: str, equity: float) -> dict:
    ruta = "logs/estado_dia.json"
    est = leer_estado(ruta)
    if est.get("fecha") != hoy or not est.get("equity_inicio"):
        est = {"fecha": hoy, "equity_inicio": equity, "alerta_enviada": False}
        escribir_json_atomico(ruta, est)
    return est


class Escaner:
    def __init__(self, telegram, gemini, ws, bybit, cmc, store, sombra, estado, valid_symbols):
        self.telegram, self.gemini, self.ws, self.bybit, self.cmc = telegram, gemini, ws, bybit, cmc
        self.store, self.sombra, self.estado, self.valid_symbols = store, sombra, estado, valid_symbols
        self.veto_cooldowns = {}
        self.last_daily_report_date = None
        self.btc_bloqueado = False
        self.ultimo_refresco = time.time()
        self.monitor_task = None
        self.paused = False  # Indicates pause due to Gemini outages
        self.fallos_ia = 0   # fallos consecutivos de Gemini (sin respuesta válida)
        self.espera_pausa = 120

    def lanzar_monitor(self):
        self.monitor_task = asyncio.create_task(monitor_posiciones_ws(
            self.bybit, self.ws, self.telegram, self.store, self.valid_symbols, self.estado))

    async def health_check(self) -> bool:
        """True si algún modelo del pool de Gemini responde (llamada mínima, casi sin coste)."""
        try:
            return await self.gemini.ping()
        except Exception:
            return False

    def registrar_fallo_ia(self, razon: str) -> int:
        """Backoff exponencial ante fallos de Gemini. Devuelve los segundos de espera."""
        self.fallos_ia += 1
        if razon == "quota_exhausted":
            espera = 900
        else:
            espera = min(60 * (2 ** (self.fallos_ia - 1)), 900)
        if self.fallos_ia >= 3 and not self.paused:
            self.paused = True
            print(f"[⏳] {self.fallos_ia} fallos seguidos de Gemini ({razon}). Escáner en PAUSA "
                  f"(el monitor de posiciones sigue activo).", flush=True)
        self.espera_pausa = espera
        return espera

    async def ciclo(self) -> int:
        """Un ciclo de escaneo. Devuelve los segundos que hay que esperar antes del siguiente."""
        if self.telegram.is_paused:
            print("[!] Bot en PAUSA desde Telegram.", flush=True)
            return 30

        # Si el escáner está en pausa por problemas de Gemini, intentar recuperación
        if self.paused:
            print("[⏳] Escáner pausado por Gemini, verificando salud...", flush=True)
            if await self.health_check():
                print("[✅] Gemini operativo – reanudando escáner.", flush=True)
                self.paused = False
                self.fallos_ia = 0
            else:
                return self.espera_pausa

        now = datetime.now()
        hoy = now.strftime("%Y-%m-%d")
        ts = now.isoformat()

        # El reporte diario lo envía telegram.daily_report_loop() a las 23:59 (evita duplicados)

        if time.time() - self.ultimo_refresco > 6 * 3600:
            try:
                nuevos = await asyncio.to_thread(self.bybit.get_valid_symbols)
                self.valid_symbols.clear()
                self.valid_symbols.update(nuevos)
                self.ultimo_refresco = time.time()
            except BybitError as e:
                print(f"[!] No se pudo refrescar el catálogo de símbolos: {e}", flush=True)

        modo = "SOMBRA (DRY_RUN)" if DRY_RUN else "REAL"
        print(f"\n[{ts}] === CICLO DE TRADING [{modo}] ===", flush=True)

        # No abrir posiciones si no se pueden vigilar
        if not DRY_RUN:
            if self.monitor_task is None or self.monitor_task.done():
                print("[🚨] La tarea del monitor no está activa. Reiniciándola.", flush=True)
                await avisar(self.telegram, "🚨 El monitor de posiciones se detuvo. Reiniciándolo.")
                self.lanzar_monitor()
                return 15
            edad = time.time() - self.estado["monitor_ok"]
            if edad > MONITOR_MAX_EDAD_SEG:
                print(f"[!] Monitor sin ciclo válido hace {edad:.0f}s: no se abren posiciones.", flush=True)
                return 15

        available_usdc, total_equity = await asyncio.gather(
            asyncio.to_thread(self.bybit.get_balance, "USDC"),
            asyncio.to_thread(self.bybit.get_total_equity))
        print(f"[*] Bybit: {available_usdc:.2f} USDC disponibles | Equity: ${total_equity:.2f}", flush=True)

        if not DRY_RUN:
            if total_equity <= 0:
                print("[!] Equity no legible o cero: no se opera.", flush=True)
                return 60
            if total_equity < 5.0:
                print(f"[ALERTA DE CAPITAL] Equity ${total_equity:.2f} < $5. Pausando.", flush=True)
                await avisar(self.telegram, f"🚨 <b>DISYUNTOR DE CAPITAL</b>\n\nEquity ${total_equity:.2f}. Operaciones pausadas.")
                return 300
            est = equity_inicio_del_dia(hoy, total_equity)
            if total_equity < est["equity_inicio"] * (1 - MAX_PERDIDA_DIARIA_PCT / 100):
                print(f"[🚨 PÉRDIDA DIARIA] Equity ${total_equity:.2f} vs inicio ${est['equity_inicio']:.2f}. "
                      f"No se abren posiciones hoy.", flush=True)
                if not est.get("alerta_enviada"):
                    est["alerta_enviada"] = True
                    escribir_json_atomico("logs/estado_dia.json", est)
                    await avisar(self.telegram, f"🚨 <b>PÉRDIDA DIARIA MÁXIMA ({MAX_PERDIDA_DIARIA_PCT}%)</b>\n\n"
                                                f"Sin nuevas posiciones hasta mañana.")
                return 300

        auditoria = await self.store.leer()
        simbolos_abiertos = {t.get("symbol") for t in auditoria if not t.get("cerrado", False)}
        if not DRY_RUN:
            activos = [t for t in auditoria if not t.get("cerrado", False) and t.get("symbol") in self.valid_symbols]
            if len(activos) >= MAX_POSICIONES:
                print(f"[TOPE] Posiciones activas {len(activos)}/{MAX_POSICIONES}. Esperando cierres.", flush=True)
                return 60

        # CAPA 1: filtro maestro de BTC (falla cerrado) — en paralelo con los saldos
        balances, klines_btc = await asyncio.gather(
            asyncio.to_thread(self.bybit.get_all_balances),
            asyncio.to_thread(self.bybit.get_klines, "BTCUSDC", interval="15", limit=20))
        btc_seguro, btc_razon = evaluar_tendencia_bitcoin(klines_btc)
        if not btc_seguro:
            print(f"[CAPA 1 ACTIVA] {btc_razon}", flush=True)
            if not self.btc_bloqueado:
                self.btc_bloqueado = True
                await avisar(self.telegram, f"🛡️ <b>FILTRO MAESTRO (CAPA 1)</b>\n\n{btc_razon}\nCompras suspendidas.")
            return 300
        self.btc_bloqueado = False

        # Ingesta y señal cuantitativa
        df_mercado = await self.cmc.get_gainers_losers(limit=100)
        if df_mercado.empty or "symbol" not in df_mercado.columns:
            print("[!] Ingesta vacía o error en CoinMarketCap.", flush=True)
            return 300
        df_analizado = aplicar_alfas_formulaicos(df_mercado)

        mejor, par = None, None
        for _, row in df_analizado.iterrows():
            cand = f"{row['symbol']}USDC"
            if cand not in self.valid_symbols:
                continue
            if cand in self.veto_cooldowns:
                if (now - self.veto_cooldowns[cand]).total_seconds() < 3600:
                    continue
                del self.veto_cooldowns[cand]
            if row.get("volume", 0) < 10_000_000:
                continue
            if DRY_RUN and await self.sombra.en_cooldown(cand):
                continue
            if cand in simbolos_abiertos:
                continue
            precio_row = float(row["close_price"])
            if row["symbol"] in balances and balances[row["symbol"]] * precio_row >= 0.5:
                print(f"[*] Saltando {cand}: ya tenemos una posición.", flush=True)
                continue
            if not DRY_RUN:
                pendientes = await asyncio.to_thread(self.bybit.get_open_orders, cand)  # si falla, se salta el ciclo
                if any(o.get("side") == "Buy" for o in pendientes):
                    print(f"[*] Saltando {cand}: tiene una orden Buy pendiente.", flush=True)
                    continue
            mejor, par = row, cand
            break

        if mejor is None:
            print("[*] Sin señales válidas en este ciclo.", flush=True)
            return SCAN_INTERVAL

        symbol = mejor["symbol"]
        precio_cmc = float(mejor["close_price"])
        cambio_1h = float(mejor.get("percent_change", 0.0))
        cambio_24h = float(mejor.get("percent_change_24h", 0.0))
        volumen = float(mejor.get("volume", 0.0))

        try:
            precio = await asyncio.to_thread(self.bybit.get_current_price, par)
        except BybitError as e:
            print(f"[!] Sin precio de Bybit para {par}: {e}", flush=True)
            return SCAN_INTERVAL
        desvio = abs(precio / precio_cmc - 1) * 100 if precio_cmc > 0 else 999.0
        if desvio > MAX_DESVIO_PRECIO_PCT:
            print(f"[!] {par}: CMC ${precio_cmc:.6f} vs Bybit ${precio:.6f} ({desvio:.1f}%). Señal descartada.", flush=True)
            self.veto_cooldowns[par] = now
            return SCAN_INTERVAL

        print(f"[*] Señal: {par} | Bybit ${precio:.6f} | 1h: {cambio_1h:+.2f}% | 24h: {cambio_24h:+.2f}%", flush=True)

        raw_15m, klines_1h = await asyncio.gather(
            asyncio.to_thread(self.bybit.get_klines_raw, par, interval="15", limit=20),
            asyncio.to_thread(self.bybit.get_klines, par, interval="60", limit=12))

        if not raw_15m or len(raw_15m) < 2:
            print(f"[*] {par}: Faltan velas 15m. Señal descartada.", flush=True)
            return SCAN_INTERVAL

        # Pre-filtro: 
        # 1. Si ni la vela actual ni la anterior son verdes (close > open), descartar para ahorrar API
        # 2. Si la tendencia de 1h es excesivamente negativa (< -0.5%), evitar atrapar "cuchillos cayendo"
        # 3. Si la tendencia de 1h es excesivamente alcista (> +1.0%), evitar comprar la cima
        c_actual = raw_15m[-1]
        c_anterior = raw_15m[-2]
        if (c_actual[4] <= c_actual[1] and c_anterior[4] <= c_anterior[1]) or cambio_1h < -0.5 or cambio_1h > 1.0:
            print(f"[*] {par}: Pre-filtro falló (velas rojas en 15m, o tendencia 1h fuera del rango [-0.5%, +1.0%]).", flush=True)
            self.veto_cooldowns[par] = now
            return SCAN_INTERVAL

        # Convertimos las velas raw al formato texto que espera Gemini
        lines = ["Hora | Open | High | Low | Close | Volume"]
        for k in raw_15m:
            hora = datetime.fromtimestamp(k[0] / 1000).strftime("%H:%M")
            lines.append(f"{hora} | {k[1]} | {k[2]} | {k[3]} | {k[4]} | {k[5]}")
        klines_15m = "\n".join(lines)

        # Cálculo del ATR de 14 periodos en 15m
        atr_pct = 1.0 # fallback
        if len(raw_15m) >= 15:
            trs = []
            for i in range(1, len(raw_15m)):
                k_curr = raw_15m[i]
                k_prev = raw_15m[i-1]
                high = float(k_curr[2])
                low = float(k_curr[3])
                close_prev = float(k_prev[4])
                trs.append(max(high - low, abs(high - close_prev), abs(low - close_prev)))
            
            trs_14 = trs[-14:] if len(trs) >= 14 else trs
            atr = sum(trs_14) / len(trs_14)
            atr_pct = (atr / precio) * 100 if precio > 0 else 1.0

        ts_ms_eval = int(time.time() * 1000) # Snapshot temporal exacto ANTES de la evaluación (sugerencia de Codex)
        
        try:
            ev = await self.gemini.evaluate_market_signal(
                symbol=symbol, price=precio, change_1h=cambio_1h, change_24h=cambio_24h,
                volume=volumen, klines_data=klines_15m, klines_1h_data=klines_1h, atr_pct=atr_pct)
        except Exception as e:
            print(f"[!] Gemini no respondió ({e}). No se opera este ciclo.", flush=True)
            return self.registrar_fallo_ia("exception")
        if not isinstance(ev, dict):
            ev = {}

        # Sin respuesta real de la IA: NO es un veto. No se registra en sombra (contaminaría las
        # estadísticas) ni se pone el par en cooldown; se espera con backoff y se reintenta.
        if ev.get("fallback") or not ev:
            razon = ev.get("fallback_reason", "sin_respuesta")
            espera = self.registrar_fallo_ia(razon)
            print(f"[⏳] Gemini sin respuesta válida ({razon}). {par} se reevaluará. Reintento en {espera}s.", flush=True)
            return espera
        self.fallos_ia = 0

        # FAIL-CLOSED: sin aprobación explícita y válida, se rechaza
        veredicto = ev.get("verdict", "REJECTED_FAILSAFE")
        narrativa = ev.get("narrative", "Sin respuesta válida de la IA. Operación cancelada por seguridad.")
        risk_score = ev.get("risk_score", 10)
        aprobada = ev.get("allow_trade") is True and veredicto == "APPROVED"

        try:
            tp_pct = float(ev.get("tp_pct", SCALP_TP_PCT))
            sl_pct = float(ev.get("sl_pct", SCALP_SL_PCT))
            dca_levels = int(ev.get("dca_levels", 0))
        except (TypeError, ValueError):
            tp_pct, sl_pct, dca_levels = SCALP_TP_PCT, SCALP_SL_PCT, 0
            aprobada, veredicto = False, "REJECTED_BAD_PARAMS"

        # En un veto la IA suele devolver TP/SL = 0: para la simulación contrafactual de sombra
        # se usan los valores por defecto (si no, SL = precio de entrada y el análisis sería inútil)
        if not aprobada and not (TP_PCT_MIN <= tp_pct <= TP_PCT_MAX and SL_PCT_MIN <= sl_pct <= SL_PCT_MAX):
            tp_pct, sl_pct = SCALP_TP_PCT, SCALP_SL_PCT

        if aprobada and tp_pct / sl_pct < MIN_RR_RATIO:
            print(f"[⚖️ R/R] TP {tp_pct}% / SL {sl_pct}% < {MIN_RR_RATIO}. Señal rechazada (no se aprieta el SL).", flush=True)
            aprobada, veredicto = False, "REJECTED_RR"

        print(f"[*] IA: {veredicto} (riesgo {risk_score}/10) | TP +{tp_pct}% | SL -{sl_pct}% | {narrativa}", flush=True)

        combined_signal = float(mejor.get("combined_signal", 10.0))

        # Modo sombra: se registra TODA señal evaluada (aprobada o no) para medir la estrategia y el veto
        reg_id = None
        if not await self.sombra.en_cooldown(par):
            reg_id = await self.sombra.registrar(
                par, precio_ref=precio, tp_pct=tp_pct, sl_pct=sl_pct, precio_cmc=precio_cmc,
                veredicto=veredicto, allow_trade=aprobada, risk_score=risk_score,
                combined_signal=combined_signal, cambio_1h=cambio_1h, cambio_24h=cambio_24h,
                volumen=volumen, modo="DRY_RUN" if DRY_RUN else "REAL",
                version="v4.1_gemini_flash", narrative=ev.get("narrative", ""),
                fallback=ev.get("fallback", False), fallback_reason=ev.get("fallback_reason", ""),
                model=ev.get("model", "unknown"), ts_ms_eval=ts_ms_eval)

        tp = r_precio(precio * (1 + tp_pct / 100))
        sl = r_precio(precio * (1 - sl_pct / 100))

        if not aprobada:
            print(f"[!] VETO: operación en {par} cancelada.", flush=True)
            self.veto_cooldowns[par] = now
            # Sin aviso a Telegram (solo notificaciones positivas) y sin escribir en auditoria.json:
            # el veto ya queda registrado con más detalle en logs/sombra.json.
            return SCAN_INTERVAL

        if DRY_RUN:
            print(f"[🧪 SOMBRA] Señal APROBADA en {par} registrada. NO se envía ninguna orden.", flush=True)
            linea_cartera = ""
            cartera = self.sombra.cartera
            if cartera and reg_id:
                pos, motivo = cartera.abrir(reg_id, par, precio, tp_pct, sl_pct)
                if pos:
                    print(f"[💼 CARTERA] Posición virtual {par}: {pos['usd']:.2f} | saldo {cartera.e['balance']:.2f} "
                          f"| libre {cartera.disponible:.2f}", flush=True)
                    linea_cartera = (f"\n\n💼 <b>Cartera virtual:</b> {pos['usd']:.2f} {cartera.e['moneda']} invertidos\n"
                                     f"Saldo {cartera.e['balance']:.2f} | Libre {cartera.disponible:.2f}")
                else:
                    print(f"[💼 CARTERA] Posición virtual omitida en {par}: {motivo}", flush=True)
                    linea_cartera = f"\n\n💼 Cartera virtual: omitida ({motivo})"
            await avisar(self.telegram, f"🧪 <b>SEÑAL (modo sombra)</b>\n\nPar: {par}\nPrecio: ${precio:.6f}\n"
                                        f"TP +{tp_pct}% / SL -{sl_pct}%\nNo se ejecutó ninguna orden.{linea_cartera}")
            return SCAN_INTERVAL

        # ---------------- Ejecución real ----------------
        import math
        risk_budget_usdc = total_equity * (RISK_PER_TRADE_PCT / 100.0)
        
        # Pérdida estimada como porcentaje del nominal, incluyendo stop, fees, slippage y colchón de gaps
        loss_pct_of_notional = (sl_pct + (2 * FEE_LADO_PCT) + STOP_SLIPPAGE_PCT + GAP_BUFFER_PCT)
        loss_fraction = loss_pct_of_notional / 100.0
        
        notional_by_risk = risk_budget_usdc / loss_fraction if loss_fraction > 0 else 0
        notional_by_equity = total_equity * MAX_PCT_EQUITY_TRADE
        notional_by_cash = available_usdc * (1 - CASH_RESERVE_PCT)
        
        trade_amount_usdc = min(notional_by_risk, notional_by_equity, notional_by_cash)
        
        if trade_amount_usdc < MIN_ORDEN_USDC:
            print(f"[!] Tamaño calculado ({trade_amount_usdc:.2f} USDC) es menor al mínimo de {MIN_ORDEN_USDC}.", flush=True)
            return SCAN_INTERVAL

        print(f"[*] Tamaño: {trade_amount_usdc:.2f} USDC (Riesgo {RISK_PER_TRADE_PCT}% eq, nominal basado en SL {sl_pct}%)", flush=True)
        link_id = f"{par}-B-{int(time.time())}-{uuid.uuid4().hex[:6]}"[:36]
        registro = {
            "id": uuid.uuid4().hex[:12], "timestamp": ts, "symbol": par, "entry_price": precio,
            "percent_change_1h": cambio_1h, "percent_change_24h": cambio_24h, "margin_ratio": 0.0,
            "available_usdc": available_usdc, "total_equity": total_equity, "take_profit": tp,
            "stop_loss": sl, "tp_pct": tp_pct, "sl_pct": sl_pct, "dca_max": int(dca_levels),
            "ai_verdict": veredicto, "ai_risk_score": risk_score, "ai_narrative": narrativa,
            "unrealized_pnl": 0.0, "cerrado": False, "dca_count": 0,
            "estado": "PENDIENTE_ENTRADA", "entry_link_id": link_id, "usdc_invertido": trade_amount_usdc,
        }
        # Registro ANTES de enviar la orden: si el proceso muere justo después, la posición no queda huérfana
        await self.store.agregar(registro)
        self.ws.subscribe(par)

        incierto = False
        try:
            res = await asyncio.to_thread(
                self.bybit.place_spot_order, symbol=par, side="Buy", qty=trade_amount_usdc / precio,
                order_type="Limit", price=precio, order_link_id=link_id)
        except OrderStateUnknown as e:
            incierto, res = True, None
            print(f"[!] Estado incierto de la orden de entrada: {e}", flush=True)
            await avisar(self.telegram, f"⚠️ Orden de entrada en {par} con estado incierto. El monitor la resolverá.")

        if res is None and not incierto:
            print(f"[-] Orden rechazada para {par}. Cooldown de 1h.", flush=True)
            await guardar(self.store, registro, cerrado=True, estado="RECHAZADA",
                          exit_reason="ORDER_REJECTED", exit_time=datetime.now().isoformat())
            self.veto_cooldowns[par] = now
            return SCAN_INTERVAL

        if res:
            await guardar(self.store, registro, entry_order_id=res.get("orderId"))
            print(f"[+] Orden Limit enviada. ID: {res.get('orderId')}", flush=True)
            await seguro(self.telegram.notify_trade(
                symbol=par, price=precio, change_1h=cambio_1h, change_24h=cambio_24h, tp=tp, sl=sl,
                ai_verdict=veredicto, ai_narrative=narrativa, trade_amount=trade_amount_usdc))
        return SCAN_INTERVAL


async def orquestador_bucle(telegram, gemini, ws, store, sombra, estado):
    validate_environment()
    cmc = CMCClient()
    bybit = BybitClient(testnet=False)

    valid_symbols = set(await asyncio.to_thread(bybit.get_valid_symbols))   # si falla, el bot no arranca
    print(f"[*] Catálogo Bybit cargado: {len(valid_symbols)} pares Spot.", flush=True)

    ws.connect()
    for t in await store.leer():                                             # si está corrupto, no arranca
        if not t.get("cerrado", False) and t.get("symbol") in valid_symbols:
            ws.subscribe(t["symbol"])

    esc = Escaner(telegram, gemini, ws, bybit, cmc, store, sombra, estado, valid_symbols)
    esc.lanzar_monitor()
    print(f"[*] 🚀 Monitor de posiciones activo (cada {MONITOR_INTERVAL}s)", flush=True)

    while True:
        try:
            espera = await esc.ciclo()
        except Exception as e:
            print(f"Excepción en bucle de orquestación: {e}", flush=True)
            espera = 30
        await asyncio.sleep(espera)


async def dust_sweeper_task(bybit, store):
    """Apagado por defecto (DUST_SWEEPER=1 para activarlo). Convierte saldos de $0.5-$5 a USDC."""
    while True:
        await asyncio.sleep(43200)
        if DRY_RUN:
            continue
        try:
            balances = await asyncio.to_thread(bybit.get_all_balances)
            en_uso = {coin_de(r["symbol"]) for r in await store.leer()
                      if not r.get("cerrado", False) and r.get("symbol")}
            for coin, qty in balances.items():
                if coin in ("USDC", "USDT", "EUR") or coin in en_uso:
                    continue
                par = f"{coin}USDC"
                try:
                    precio = await asyncio.to_thread(bybit.get_current_price, par)
                except BybitError:
                    continue
                if not (0.5 < qty * precio < 5.0):
                    continue
                if await asyncio.to_thread(bybit.get_balance, "USDC") < 10.5:
                    continue
                print(f"[🧹 DUST] {coin} (${qty * precio:.2f}). Limpiando...", flush=True)
                compra = await asyncio.to_thread(bybit.place_spot_order, symbol=par, side="Buy", qty=10.0,
                                                 order_type="Market", market_unit="quoteCoin")
                if not compra:
                    continue
                await asyncio.sleep(3)
                total = (await asyncio.to_thread(bybit.get_all_balances)).get(coin, 0.0)
                await asyncio.to_thread(bybit.place_spot_order, symbol=par, side="Sell", qty=total,
                                        order_type="Market", market_unit="baseCoin")
        except Exception as e:
            print(f"[!] Error en Dust Sweeper: {e}", flush=True)


async def main():
    os.makedirs("logs", exist_ok=True)
    store = JsonListStore("logs/auditoria.json")
    cartera = None
    if DRY_RUN:   # la cartera virtual SOLO existe en modo sombra: nunca interfiere con el modo real
        cartera = CarteraVirtual(
            "logs/cartera_virtual.json", capital_inicial=CAPITAL_VIRTUAL, moneda="USD",
            risk_pct=RISK_PER_TRADE_PCT, max_pct_equity=MAX_PCT_EQUITY_TRADE,
            cash_reserve_pct=CASH_RESERVE_PCT, min_orden=MIN_ORDEN_USDC, max_posiciones=MAX_POSICIONES,
            max_perdida_diaria_pct=MAX_PERDIDA_DIARIA_PCT, fee_lado_pct=FEE_LADO_PCT,
            stop_slippage_pct=STOP_SLIPPAGE_PCT, gap_buffer_pct=GAP_BUFFER_PCT)
    sombra = Sombra("logs/sombra.json", cartera=cartera)
    estado = {"monitor_ok": 0.0}

    telegram = TelegramNotifier()

    async def aviso_cierre(c):
        # Solo notificaciones positivas; las pérdidas quedan en el reporte diario y en /cartera
        if c["pnl_usd"] > 0:
            await avisar(telegram, f"✅ <b>GANANCIA VIRTUAL</b> ({c['outcome']})\n\nPar: {c['symbol']}\n"
                                   f"Resultado: {c['pnl_usd']:+.2f} USD ({c['pnl_pct']:+.2f}%)\n"
                                   f"Saldo cartera: {c['balance_tras']:.2f} USD")
    sombra.on_cierre = aviso_cierre
    gemini = GeminiAgentClient()
    ws = BybitWebSocketClient(testnet=False)

    print("=" * 60, flush=True)
    print("🚀 ANTIGRAVITY MICRO-SCALPING BOT", flush=True)
    print(f"   MODO: {'🧪 SOMBRA (DRY_RUN): no se abren posiciones nuevas' if DRY_RUN else '💰 REAL'}", flush=True)
    if cartera:
        m = cartera.metricas()
        print(f"   💼 Cartera virtual: {m['balance']:.2f} / {m['capital_inicial']:.2f} USD "
              f"({m['pnl_pct']:+.2f}%) | {m['operaciones']} cerradas | {m['abiertas']} abiertas", flush=True)
    print(f"   TP: +{SCALP_TP_PCT}% | SL: -{SCALP_SL_PCT}% | DCA: {DCA_MAX_LEVELS} niveles", flush=True)
    print(f"   Break-Even: +{BREAKEVEN_TRIGGER_PCT}% | Time-Exit: {TIME_EXIT_HOURS}h", flush=True)
    print(f"   Máx. posiciones: {MAX_POSICIONES} | Máx. por operación: {MAX_PCT_EQUITY_TRADE * 100:.0f}% | "
          f"Pérdida diaria máx.: {MAX_PERDIDA_DIARIA_PCT}%", flush=True)
    print("=" * 60, flush=True)

    tareas = [
        orquestador_bucle(telegram, gemini, ws, store, sombra, estado),
        telegram.listen_for_commands(),
        telegram.daily_report_loop(),
        sombra.loop(BybitClient(testnet=False)),
    ]
    if DUST_SWEEPER:
        tareas.append(dust_sweeper_task(BybitClient(testnet=False), store))
    await asyncio.gather(*tareas)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Agente detenido manualmente.", flush=True)

