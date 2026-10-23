from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List

class State(Enum):
    FLAT = auto()
    WORKING_ENTRY = auto()
    IN_POSITION = auto()
    WORKING_EXIT = auto()

@dataclass(frozen=True)
class Fill:
    ts: int
    fold: int
    finalist_id: str
    side: int            # +1 (comprador) o -1 (vendedor)
    px: float
    qty: float
    liquidity: str       # 'M' para Maker, 'T' para Taker
    fee: float           # positivo = pago, negativo = rebate
    order_id: str
    reason: str          # ENTRY / EXIT_PASSIVE / EXIT_TAKER / FOLD_CLOSE

class Ledger:
    def __init__(self, initial_capital: float = 1000.0):
        self.initial_capital = initial_capital
        self.fills: List[Fill] = []
        
        # Derivados
        self.position: float = 0.0
        self.avg_entry: float = 0.0
        self.cash: float = initial_capital
        self.realized_pnl: float = 0.0
        self.fees_paid: float = 0.0

    def add_fill(self, f: Fill):
        self.fills.append(f)
        self.fees_paid += f.fee
        self.cash -= f.fee
        
        if self.position == 0:
            # Abriendo posición
            self.position = f.qty * f.side
            self.avg_entry = f.px
            # Al comprar, descontamos efectivo. Al vender en corto, recibimos efectivo
            self.cash -= (f.qty * f.px * f.side)
        else:
            # Cerrando o aumentando posición
            # Para este bot de max_pos=1, asumimos que siempre es cierre
            if (self.position > 0 and f.side < 0) or (self.position < 0 and f.side > 0):
                # Es un cierre
                closed_qty = min(abs(self.position), f.qty)
                pnl = closed_qty * (f.px - self.avg_entry) if self.position > 0 else closed_qty * (self.avg_entry - f.px)
                self.realized_pnl += pnl
                
                self.position += (f.qty * f.side)
                self.cash -= (f.qty * f.px * f.side)
                
                if self.position == 0:
                    self.avg_entry = 0.0
            else:
                # Piramidar (no soportado en max_pos=1, pero lo modelamos para estar seguros)
                new_pos = self.position + (f.qty * f.side)
                total_val = abs(self.position) * self.avg_entry + f.qty * f.px
                self.avg_entry = total_val / abs(new_pos)
                self.position = new_pos
                self.cash -= (f.qty * f.px * f.side)

    def equity(self, mid_px: float) -> float:
        """Mark-to-Market actual"""
        return self.cash + (self.position * mid_px)

    def check_invariant(self, mid_px: float):
        # equity == capital_inicial + realized + unrealized - fees + funding
        unrealized = self.position * (mid_px - self.avg_entry) if self.position > 0 else abs(self.position) * (self.avg_entry - mid_px)
        expected = self.initial_capital + self.realized_pnl + unrealized - self.fees_paid
        actual = self.equity(mid_px)
        assert abs(actual - expected) < 1e-5, f"Invariante falló: {actual} != {expected}"
