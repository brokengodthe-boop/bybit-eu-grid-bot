"""
src/grid_v2/persistence.py — Persistencia SQLite (lotes, órdenes, ejecuciones, estado y PnL).

- Una única conexión protegida por lock (los callbacks del WebSocket corren en otros hilos).
- Soporta ":memory:" para backtests rápidos.
- La tabla `executions` hace idempotente el procesamiento de fills: el mismo execId que llega
  por WebSocket y por el sondeo REST solo se contabiliza una vez.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from decimal import Decimal
from typing import Dict, List, Optional

from src.grid_v2.models import Lot, LotStatus, Order, OrderSide, OrderStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    buy_order_link_id TEXT NOT NULL,
    buy_price TEXT NOT NULL,
    buy_qty TEXT NOT NULL,
    fee_base_coin TEXT NOT NULL,
    net_qty TEXT NOT NULL,
    cost_quote TEXT NOT NULL DEFAULT '0',
    sell_order_link_id TEXT,
    target_sell_price TEXT NOT NULL,
    executed_sell_price TEXT,
    sell_fee TEXT NOT NULL,
    status TEXT NOT NULL,
    gross_profit TEXT NOT NULL,
    net_profit TEXT NOT NULL,
    opened_at INTEGER NOT NULL,
    closed_at INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
    order_link_id TEXT PRIMARY KEY,
    order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    status TEXT NOT NULL,
    cum_exec_qty TEXT NOT NULL,
    cum_exec_value TEXT NOT NULL,
    cum_exec_fee TEXT NOT NULL,
    fee_coin TEXT,
    lot_id TEXT,
    step_bps TEXT NOT NULL DEFAULT '0',
    created_time INTEGER NOT NULL,
    updated_time INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    exec_id TEXT PRIMARY KEY,
    order_link_id TEXT NOT NULL,
    exec_time INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    cash_balance TEXT NOT NULL,
    equity TEXT NOT NULL,
    drawdown_pct TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str = "data/grid_state.db"):
        self.db_path = db_path
        if db_path != ":memory:":
            d = os.path.dirname(db_path)
            if d:
                os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Añade columnas nuevas a bases de datos creadas por versiones anteriores."""
        def cols(table: str) -> set:
            return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        if "cost_quote" not in cols("lots"):
            self._conn.execute("ALTER TABLE lots ADD COLUMN cost_quote TEXT NOT NULL DEFAULT '0'")
        oc = cols("orders")
        if "lot_id" not in oc:
            self._conn.execute("ALTER TABLE orders ADD COLUMN lot_id TEXT")
        if "step_bps" not in oc:
            self._conn.execute("ALTER TABLE orders ADD COLUMN step_bps TEXT NOT NULL DEFAULT '0'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ órdenes
    def save_order(self, o: Order) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO orders (order_link_id, order_id, symbol, side, price, qty, status,
                   cum_exec_qty, cum_exec_value, cum_exec_fee, fee_coin, lot_id, step_bps, created_time, updated_time)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (o.order_link_id, o.order_id, o.symbol, o.side.value, str(o.price), str(o.qty), o.status.value,
                 str(o.cum_exec_qty), str(o.cum_exec_value), str(o.cum_exec_fee), o.fee_coin, o.lot_id,
                 str(o.step_bps), o.created_time, o.updated_time),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_order(r: sqlite3.Row) -> Order:
        return Order(
            order_link_id=r["order_link_id"], order_id=r["order_id"], symbol=r["symbol"],
            side=OrderSide(r["side"]), price=Decimal(r["price"]), qty=Decimal(r["qty"]),
            status=OrderStatus(r["status"]), cum_exec_qty=Decimal(r["cum_exec_qty"]),
            cum_exec_value=Decimal(r["cum_exec_value"]), cum_exec_fee=Decimal(r["cum_exec_fee"]),
            fee_coin=r["fee_coin"] or "", lot_id=r["lot_id"], step_bps=Decimal(r["step_bps"] or "0"),
            created_time=r["created_time"], updated_time=r["updated_time"],
        )

    def get_order(self, order_link_id: str) -> Optional[Order]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM orders WHERE order_link_id=?", (order_link_id,)).fetchone()
        return self._row_to_order(r) if r else None

    def get_active_orders(self) -> List[Order]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE status IN ('Pending','New','PartiallyFilled')").fetchall()
        return [self._row_to_order(r) for r in rows]

    # ------------------------------------------------------------------ lotes
    def save_lot(self, l: Lot) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO lots (lot_id, symbol, buy_order_link_id, buy_price, buy_qty, fee_base_coin,
                   net_qty, cost_quote, sell_order_link_id, target_sell_price, executed_sell_price, sell_fee, status,
                   gross_profit, net_profit, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (l.lot_id, l.symbol, l.buy_order_link_id, str(l.buy_price), str(l.buy_qty), str(l.fee_base_coin),
                 str(l.net_qty), str(l.cost_quote), l.sell_order_link_id, str(l.target_sell_price),
                 str(l.executed_sell_price) if l.executed_sell_price is not None else None, str(l.sell_fee),
                 l.status.value, str(l.gross_profit), str(l.net_profit), l.opened_at, l.closed_at),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_lot(r: sqlite3.Row) -> Lot:
        return Lot(
            lot_id=r["lot_id"], symbol=r["symbol"], buy_order_link_id=r["buy_order_link_id"],
            buy_price=Decimal(r["buy_price"]), buy_qty=Decimal(r["buy_qty"]),
            fee_base_coin=Decimal(r["fee_base_coin"]), net_qty=Decimal(r["net_qty"]),
            cost_quote=Decimal(r["cost_quote"] or "0"), sell_order_link_id=r["sell_order_link_id"],
            target_sell_price=Decimal(r["target_sell_price"]),
            executed_sell_price=Decimal(r["executed_sell_price"]) if r["executed_sell_price"] else None,
            sell_fee=Decimal(r["sell_fee"]), status=LotStatus(r["status"]),
            gross_profit=Decimal(r["gross_profit"]), net_profit=Decimal(r["net_profit"]),
            opened_at=r["opened_at"], closed_at=r["closed_at"],
        )

    def get_open_lots(self, symbol: Optional[str] = None) -> List[Lot]:
        q, args = "SELECT * FROM lots WHERE status='OPEN'", ()
        if symbol:
            q, args = q + " AND symbol=?", (symbol,)
        with self._lock:
            return [self._row_to_lot(r) for r in self._conn.execute(q, args).fetchall()]

    def get_all_lots(self, symbol: Optional[str] = None) -> List[Lot]:
        q, args = "SELECT * FROM lots", ()
        if symbol:
            q, args = q + " WHERE symbol=?", (symbol,)
        with self._lock:
            return [self._row_to_lot(r) for r in self._conn.execute(q, args).fetchall()]

    # ------------------------------------------------------------------ ejecuciones (idempotencia)
    def register_execution(self, exec_id: str, order_link_id: str, exec_time: int) -> bool:
        """Devuelve True si el execId es nuevo (debe procesarse), False si ya se procesó."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO executions (exec_id, order_link_id, exec_time) VALUES (?,?,?)",
                (exec_id, order_link_id, exec_time))
            self._conn.commit()
            return cur.rowcount == 1

    def last_execution_time(self) -> int:
        with self._lock:
            r = self._conn.execute("SELECT MAX(exec_time) AS t FROM executions").fetchone()
        return int(r["t"] or 0)

    # ------------------------------------------------------------------ estado escalar
    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (key, value))
            self._conn.commit()

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def get_state_prefix(self, prefix: str) -> Dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM state WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {r["key"][len(prefix):]: r["value"] for r in rows}

    # ------------------------------------------------------------------ snapshots
    def save_pnl_snapshot(self, timestamp: int, realized_pnl: Decimal, unrealized_pnl: Decimal,
                          cash_balance: Decimal, equity: Decimal, drawdown_pct: Decimal) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO pnl_snapshots (timestamp, realized_pnl, unrealized_pnl, cash_balance, equity, drawdown_pct)
                   VALUES (?,?,?,?,?,?)""",
                (timestamp, str(realized_pnl), str(unrealized_pnl), str(cash_balance), str(equity), str(drawdown_pct)))
            self._conn.commit()


class NullDatabase(Database):
    """Base de datos en memoria sin coste de commit: para barridos de backtest masivos."""

    def __init__(self) -> None:
        super().__init__(":memory:")

    def save_order(self, o: Order) -> None:  # noqa: D401 - el estado vive en memoria en el motor
        pass

    def save_lot(self, l: Lot) -> None:
        pass

    def set_state(self, key: str, value: str) -> None:
        pass

    def save_pnl_snapshot(self, *args, **kwargs) -> None:
        pass
