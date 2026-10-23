"""
Detector de Flow Exhaustion (Agotamiento de Ventas) en Tiempo Continuo.
Utiliza EWMAs con decaimiento temporal exacto e^(-dt / tau), O(1) por evento, sin buffers.
"""
import math

class Ewma:
    __slots__ = ("tau", "v", "t")
    
    def __init__(self, tau: float):
        self.tau = tau
        self.v = 0.0
        self.t = None

    def decay(self, t: float):
        if self.t is not None:
            self.v = self.v * math.exp(-(t - self.t) / self.tau)
        self.t = t

    def add_count(self, t: float, x: float):
        """Tasa: suma con decay, normalizada a eventos/s."""
        self.decay(t)
        self.v += x / self.tau

    def update_level(self, t: float, x: float):
        """Media de un nivel con alpha según el dt."""
        if self.t is None:
            self.v = x
            self.t = t
            return
        a = 1.0 - math.exp(-(t - self.t) / self.tau)
        self.t = t
        self.v += a * (x - self.v)

class FlowExhaustion:
    def __init__(self, levels: int = 5):
        self.lsfast = Ewma(1.0)
        self.lsslow = Ewma(10.0)
        self.obifast = Ewma(0.5)
        self.obislow = Ewma(5.0)
        self.vmid = Ewma(1.0)
        self.vmp = Ewma(1.0)
        self.midslow = Ewma(30.0)
        self.w = [1.0 / (i + 1) for i in range(levels)]
        self.peakD = 0.0
        self.lastmid = None
        self.lastmp = None

    def on_trade(self, t: float, qty: float, side: int):
        """side = -1 venta agresiva (taker sell), +1 compra agresiva (taker buy)."""
        if side < 0:
            self.lsfast.add_count(t, qty)
        self.lsfast.decay(t)
        self.lsslow.add_count(t, qty if side < 0 else 0.0)
        
        d = self.lsfast.v / max(self.lsslow.v, 1e-12)
        self.peakD = max(self.peakD * 0.999, d)

    def on_book(self, t: float, bids: list, asks: list):
        """bids, asks: listas [(px, sz), ...] ordenadas."""
        if not bids or not asks:
            return

        nb = sum(w * b[1] for w, b in zip(self.w, bids))
        na = sum(w * a[1] for w, a in zip(self.w, asks))
        obi = (nb - na) / max(nb + na, 1e-12)
        
        self.obifast.update_level(t, obi)
        self.obislow.update_level(t, obi)

        (b, vb), (a, va) = bids[0], asks[0]
        mid = (a + b) / 2.0
        mp = (a * vb + b * va) / max(va + vb, 1e-12)

        if self.lastmid is not None:
            self.vmid.update_level(t, mid - self.lastmid)
            self.vmp.update_level(t, mp - self.lastmp)
            
        self.lastmid = mid
        self.lastmp = mp
        self.midslow.update_level(t, mid)

    def signal(self, t: float, sigmapx30s: float, dropk: float = 2.0) -> bool:
        """
        Devuelve True si se cumplen las 4 condiciones simultáneas de agotamiento:
        1. Caída real previa: mid ha caído más de dropk * sigma en 30s.
        2. Desaceleración: la tasa de venta se desploma tras un clímax (peakD > 2 y D < 0.7).
        3. Giro de OBI: el book se inclina al bid (obifast > obislow y > 0).
        4. Divergencia de Micro-price: micro-price frena o sube mientras mid caía.
        """
        if self.lastmid is None or self.midslow.t is None:
            return False

        self.lsfast.decay(t)
        self.lsslow.decay(t)
        d = self.lsfast.v / max(self.lsslow.v, 1e-12)
        
        fell = (self.lastmid - self.midslow.v) < -dropk * sigmapx30s
        decel = self.peakD > 2.0 and d < 0.7
        obiturn = self.obifast.v > self.obislow.v and self.obifast.v > 0.0
        mpdiv = self.vmid.v < 0 and self.vmp.v > self.vmid.v and self.vmp.v >= 0

        if fell and decel and obiturn and mpdiv:
            self.peakD = 0.0  # reset tras disparo
            return True
        return False
