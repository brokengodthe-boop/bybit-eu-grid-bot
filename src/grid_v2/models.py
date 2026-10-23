"""
src/grid_v2/models.py — Modelos de datos del motor de grid trading spot.

Todas las magnitudes monetarias y de cantidad son Decimal (nunca float).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


def now_ms() -> int:
    return int(time.time() * 1000)


class OrderSide(str, Enum):
    BUY = "Buy"
    SELL = "Sell"


class OrderStatus(str, Enum):
    PENDING = "Pending"            # creada localmente, aún no confirmada por el exchange
    NEW = "New"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"

    @property
    def is_active(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


class LotStatus(str, Enum):
    OPEN = "OPEN"              # compra ejecutada, venta pareada pendiente
    CLOSED = "CLOSED"          # venta pareada ejecutada
    LIQUIDATED = "LIQUIDATED"  # cerrado por stop de emergencia (puede tener pérdida)


@dataclass
class InstrumentInfo:
    symbol: str
    base_coin: str
    quote_coin: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    min_order_amt: Decimal  # nocional mínimo en moneda cotizada


@dataclass
class FeeRate:
    symbol: str
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal

    @property
    def maker_fee_bps(self) -> Decimal:
        return self.maker_fee_rate * Decimal("10000")

    @property
    def taker_fee_bps(self) -> Decimal:
        return self.taker_fee_rate * Decimal("10000")


@dataclass
class Order:
    order_link_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    cum_exec_qty: Decimal = Decimal("0")
    cum_exec_value: Decimal = Decimal("0")
    cum_exec_fee: Decimal = Decimal("0")
    fee_coin: str = ""
    lot_id: Optional[str] = None          # para ventas: lote al que pertenece
    step_bps: Decimal = Decimal("0")      # para compras: espaciado vigente al crearla
    created_time: int = field(default_factory=now_ms)
    updated_time: int = field(default_factory=now_ms)

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.cum_exec_qty

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty


@dataclass
class Lot:
    """Inventario nacido de UNA compra (o de los fills parciales acumulados de esa compra)."""
    lot_id: str
    symbol: str
    buy_order_link_id: str
    buy_price: Decimal            # precio medio de ejecución de la compra
    buy_qty: Decimal              # cantidad bruta ejecutada que alimenta este lote
    fee_base_coin: Decimal        # comisión de compra cobrada en moneda base
    net_qty: Decimal              # cantidad vendible (neta de comisión, múltiplo de qtyStep)
    cost_quote: Decimal = Decimal("0")   # coste exacto en moneda cotizada de net_qty
    sell_order_link_id: Optional[str] = None
    target_sell_price: Decimal = Decimal("0")
    executed_sell_price: Optional[Decimal] = None
    sell_fee: Decimal = Decimal("0")
    status: LotStatus = LotStatus.OPEN
    gross_profit: Decimal = Decimal("0")
    net_profit: Decimal = Decimal("0")
    opened_at: int = field(default_factory=now_ms)
    closed_at: Optional[int] = None

    @property
    def unit_cost(self) -> Decimal:
        return self.cost_quote / self.net_qty if self.net_qty > 0 else Decimal("0")


@dataclass
class GridMetrics:
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    total_fees_quote: Decimal = Decimal("0")   # comisiones de venta (cotizada) + compra valorada al precio de compra
    cash_balance: Decimal = Decimal("0")
    mark_to_market_equity: Decimal = Decimal("0")
    completed_cycles: int = 0
    liquidated_lots: int = 0
    total_fills: int = 0
