from __future__ import annotations
from dataclasses import dataclass

Level = tuple[float, float]  # (precio, cantidad)

@dataclass(slots=True, frozen=True)
class DepthDiff:
    symbol: str
    first_id: int      # U
    final_id: int      # u
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    exch_ts: int       # ms, exchange
    recv_ns: int       # ns, local

@dataclass(slots=True, frozen=True)
class Trade:
    symbol: str
    trade_id: int
    price: float
    qty: float
    buyer_is_maker: bool   # True => agresor vendedor
    exch_ts: int
    recv_ns: int

@dataclass(slots=True, frozen=True)
class Snapshot:
    symbol: str
    last_update_id: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    recv_ns: int

@dataclass(slots=True, frozen=True)
class Disconnect:
    """La conexión se cayó: todos los libros quedan inválidos."""
    recv_ns: int

Event = DepthDiff | Trade | Snapshot | Disconnect
