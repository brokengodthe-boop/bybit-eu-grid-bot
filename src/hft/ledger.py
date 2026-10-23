from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal as D
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class Liquidity(Enum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(frozen=True)
class FeeSchedule:
    maker: D = D("0.001")   # AJUSTA a tu tier real
    taker: D = D("0.001")   # AJUSTA a tu tier real
    fee_on_buy_in_base: bool = True

    def rate(self, liq: Liquidity) -> D:
        return self.maker if liq is Liquidity.MAKER else self.taker


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    ts: int                 # timestamp del evento (ms), nunca reloj de pared
    kind: str               # RESERVE | RELEASE | FILL
    order_id: str
    side: Side
    price: D
    qty: D                  # cantidad base
    fee: D                  # en fee_asset
    fee_asset: str          # "quote" | "base"
    liquidity: Liquidity | None
    ref_mid: D | None       # mid del libro en el momento (para medir coste)
    ref_best: D | None      # mejor precio contrario (ask si compras)


class LedgerError(Exception):
    pass


@dataclass
class Ledger:
    fees: FeeSchedule
    quote_free: D
    base_free: D = D(0)
    quote_reserved: D = D(0)
    base_reserved: D = D(0)
    entries: list[LedgerEntry] = field(default_factory=list)
    # order_id -> [side, limit_price, qty_restante, reserva_restante]
    _orders: dict[str, list] = field(default_factory=dict)
    _seq: int = 0

    # ---------- helpers ----------
    def _log(self, **kw) -> LedgerEntry:
        self._seq += 1
        e = LedgerEntry(seq=self._seq, **kw)
        self.entries.append(e)
        return e

    def _check(self) -> None:
        for v in (self.quote_free, self.quote_reserved,
                  self.base_free, self.base_reserved):
            if v < 0:
                raise LedgerError(f"Saldo negativo: {self}")

    # ---------- reservas ----------
    def reserve(self, ts: int, order_id: str, side: Side,
                price: D, qty: D) -> None:
        if order_id in self._orders:
            raise LedgerError(f"order_id duplicado {order_id}")
        if side is Side.BUY:
            # reserva con margen para la comisión taker si se llenara como taker
            need = price * qty
            if not self.fees.fee_on_buy_in_base:
                need *= (1 + self.fees.taker)
            if self.quote_free < need:
                raise LedgerError("Efectivo libre insuficiente")
            self.quote_free -= need
            self.quote_reserved += need
            held = need
        else:
            if self.base_free < qty:
                raise LedgerError("Inventario base insuficiente")
            self.base_free -= qty
            self.base_reserved += qty
            held = qty
        self._orders[order_id] = [side, price, qty, held]
        self._log(ts=ts, kind="RESERVE", order_id=order_id, side=side,
                  price=price, qty=qty, fee=D(0), fee_asset="",
                  liquidity=None, ref_mid=None, ref_best=None)
        self._check()

    def release(self, ts: int, order_id: str) -> None:
        """Cancelación: devuelve lo no usado a libre."""
        side, price, qty_left, held = self._orders.pop(order_id)
        if side is Side.BUY:
            self.quote_reserved -= held
            self.quote_free += held
        else:
            self.base_reserved -= held
            self.base_free += held
        self._log(ts=ts, kind="RELEASE", order_id=order_id, side=side,
                  price=price, qty=qty_left, fee=D(0), fee_asset="",
                  liquidity=None, ref_mid=None, ref_best=None)
        self._check()

    # ---------- fills ----------
    def fill(self, ts: int, order_id: str, fill_price: D, fill_qty: D,
             liquidity: Liquidity, ref_mid: D | None = None,
             ref_best: D | None = None) -> LedgerEntry:
        o = self._orders.get(order_id)
        if o is None:
            raise LedgerError(f"Orden desconocida {order_id}")
        side, limit, qty_left, held = o
        if fill_qty > qty_left:
            raise LedgerError("Fill mayor que lo pendiente")
        rate = self.fees.rate(liquidity)
        notional = fill_price * fill_qty

        if side is Side.BUY:
            if fill_price > limit:
                raise LedgerError("Fill por encima del límite")
            # se consume reserva proporcional al precio LÍMITE reservado
            per_unit = held / qty_left
            used = per_unit * fill_qty
            self.quote_reserved -= used
            if self.fees.fee_on_buy_in_base:
                fee, fee_asset = fill_qty * rate, "base"
                self.base_free += fill_qty - fee
                self.quote_free += used - notional   # mejora de precio
            else:
                fee, fee_asset = notional * rate, "quote"
                self.base_free += fill_qty
                self.quote_free += used - notional - fee
            o[3] = held - used
        else:
            if fill_price < limit:
                raise LedgerError("Fill por debajo del límite")
            self.base_reserved -= fill_qty
            fee, fee_asset = notional * rate, "quote"
            self.quote_free += notional - fee
            o[3] = held - fill_qty

        o[2] = qty_left - fill_qty
        if o[2] == 0:
            # residuo de reserva (si lo hay) vuelve a libre
            if side is Side.BUY and o[3] > 0:
                self.quote_reserved -= o[3]; self.quote_free += o[3]
            del self._orders[order_id]

        e = self._log(ts=ts, kind="FILL", order_id=order_id, side=side,
                      price=fill_price, qty=fill_qty, fee=fee,
                      fee_asset=fee_asset, liquidity=liquidity,
                      ref_mid=ref_mid, ref_best=ref_best)
        self._check()
        return e

    # ---------- valoración y métricas ----------
    def equity(self, mid: D) -> D:
        return (self.quote_free + self.quote_reserved
                + (self.base_free + self.base_reserved) * mid)

    def entry_cost_bps(self, e: LedgerEntry) -> dict[str, D]:
        """Coste de una entrada/salida respecto al mid, en puntos básicos."""
        if e.ref_mid is None:
            raise LedgerError("Sin ref_mid")
        sign = D(1) if e.side is Side.BUY else D(-1)
        slippage = sign * (e.price - e.ref_mid) / e.ref_mid * D(10000)
        notional = e.price * e.qty
        fee_quote = e.fee * (e.price if e.fee_asset == "base" else 1)
        fee_bps = fee_quote / notional * D(10000)
        return {"slippage_vs_mid_bps": slippage, "fee_bps": fee_bps,
                "total_bps": slippage + fee_bps}
