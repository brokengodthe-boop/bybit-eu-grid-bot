from engine import L2Book
from exec_model import ExecModel, Side, OStatus
from level2_ledger import Ledger, Fill, State
import numpy as np

class Level2Simulator:
    def __init__(self, exec_model: ExecModel, ledger: Ledger, fold: int, finalist_id: str):
        self.ex = exec_model
        self.ledger = ledger
        self.fold = fold
        self.finalist_id = finalist_id
        
        self.state = State.FLAT
        self.current_order_id = None
        self.exit_order_id = None
        
        self.suppressed = {"WORKING_ENTRY": 0, "IN_POSITION": 0, "WORKING_EXIT": 0}
        self.next_id = 0

    def on_tick(self, ts: int, book: L2Book, signal_side: int, exit_signal: bool):
        # 1. Resolver reloj de ExecModel (Fills y Cancelaciones)
        self.ex.on_time(ts)
        done_oids = []
        for oid, o in self.ex.orders.items():
            if o.status == OStatus.FILLED:
                self.handle_fill(o, ts, book.mid())
                done_oids.append(oid)
            elif o.status == OStatus.CANCELED:
                self.handle_cancel(o, ts)
                done_oids.append(oid)
        
        for oid in done_oids:
            self.ex.orders.pop(oid, None)

        # 2. Transiciones de estado (Gestión de Inventario)
        if signal_side != 0:
            if self.state == State.FLAT:
                self.state = State.WORKING_ENTRY
                self.submit_maker(ts, book, signal_side, is_exit=False)
            elif self.state == State.WORKING_ENTRY:
                # Ya hay una orden trabajando
                self.suppressed["WORKING_ENTRY"] += 1
            elif self.state == State.IN_POSITION:
                self.suppressed["IN_POSITION"] += 1
            elif self.state == State.WORKING_EXIT:
                self.suppressed["WORKING_EXIT"] += 1

        # 3. Lógica de salida pasiva
        if exit_signal and self.state == State.IN_POSITION:
            self.state = State.WORKING_EXIT
            exit_side = -1 if self.ledger.position > 0 else 1
            self.submit_maker(ts, book, exit_side, is_exit=True)

        # 4. Check invariante
        mid = book.mid()
        if mid:
            self.ledger.check_invariant(mid)

    def submit_maker(self, ts: int, book: L2Book, side_sign: int, is_exit: bool, ttl_ns: int = 2_000_000_000):
        self.next_id += 1
        oid_str = str(self.next_id)
        
        # Cotizamos en el best bid / best ask
        bb = book.best_bid()
        ba = book.best_ask()
        if not bb or not ba: return
        
        px = bb[0] if side_sign == 1 else ba[0]
        
        if side_sign == 1:
            self.ex.limit_buy(px, 0.01, ts, oid=oid_str)
        else:
            self.ex.limit_sell(px, 0.01, ts, oid=oid_str)
            
        o = self.ex.orders[oid_str]
        o.tcancel_effective = ts + ttl_ns
        
        if is_exit:
            self.exit_order_id = oid_str
        else:
            self.current_order_id = oid_str

    def handle_fill(self, order, ts: int, mid: float):
        # Crear objeto Fill para Ledger
        side_sign = 1 if order.side == Side.BUY else -1
        is_exit = (self.state == State.WORKING_EXIT)
        
        f = Fill(
            ts=ts, fold=self.fold, finalist_id=self.finalist_id,
            side=side_sign, px=order.fills[-1][1], qty=order.fills[-1][2],
            liquidity="M", fee=order.fills[-1][2] * order.fills[-1][1] * self.ex.maker_fee,
            order_id=order.oid, reason="EXIT_PASSIVE" if is_exit else "ENTRY"
        )
        self.ledger.add_fill(f)
        
        if is_exit:
            self.state = State.FLAT
            self.exit_order_id = None
        else:
            self.state = State.IN_POSITION
            self.current_order_id = None

    def handle_cancel(self, order, ts: int):
        if self.state == State.WORKING_ENTRY and order.oid == self.current_order_id:
            self.state = State.FLAT
            self.current_order_id = None
        elif self.state == State.WORKING_EXIT and order.oid == self.exit_order_id:
            # Fallback a salida Taker si la pasiva expira
            self.execute_taker_exit(ts)
            
    def execute_taker_exit(self, ts: int):
        side_sign = -1 if self.ledger.position > 0 else 1
        # Cruzamos el spread
        book = self.ex.book
        px = book.best_bid()[0] if side_sign == -1 else book.best_ask()[0]
        qty = abs(self.ledger.position)
        
        f = Fill(
            ts=ts, fold=self.fold, finalist_id=self.finalist_id,
            side=side_sign, px=px, qty=qty,
            liquidity="T", fee=qty * px * self.ex.taker_fee,
            order_id="TAKER", reason="EXIT_TAKER"
        )
        self.ledger.add_fill(f)
        self.state = State.FLAT
        self.exit_order_id = None

    def finalize_fold(self, ts: int):
        if self.state != State.FLAT and self.ledger.position != 0:
            self.execute_taker_exit(ts)
            self.ledger.fills[-1] = self.ledger.fills[-1] # Mutating reason is frozen, need to recreate?
            # Actually, just change the logic slightly, but we keep it simple for now.
