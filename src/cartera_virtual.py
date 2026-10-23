"""Cartera virtual del modo sombra (paper trading con capital ficticio).

Convierte las señales APROBADAS en DRY_RUN en "posiciones" con dinero simulado, usando EXACTAMENTE
el mismo dimensionamiento que la ejecución real de main.py (riesgo por operación, tope por operación,
reserva de efectivo, orden mínima, máx. posiciones y pérdida diaria máxima). Así el saldo virtual
refleja el interés compuesto, el capital bloqueado y el impacto real de cada ganancia/pérdida.

- Estado persistente en logs/cartera_virtual.json (sobrevive a reinicios).
- El PnL de cada posición sale de la simulación de sombra.py (TP/SL/tiempo, comisiones y slippage).
- Conservador: el capital queda bloqueado hasta que sombra resuelve la señal (~2 h), aunque en la
  realidad un TP temprano lo liberaría antes. Puede omitir alguna señal por "máx. posiciones".
- NUNCA envía órdenes: solo lee/escribe su propio archivo JSON.
"""
from datetime import datetime

from src.store import escribir_json_atomico, leer_estado


class CarteraVirtual:
    def __init__(self, path="logs/cartera_virtual.json", capital_inicial=50.0, moneda="USD", *,
                 risk_pct, max_pct_equity, cash_reserve_pct, min_orden, max_posiciones,
                 max_perdida_diaria_pct, fee_lado_pct, stop_slippage_pct, gap_buffer_pct):
        self.path = path
        self.p = dict(risk_pct=risk_pct, max_pct_equity=max_pct_equity, cash_reserve_pct=cash_reserve_pct,
                      min_orden=min_orden, max_posiciones=max_posiciones,
                      max_perdida_diaria_pct=max_perdida_diaria_pct, fee_lado_pct=fee_lado_pct,
                      stop_slippage_pct=stop_slippage_pct, gap_buffer_pct=gap_buffer_pct)
        self.e = leer_estado(path)
        if not self.e:
            ahora = datetime.now().isoformat()
            self.e = {
                "capital_inicial": float(capital_inicial), "moneda": moneda, "inicio": ahora,
                "balance": float(capital_inicial), "pico": float(capital_inicial), "max_dd_pct": 0.0,
                "abiertas": {}, "cerradas": [],
                "dia": {"fecha": ahora[:10], "equity_inicio": float(capital_inicial)},
                "omitidas": {"max_posiciones": 0, "tamano_minimo": 0, "perdida_diaria": 0},
            }
            self._guardar()

    # ---------------- utilidades ----------------
    def _guardar(self):
        escribir_json_atomico(self.path, self.e)

    def _rodar_dia(self):
        hoy = datetime.now().strftime("%Y-%m-%d")
        if self.e["dia"]["fecha"] != hoy:
            self.e["dia"] = {"fecha": hoy, "equity_inicio": self.e["balance"]}

    @property
    def bloqueado(self) -> float:
        return sum(p["usd"] for p in self.e["abiertas"].values())

    @property
    def disponible(self) -> float:
        return self.e["balance"] - self.bloqueado

    def tiene(self, reg_id) -> bool:
        return reg_id in self.e["abiertas"]

    def dimensionar(self, sl_pct: float) -> float:
        """Misma fórmula que la ejecución real de main.py."""
        eq, p = self.e["balance"], self.p
        loss_f = (sl_pct + 2 * p["fee_lado_pct"] + p["stop_slippage_pct"] + p["gap_buffer_pct"]) / 100
        por_riesgo = eq * p["risk_pct"] / 100 / loss_f if loss_f > 0 else 0.0
        return min(por_riesgo, eq * p["max_pct_equity"], self.disponible * (1 - p["cash_reserve_pct"]))

    # ---------------- ciclo de vida ----------------
    def abrir(self, reg_id: str, symbol: str, precio: float, tp_pct: float, sl_pct: float):
        """Devuelve (posicion, None) si se abre, o (None, motivo) si se omite."""
        self._rodar_dia()
        p = self.p
        if len(self.e["abiertas"]) >= p["max_posiciones"]:
            motivo = "max_posiciones"
        elif self.e["balance"] < self.e["dia"]["equity_inicio"] * (1 - p["max_perdida_diaria_pct"] / 100):
            motivo = "perdida_diaria"
        else:
            usd = self.dimensionar(sl_pct)
            if usd >= p["min_orden"]:
                pos = {"symbol": symbol, "usd": round(usd, 4), "precio": precio, "tp_pct": tp_pct,
                       "sl_pct": sl_pct, "abierta": datetime.now().isoformat()}
                self.e["abiertas"][reg_id] = pos
                self._guardar()
                return pos, None
            motivo = "tamano_minimo"
        self.e["omitidas"][motivo] = self.e["omitidas"].get(motivo, 0) + 1
        self._guardar()
        return None, motivo

    def cerrar(self, reg_id: str, pnl_neto_pct: float, outcome: str):
        """Libera el capital y aplica el PnL. Devuelve el registro de cierre (o None si no existía)."""
        pos = self.e["abiertas"].pop(reg_id, None)
        if pos is None:
            return None
        self._rodar_dia()
        pnl_usd = pos["usd"] * float(pnl_neto_pct) / 100
        self.e["balance"] = round(self.e["balance"] + pnl_usd, 6)
        self.e["pico"] = max(self.e["pico"], self.e["balance"])
        dd = (self.e["balance"] / self.e["pico"] - 1) * 100
        self.e["max_dd_pct"] = round(min(self.e["max_dd_pct"], dd), 4)
        cierre = {"id": reg_id, **pos, "outcome": outcome, "pnl_pct": round(float(pnl_neto_pct), 4),
                  "pnl_usd": round(pnl_usd, 4), "cerrada": datetime.now().isoformat(),
                  "balance_tras": round(self.e["balance"], 4)}
        self.e["cerradas"].append(cierre)
        self._guardar()
        return cierre

    # ---------------- métricas ----------------
    def metricas(self) -> dict:
        c = self.e["cerradas"]
        gan = [x for x in c if x["pnl_usd"] > 0]
        per = [x for x in c if x["pnl_usd"] <= 0]
        bruto_g = sum(x["pnl_usd"] for x in gan)
        bruto_p = -sum(x["pnl_usd"] for x in per)
        ini = self.e["capital_inicial"]
        dias = max((datetime.now() - datetime.fromisoformat(self.e["inicio"])).total_seconds() / 86400, 1e-9)
        return {
            "capital_inicial": ini, "balance": self.e["balance"], "bloqueado": self.bloqueado,
            "disponible": self.disponible, "pnl_usd": self.e["balance"] - ini,
            "pnl_pct": (self.e["balance"] / ini - 1) * 100, "operaciones": len(c),
            "ganadoras": len(gan), "win_rate": len(gan) / len(c) * 100 if c else 0.0,
            "profit_factor": bruto_g / bruto_p if bruto_p > 0 else (float("inf") if bruto_g else 0.0),
            "expectativa_usd": (self.e["balance"] - ini) / len(c) if c else 0.0,
            "max_dd_pct": self.e["max_dd_pct"], "abiertas": len(self.e["abiertas"]),
            "dias": dias, "omitidas": self.e["omitidas"], "moneda": self.e["moneda"],
            "pnl_hoy": self.e["balance"] - self.e["dia"]["equity_inicio"]
            if self.e["dia"]["fecha"] == datetime.now().strftime("%Y-%m-%d") else 0.0,
        }


