from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import random
from decimal import Decimal as D
from typing import Optional

class Side(Enum):
    BUY = 1
    SELL = -1

class OStatus(Enum):
    PENDING_NEW = 0   # en vuelo (latencia de envío)
    OPEN = 1
    PENDING_CANCEL = 2
    FILLED = 3
    CANCELED = 4

@dataclass
class LimitOrder:
    oid: str
    side: Side
    price: float
    qty: float
    tsubmit: int                 # ms
    tactive: int                 # tsubmit + latencia de envío
    status: OStatus = OStatus.PENDING_NEW
    filled: float = 0.0
    queue_ahead: float = 0.0
    tcancel_effective: int | None = None
    fills: list = field(default_factory=list)   # [(ts, px, qty)]

    @property
    def remaining(self) -> float:
        return self.qty - self.filled

class ExecModel:
    def __init__(self, ledger, book, maker_fee: D = D("0.001"), taker_fee: D = D("0.001"),
                 lat_submit_ms: int = 15, lat_cancel_ms: int = 15,
                 queue_model: str = "riskaverse", prob_n: float = 2.0, seed: int = 0):
        self.ledger = ledger
        self.book = book
        self.maker_fee = float(maker_fee)
        self.taker_fee = float(taker_fee)
        self.lat_submit = lat_submit_ms
        self.lat_cancel = lat_cancel_ms
        self.queue_model = queue_model
        self.n = prob_n
        self.orders: dict[str, LimitOrder] = {}
        self.traded_at: dict[tuple[Side, float], float] = {}   # (side, px) -> vol negociado desde el último delta
        self.rng = random.Random(seed)

    # ---------- API ----------
    def limit_buy(self, price: float, qty: float, ts: int, oid: str) -> str:
        return self.submit(Side.BUY, price, qty, ts, oid)

    def limit_sell(self, price: float, qty: float, ts: int, oid: str) -> str:
        return self.submit(Side.SELL, price, qty, ts, oid)

    def cancel(self, oid: str, ts: int) -> None:
        o = self.orders.get(oid)
        if o and o.status in (OStatus.OPEN, OStatus.PENDING_NEW):
            o.status = OStatus.PENDING_CANCEL
            o.tcancel_effective = ts + self.lat_cancel

    def submit(self, side: Side, price: float, qty: float, ts: int, oid: str) -> str:
        o = LimitOrder(oid, side, price, qty, ts, ts + self.lat_submit)
        self.orders[o.oid] = o
        return o.oid

    # ---------- reloj ----------
    def on_time(self, ts: int) -> None:
        for o in self.orders.values():
            if o.status == OStatus.PENDING_NEW and ts >= o.tactive:
                self.activate(o, ts)
            elif o.status == OStatus.PENDING_CANCEL and o.tcancel_effective is not None and ts >= o.tcancel_effective:
                o.status = OStatus.CANCELED

    def activate(self, o: LimitOrder, ts: int) -> None:
        best_ask = self.book.best_ask()
        best_bid = self.book.best_bid()
        ba_px = best_ask[0] if best_ask else float('inf')
        bb_px = best_bid[0] if best_bid else 0.0

        # Post-only: si cruzaría el spread, se rechaza
        if (o.side == Side.BUY and o.price >= ba_px) or \
           (o.side == Side.SELL and o.price <= bb_px):
            o.status = OStatus.CANCELED
            return
            
        # queue_ahead = tamaño visible en tu precio en ese momento
        # size_at returns the total quantity at a given price level
        qa = 0.0
        if o.side == Side.BUY:
            for p, q in self.book.bids_iter():
                if p == o.price:
                    qa = q
                    break
        else:
            for p, q in self.book.asks_iter():
                if p == o.price:
                    qa = q
                    break

        o.queue_ahead = qa
        o.status = OStatus.OPEN

    # ---------- trades públicos ----------
    def on_trade(self, ts: int, price: float, qty: float, buyer_is_maker: bool) -> None:
        """
        buyer_is_maker=True -> el agresor fue VENDEDOR (golpeó Bids).
        buyer_is_maker=False -> el agresor fue COMPRADOR (golpeó Asks).
        """
        self.on_time(ts)
        aggressor = Side.SELL if buyer_is_maker else Side.BUY
        passive = Side.BUY if aggressor == Side.SELL else Side.SELL
        key = (passive, price)
        self.traded_at[key] = self.traded_at.get(key, 0.0) + qty

        for o in self.live(passive):
            through = (price < o.price) if passive == Side.BUY else (price > o.price)
            if through:
                self.fill(o, ts, o.price, o.remaining)        # nivel barrido
            elif price == o.price:
                o.queue_ahead -= qty
                if o.queue_ahead < 0:
                    self.fill(o, ts, o.price, min(-o.queue_ahead, o.remaining))
                    o.queue_ahead = 0.0

    # ---------- deltas del libro ----------
    def on_book_level_change(self, ts: int, side: Side, price: float, old_size: float, new_size: float) -> None:
        self.on_time(ts)
        traded = self.traded_at.pop((side, price), 0.0)
        cancel_vol = max(0.0, (old_size - new_size) - traded)  # bajada no explicada por trades

        for o in self.live(side):
            if o.price != price:
                continue
            if self.queue_model == "prob" and cancel_vol > 0:
                ahead = o.queue_ahead
                behind = max(0.0, old_size - traded - ahead)
                fa, fb = ahead ** self.n, behind ** self.n
                p = fa / (fa + fb) if (fa + fb) > 0 else 0.0
                o.queue_ahead -= cancel_vol * p
            o.queue_ahead = max(0.0, min(o.queue_ahead, new_size))

    # ---------- helpers ----------
    def live(self, side: Side) -> list[LimitOrder]:
        return [o for o in self.orders.values()
                if o.side == side and o.status in (OStatus.OPEN, OStatus.PENDING_CANCEL)]

    def fill(self, o: LimitOrder, ts: int, px: float, q: float) -> None:
        if q <= 0:
            return
        o.filled += q
        o.fills.append((ts, px, q))
        if o.remaining <= 1e-12:
            o.status = OStatus.FILLED
            
        # El Ledger aplica maker_fee aquí, lo simulamos restando o sumando de forma determinista
        quote_spent = D(str(q)) * D(str(px))
        fee = quote_spent * D(str(self.maker_fee))
        if o.side == Side.BUY:
            # fee se cobra en base
            fee_base = D(str(q)) * D(str(self.maker_fee))
            self.ledger.base_free += (D(str(q)) - fee_base)
            self.ledger.quote_free -= quote_spent
        else:
            # fee se cobra en quote
            self.ledger.base_free -= D(str(q))
            self.ledger.quote_free += (quote_spent - fee)
