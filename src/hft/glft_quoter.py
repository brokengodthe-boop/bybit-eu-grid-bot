"""
Cotizador Óptimo de Control Estocástico GLFT (Guéant-Lehalle-Fernández-Tapia).
Formulación asintótica de horizonte infinito en ticks discretos sin dependencias temporales arbitrarias.
"""
import math
from decimal import Decimal
from dataclasses import dataclass
from typing import Tuple, Optional

class GLFTAsymptoticQuoter:
    __slots__ = ("tick", "tickdec", "gamma", "k", "A", "floor", "c1", "c2base", "maxq")

    def __init__(self, ticksize: str, gamma: float, k: float, A: float,
                 spreadfloorticks: int = 1, maxq: int = 5):
        self.tickdec = Decimal(ticksize)            # Pasar como str: "0.01", "0.0001", etc.
        self.tick = float(self.tickdec)
        self.floor = spreadfloorticks
        self.maxq = maxq
        self.set_params(gamma, k, A)

    def set_params(self, gamma: float, k: float, A: float):
        self.gamma, self.k, self.A = gamma, k, A
        r = gamma / k
        self.c1 = math.log1p(r) / gamma
        # c2 = sigma * sqrt(gamma/(2kA) * (1+r)^(1+k/gamma)); se calcula en espacio logarítmico para estabilidad numérica
        self.c2base = math.sqrt(math.exp(math.log(gamma / (2.0 * k * A)) + (1.0 + 1.0 / r) * math.log1p(r)))

    def quote(self, mid: float, q: float, sigma: float) -> Tuple[Optional[str], Optional[str]]:
        """
        mid: precio medio en $ (float)
        q: inventario actual en clips con signo (float/int)
        sigma: volatilidad en ticks/sqrt(s) (float)
        Devuelve (bid_px_str, ask_px_str) o None por cada lado si supera el límite de inventario maxq.
        """
        c2 = sigma * self.c2base
        db = self.c1 + (2.0 * q + 1.0) * 0.5 * c2          # delta bid en ticks
        da = self.c1 - (2.0 * q - 1.0) * 0.5 * c2          # delta ask en ticks

        midt = mid / self.tick                           # mid expresado en ticks (float)
        bidt = math.floor(midt - db + 1e-9)             # redondeo conservador alejándose del mid
        askt = math.ceil(midt + da - 1e-9)
        
        # Suelo de spread simétrico
        if askt - bidt < self.floor:
            c = (askt + bidt) / 2.0
            bidt = math.floor(c - self.floor / 2.0)
            askt = bidt + self.floor

        bid = None if q >= self.maxq else self.px(bidt)
        ask = None if q <= -self.maxq else self.px(askt)
        return bid, ask

    def px(self, nticks: int) -> str:
        """Precio exacto formateado como string para la API: entero de ticks * Decimal(tick)."""
        return str((Decimal(nticks) * self.tickdec).normalize())

# ============================================================================
# COMPATIBILIDAD CON ENGINE ANTERIOR
# ============================================================================
@dataclass
class GLFTParams:
    gamma: float      # aversión al riesgo (1/ticks)
    A: float          # intensidad base de fills (1/s)
    k: float          # decaimiento de fills (1/tick)
    beta: float       # shrinkage del alpha (0.3 a 0.8)
    fee: float        # fee maker en ticks (negativo si hay rebate)
    q_soft: float     # Inventario suave (solo cotiza reducción)
    q_hard: float     # Inventario duro (cierre taker de emergencia)
    tox_kill: float   # umbral de markout adverso para retirar pata

def quote_glft(micro: float, q: float, sigma: float, alpha: float, 
               tox_b: float, tox_a: float, p: GLFTParams) -> Tuple[Optional[int], Optional[int]]:
    g, k, A = p.gamma, p.k, p.A
    r = g / k
    c1 = math.log1p(r) / g
    c2 = math.sqrt(math.exp(math.log(g / (2.0 * k * A)) + (1.0 + 1.0 / r) * math.log1p(r))) * sigma
    
    half_spread_base = c1 + 0.5 * c2
    shift = max(-half_spread_base, min(half_spread_base, p.beta * alpha))
    fair = micro + shift

    db = half_spread_base + q * c2
    da = half_spread_base - q * c2

    db = max(db, p.fee + tox_b)
    da = max(da, p.fee + tox_a)

    bid = math.floor(fair - db)
    ask = math.ceil(fair + da)

    if q >= p.q_soft or tox_b > p.tox_kill:
        bid = None
    if q <= -p.q_soft or tox_a > p.tox_kill:
        ask = None
        
    return bid, ask
