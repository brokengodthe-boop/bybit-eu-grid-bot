"""
Simulador de Órdenes Virtuales y Dinámica de Cola (VirtualOrder).
Modela la posición en la cola (ahead) usando orderbook.1 y publicTrade
con tres variantes de descomposición de cancelaciones:
1. Optimista: todas las cancelaciones ocurren por delante de nuestra orden.
2. Pesimista: todas las cancelaciones ocurren por detrás.
3. Pro-rata con potencia (Moallemi / Rigtorp): frac = (ahead / S_prev) ** k.
"""

class VirtualOrder:
    __slots__ = ("px", "qty", "ahead", "model", "k", "filled", "ahead0", "S0")

    def __init__(self, px: float, qty: float, ahead: float, S0: float = 0.0, model: str = "prorata", k: float = 1.0):
        self.px = px
        self.qty = qty
        self.ahead = max(0.0, ahead)
        self.ahead0 = ahead
        self.S0 = S0
        self.model = model
        self.k = k
        self.filled = 0.0

    @property
    def is_filled(self) -> bool:
        return self.filled >= self.qty

    def on_interval(self, Sprev: float, Snow: float, traded: float, through_trade: bool = False):
        """
        Sprev: tamaño del nivel en el snapshot anterior de orderbook.1
        Snow: tamaño del nivel en el snapshot actual
        traded: volumen de trades agresivos que golpearon nuestro nivel en este intervalo
        through_trade: True si hubo un trade a precio peor que el nuestro (barrida total del nivel)
        """
        if self.is_filled:
            return

        # 1) Si hubo un trade a precio peor que el nuestro, nuestro nivel fue completamente barrido -> Fill seguro
        if through_trade:
            self.filled = self.qty
            self.ahead = 0.0
            return

        # 2) Los trades consumen primero la cabeza de la cola (FIFO)
        if traded > 0:
            eat = min(traded, self.ahead)
            self.ahead -= eat
            rest = traded - eat
            if rest > 0:
                needed = self.qty - self.filled
                fill_qty = min(rest, needed)
                self.filled += fill_qty

        # 3) Cancelaciones: si el tamaño disminuyó más de lo explicado por los trades
        # C = max(Sprev - Snow - traded, 0)
        C = max(Sprev - Snow - traded, 0.0)
        if C > 0 and Sprev > 0 and self.ahead > 0:
            if self.model == "optimistic":
                frac = 1.0                              # todas por delante
            elif self.model == "pessimistic":
                frac = 0.0                              # todas por detrás
            else:
                # Pro-rata con exponente k
                x = min(1.0, max(0.0, self.ahead / Sprev))
                frac = x ** self.k
            self.ahead = max(self.ahead - C * frac, 0.0)