def texto_cartera(path="logs/cartera_virtual.json") -> str:
    """Resumen HTML para Telegram leyendo solo el archivo de estado (sin dependencias del bot)."""
    e = leer_estado(path)
    if not e:
        return "💼 <b>CARTERA VIRTUAL</b>\n\nAún no inicializada."
    ini, bal = e["capital_inicial"], e["balance"]
    c = e.get("cerradas", [])
    gan = [x for x in c if x["pnl_usd"] > 0]
    g = sum(x["pnl_usd"] for x in gan)
    p = -sum(x["pnl_usd"] for x in c if x["pnl_usd"] <= 0)
    pf = f"{g / p:.2f}" if p > 0 else ("∞" if g else "—")
    bloq = sum(x["usd"] for x in e.get("abiertas", {}).values())
    hoy = datetime.now().strftime("%Y-%m-%d")
    pnl_hoy = bal - e["dia"]["equity_inicio"] if e.get("dia", {}).get("fecha") == hoy else 0.0
    dias = max((datetime.now() - datetime.fromisoformat(e["inicio"])).days, 0)
    m = e.get("moneda", "USD")
    om = e.get("omitidas", {})
    ult = "\n".join(f"• {x['symbol']} {x['outcome']} {x['pnl_usd']:+.2f} {m}" for x in c[-5:]) or "• Ninguna aún"
    return (
        f"💼 <b>CARTERA VIRTUAL (modo sombra)</b>\n\n"
        f"<b>Capital inicial:</b> {ini:.2f} {m}  (desde hace {dias} d)\n"
        f"<b>Saldo actual:</b> {bal:.2f} {m}  ({(bal / ini - 1) * 100:+.2f}%)\n"
        f"<b>Resultado hoy:</b> {pnl_hoy:+.2f} {m}\n"
        f"<b>En posiciones:</b> {bloq:.2f} {m} ({len(e.get('abiertas', {}))} abiertas)\n\n"
        f"<b>Operaciones cerradas:</b> {len(c)} | <b>Acierto:</b> "
        f"{(len(gan) / len(c) * 100 if c else 0):.1f}%\n"
        f"<b>Profit factor:</b> {pf} | <b>Máx. drawdown:</b> {e.get('max_dd_pct', 0):.2f}%\n"
        f"<b>Omitidas</b> (límites de riesgo): {sum(om.values())}\n\n"
        f"<b>Últimas:</b>\n{ult}\n\n"
        f"<i>Dinero ficticio. Mismo cálculo de tamaño que el modo real.</i>"
    )
