"""
filters.py — Filtros Cuantitativos Avanzados para Antigravity.
Usa float para señales (velocidad); el Ledger sigue en Decimal.
"""
from __future__ import annotations
import math
from collections import deque
from dataclasses import dataclass

# ---------------------------------------------------------------- OBI
class OBIFilter:
    def __init__(self, levels: int = 10, decay: float = 0.3,
                 window_ms: int = 500, min_obi: float = -0.1,
                 min_recovery: float = 0.15):
        self.levels = levels
        self.w = [math.exp(-decay * i) for i in range(levels)]
        self.window_ms = window_ms
        self.min_obi = min_obi
        self.min_recovery = min_recovery
        self.hist: deque[tuple[int, float]] = deque()  # (ts_ms, obi)

    def compute(self, bids: list[tuple[float, float]],
                asks: list[tuple[float, float]]) -> float:
        """bids/asks: [(price, size)] ordenados desde el top of book."""
        n = min(self.levels, len(bids), len(asks))
        if n == 0:
            return 0.0
        wb = sum(self.w[i] * bids[i][1] for i in range(n))
        wa = sum(self.w[i] * asks[i][1] for i in range(n))
        tot = wb + wa
        return (wb - wa) / tot if tot > 0 else 0.0

    def update(self, ts_ms: int, bids, asks) -> float:
        obi = self.compute(bids, asks)
        self.hist.append((ts_ms, obi))
        cutoff = ts_ms - self.window_ms
        while self.hist and self.hist[0][0] < cutoff:
            self.hist.popleft()
        return obi

    def recovery(self) -> float:
        if not self.hist:
            return 0.0
        cur = self.hist[-1][1]
        return cur - min(o for _, o in self.hist)

    def allow_long(self) -> tuple[bool, dict]:
        if not self.hist:
            return False, {"reason": "no_obi"}
        obi, rec = self.hist[-1][1], self.recovery()
        ok = obi > self.min_obi and rec > self.min_recovery
        return ok, {"obi": obi, "obi_rec": rec}

# ------------------------------------------------- Volatilidad realizada
class EWMAVol:
    def __init__(self, tau_ms: float, sample_ms: int):
        self.alpha = 1.0 - math.exp(-sample_ms / tau_ms)
        self.var: float | None = None

    def update(self, r: float) -> None:
        r2 = r * r
        self.var = r2 if self.var is None else self.alpha * r2 + (1 - self.alpha) * self.var

    @property
    def sigma(self) -> float:
        return math.sqrt(self.var) if self.var else 0.0

class RealizedVolFilter:
    """Muestrea el mid en una rejilla fija de sample_ms (sin sesgo por tick)."""
    def __init__(self, sample_ms: int = 100, tau_short_ms: float = 5000,
                 tau_long_ms: float = 300000, max_ratio: float = 2.0,
                 warmup_samples: int = 600):
        self.sample_ms = sample_ms
        self.short = EWMAVol(tau_short_ms, sample_ms)
        self.long = EWMAVol(tau_long_ms, sample_ms)
        self.max_ratio = max_ratio
        self.warmup = warmup_samples
        self.n = 0
        self.last_bucket: int | None = None
        self.last_mid: float | None = None

    def update(self, ts_ms: int, mid: float) -> bool:
        """Devuelve True si se ha cerrado una nueva muestra."""
        bucket = ts_ms // self.sample_ms
        if self.last_bucket is None:
            self.last_bucket, self.last_mid = bucket, mid
            return False
        if bucket == self.last_bucket:
            return False
        
        # Evitar log(0) o retornos extremos por reinicios
        if mid <= 0 or self.last_mid <= 0:
            return False
            
        r = math.log(mid / self.last_mid)
        # si se saltaron buckets (sin updates), el retorno se reparte en varias muestras
        gaps = bucket - self.last_bucket
        r_per = r / math.sqrt(gaps)
        for _ in range(min(gaps, 50)):
            self.short.update(r_per)
            self.long.update(r_per)
            self.n += 1
        self.last_bucket, self.last_mid = bucket, mid
        return True

    @property
    def ratio(self) -> float:
        sl = self.long.sigma
        return self.short.sigma / sl if sl > 0 else float("inf")

    def allow(self) -> tuple[bool, dict]:
        if self.n < self.warmup:
            return False, {"reason": "vol_warmup"}
        ok = self.ratio <= self.max_ratio
        return ok, {"vol_s": self.short.sigma, "vol_l": self.long.sigma,
                    "vol_ratio": self.ratio}

# ---------------------------------------------------- Tendencia de fondo
class TrendFilter:
    def __init__(self, vol: RealizedVolFilter, tau_ms: float = 600000,
                 horizon_ms: int = 300000, min_t: float = -1.5):
        self.vol = vol
        self.alpha = 1.0 - math.exp(-vol.sample_ms / tau_ms)
        self.horizon_samples = horizon_ms // vol.sample_ms
        self.ema: float | None = None
        self.ema_hist: deque[float] = deque(maxlen=self.horizon_samples + 1)
        self.min_t = min_t

    def on_sample(self, mid: float) -> None:
        """Llamar solo cuando RealizedVolFilter.update() devuelva True."""
        self.ema = mid if self.ema is None else self.alpha * mid + (1 - self.alpha) * self.ema
        self.ema_hist.append(self.ema)

    @property
    def t_stat(self) -> float:
        if len(self.ema_hist) <= self.horizon_samples:
            return 0.0
        sl = self.vol.long.sigma
        if sl == 0:
            return 0.0
        drift = math.log(self.ema_hist[-1] / self.ema_hist[0])
        return drift / (sl * math.sqrt(self.horizon_samples))

    def allow_long(self) -> tuple[bool, dict]:
        if len(self.ema_hist) <= self.horizon_samples:
            return False, {"reason": "trend_warmup"}
        t = self.t_stat
        return t >= self.min_t, {"trend_t": t}

# ------------------------------------------------------- Puerta de señal
@dataclass
class GateDecision:
    allow: bool
    blocked_by: list[str]
    features: dict

class SignalGate:
    def __init__(self, obi: OBIFilter, vol: RealizedVolFilter, trend: TrendFilter):
        self.obi = obi
        self.vol = vol
        self.trend = trend

    def on_book(self, ts_ms: int, bids, asks) -> None:
        if not bids or not asks:
            return
        self.obi.update(ts_ms, bids, asks)
        mid = 0.5 * (bids[0][0] + asks[0][0])
        if self.vol.update(ts_ms, mid):
            self.trend.on_sample(mid)

    def evaluate_long(self) -> GateDecision:
        feats, blocked = {}, []
        for name, (ok, f) in (("obi", self.obi.allow_long()),
                              ("vol", self.vol.allow()),
                              ("trend", self.trend.allow_long())):
            feats.update(f)
            if not ok:
                blocked.append(name)
        return GateDecision(not blocked, blocked, feats)
