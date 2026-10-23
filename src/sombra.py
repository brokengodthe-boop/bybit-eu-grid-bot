"""Modo sombra: registra cada señal (aprobada o vetada por la IA) y, 2 horas después,
simula qué habría pasado con TP / SL / salida por tiempo usando velas de 1 minuto,
descontando comisiones y slippage. Sirve para saber si la estrategia tiene ventaja
SIN arriesgar dinero.

Limitaciones (dilo tal cual al interpretar resultados):
- Asume que la orden de entrada se llena al precio de referencia (optimista).
- Simula solo TP/SL/tiempo: ignora trailing, break-even y DCA.
- Si TP y SL caen en la misma vela de 1 min, asume SL primero (conservador).
"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta

from src.store import JsonListStore, clave

HORIZONTES_MIN = (15, 30, 60, 120)
HORIZONTE_SIM_MIN = 120
FEE_LADO_PCT = 0.10          # comisión por lado (ajusta a tu tarifa real de Bybit)
SLIPPAGE_SALIDA_PCT = 0.05   # deslizamiento supuesto en salidas a mercado (SL / tiempo)


def simular(entry, tp_pct, sl_pct, velas, ts_senal_ms, horizonte_min=HORIZONTE_SIM_MIN,
            fee_lado_pct=FEE_LADO_PCT, slip_pct=SLIPPAGE_SALIDA_PCT):
    """velas: [[ts_ms, open, high, low, close], ...] ascendentes. None si no hay velas."""
    tp = entry * (1 + tp_pct / 100)
    sl = entry * (1 - sl_pct / 100)
    fin = ts_senal_ms + horizonte_min * 60_000
    ventana = [v for v in velas if ts_senal_ms <= v[0] < fin]
    if not ventana:
        return None
    
    # Si Bybit no devuelve suficientes velas (ej. faltan más del 20%), no simular TIME
    if len(ventana) < (horizonte_min * 0.8):
        return None

    mfe = mae = 0.0
    salida = None
    for ts, o, h, l, c, *_ in ventana:
        mfe = max(mfe, (h / entry - 1) * 100)
        mae = min(mae, (l / entry - 1) * 100)
        if l <= sl:                       # SL primero si ambos caen en la misma vela
            px = min(o, sl) * (1 - slip_pct / 100)
            salida = ("SL", px, ts)
            break
        if h >= tp:
            salida = ("TP", tp, ts)
            break
    if salida is None:
        salida = ("TIME", ventana[-1][4] * (1 - slip_pct / 100), ventana[-1][0])

    outcome, px, ts_sal = salida
    bruto = (px / entry - 1) * 100
    neto = bruto - 2 * fee_lado_pct
    return {
        "outcome": outcome,
        "exit_price": px,
        "pnl_bruto_pct": round(bruto, 4),
        "pnl_neto_pct": round(neto, 4),
        "minutos": round((ts_sal - ts_senal_ms) / 60_000, 1),
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
    }


def retornos_horizonte(entry, velas, ts_senal_ms, horizontes=HORIZONTES_MIN):
    """Retorno bruto (%) al cierre de la última vela dentro de cada horizonte."""
    out = {}
    for h in horizontes:
        limite = ts_senal_ms + h * 60_000
        previas = [v for v in velas if ts_senal_ms <= v[0] < limite]
        if previas:
            out[f"ret_{h}m"] = round((previas[-1][4] / entry - 1) * 100, 4)
    return out


class Sombra:
    def __init__(self, path="logs/sombra.json", cooldown_min=120, cartera=None):
        self.store = JsonListStore(path)
        self.cartera = cartera          # CarteraVirtual opcional (solo DRY_RUN)
        self.on_cierre = None           # callback async(cierre_dict) para avisos
        self.cooldown = timedelta(minutes=cooldown_min)
        self._ultimo = {}
        self._cargado = False

    async def _cargar(self):
        if self._cargado:
            return
        for r in await self.store.leer():
            try:
                t = datetime.fromisoformat(r["timestamp"])
            except (KeyError, ValueError):
                continue
            s = r.get("symbol")
            if s and (s not in self._ultimo or t > self._ultimo[s]):
                self._ultimo[s] = t
        self._cargado = True

    async def en_cooldown(self, symbol: str) -> bool:
        await self._cargar()
        t = self._ultimo.get(symbol)
        return t is not None and datetime.now() - t < self.cooldown

    async def registrar(self, symbol: str, precio_ref: float, tp_pct: float, sl_pct: float, **extra):
        await self._cargar()
        ahora = datetime.now()
        reg = {
            "id": uuid.uuid4().hex[:12],
            "timestamp": ahora.isoformat(),
            "ts_ms": int(time.time() * 1000),
            "symbol": symbol,
            "precio_ref": precio_ref,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "resuelto": False,
            **extra,
        }
        await self.store.agregar(reg)
        self._ultimo[symbol] = ahora
        return reg["id"]

    async def _cerrar_virtual(self, reg_id, pnl_pct, outcome):
        if not self.cartera or not self.cartera.tiene(reg_id):
            return
        cierre = self.cartera.cerrar(reg_id, pnl_pct, outcome)
        if cierre:
            print(f"[💼 CARTERA] Cierre {cierre['symbol']} {outcome}: {cierre['pnl_usd']:+.2f} | "
                  f"saldo {cierre['balance_tras']:.2f}", flush=True)
            await self.store.actualizar(reg_id, {"usd_virtual_pnl": cierre["pnl_usd"],
                                                 "balance_virtual_tras": cierre["balance_tras"]})
            if self.on_cierre:
                try:
                    await self.on_cierre(cierre)
                except Exception as e:
                    print(f"[!] Aviso de cierre virtual falló: {e}", flush=True)

    async def resolver_pendientes(self, bybit):
        ahora_ms = int(time.time() * 1000)
        for r in await self.store.leer():
            if r.get("resuelto"):
                continue
            
            # Usar ts_ms_eval (snapshot antes del modelo) si existe; si no, ts_ms
            ts0 = r.get("ts_ms_eval", r["ts_ms"])
            
            if ahora_ms < ts0 + (HORIZONTE_SIM_MIN + 2) * 60_000:
                continue
            try:
                velas = await asyncio.to_thread(
                    bybit.get_klines_raw, r["symbol"], "1", ts0, ts0 + HORIZONTE_SIM_MIN * 60_000)
            except Exception as e:
                print(f"[SOMBRA] No se pudieron leer velas de {r['symbol']}: {e}", flush=True)
                continue
            entry = r["precio_ref"]
            sim = simular(entry, r["tp_pct"], r["sl_pct"], velas, ts0)
            if sim is None:
                if ahora_ms > ts0 + 24 * 3600 * 1000:
                    await self.store.actualizar(clave(r), {"resuelto": True, "outcome": "SIN_DATOS"})
                    await self._cerrar_virtual(clave(r), 0.0, "SIN_DATOS")   # libera capital sin PnL
                continue
            cambios = {"resuelto": True, **sim, **retornos_horizonte(entry, velas, ts0)}
            await self.store.actualizar(clave(r), cambios)
            await self._cerrar_virtual(clave(r), sim["pnl_neto_pct"], sim["outcome"])

    async def loop(self, bybit, intervalo_seg=300):
        while True:
            try:
                await self.resolver_pendientes(bybit)
            except Exception as e:
                print(f"[!] Error en modo sombra: {e}", flush=True)
            await asyncio.sleep(intervalo_seg)
