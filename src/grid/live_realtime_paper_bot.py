#!/usr/bin/env python3
"""
src/grid/live_realtime_paper_bot.py — Motor de Prueba y Validación en Tiempo Real (Live Paper Trading).
Conecta con los WebSockets de Bybit Spot en tiempo real y ejecuta la rejilla Pionex sobre el mercado en vivo.
"""
import sys, time, logging, threading
from dataclasses import dataclass
from typing import Dict, List, Tuple
from pybit.unified_trading import WebSocket

sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("RealTimePaper")

CONFIG_PARES = {
    "ADAEUR":  {"tick": 0.0001,  "step_bps": 25.0, "lot_eur": 1.25},
    "XRPEUR":  {"tick": 0.0001,  "step_bps": 20.0, "lot_eur": 1.25},
    "DOGEEUR": {"tick": 0.00001, "step_bps": 25.0, "lot_eur": 1.25},
    "SOLEUR":  {"tick": 0.01,    "step_bps": 20.0, "lot_eur": 1.25},
}

@dataclass
class ActiveOrder:
    order_id: int
    symbol: str
    side: str          # "BUY" o "SELL"
    order_type: str    # "OPEN_BUY" o "CLOSE_SELL"
    price: float
    qty: float
    parent_price: float = 0.0

class RealTimeGridEngine:
    def __init__(self, duration_seconds: int = 60):
        self.duration = duration_seconds
        self.capital_total = 50.0  # 50 EUR
        self.maker_fee_rate = 0.0002  # 2 bps = 0.02%
        
        self.realized_profit = 0.0
        self.total_fees = 0.0
        self.fills = 0
        self.cycles = 0
        self.order_counter = 0
        
        # Estado por par
        self.initial_prices: Dict[str, float] = {}
        self.last_mids: Dict[str, float] = {}
        self.bids: Dict[str, Dict[float, ActiveOrder]] = {s: {} for s in CONFIG_PARES}
        self.asks: Dict[str, Dict[float, ActiveOrder]] = {s: {} for s in CONFIG_PARES}
        self.open_lots: Dict[str, Dict[float, float]] = {s: {} for s in CONFIG_PARES}
        
        self.lock = threading.Lock()
        self.running = True
        self.start_time = 0.0

    def next_id(self):
        self.order_counter += 1
        return self.order_counter

    def init_pair_grid(self, sym: str, mid: float):
        cfg = CONFIG_PARES[sym]
        tick = cfg["tick"]
        step = cfg["step_bps"] * 1e-4
        lot = cfg["lot_eur"]
        
        self.initial_prices[sym] = mid
        self.last_mids[sym] = mid
        
        # Colocar 5 niveles de compra por debajo y 5 niveles de venta de referencia por encima
        for i in range(1, 6):
            p_buy = round((mid * (1.0 - i * step)) / tick) * tick
            qty = lot / p_buy
            self.bids[sym][p_buy] = ActiveOrder(
                order_id=self.next_id(),
                symbol=sym,
                side="BUY",
                order_type="OPEN_BUY",
                price=p_buy,
                qty=qty
            )
        logger.info(f"[{sym}] Rejilla inicializada a Mid={mid:.5f} (5 compras activas desde {min(self.bids[sym].keys()):.5f})")

    def process_bbo(self, sym: str, best_bid: float, best_ask: float):
        with self.lock:
            mid = 0.5 * (best_bid + best_ask)
            self.last_mids[sym] = mid
            
            # Inicialización perezosa al recibir el primer BBO
            if sym not in self.initial_prices:
                self.init_pair_grid(sym, mid)
                return
                
            cfg = CONFIG_PARES[sym]
            tick = cfg["tick"]
            step = cfg["step_bps"] * 1e-4
            lot = cfg["lot_eur"]
            
            # 1. Comprobar si el mercado cruzó alguno de nuestros BIDS
            # Si el mejor ask del mercado cayó hasta o por debajo de nuestro bid pasivo -> FILL DE COMPRA!
            hit_bids = [p for p in list(self.bids[sym].keys()) if best_ask <= p]
            for p in hit_bids:
                order = self.bids[sym].pop(p)
                fee = p * order.qty * self.maker_fee_rate
                self.total_fees += fee
                self.fills += 1
                self.open_lots[sym][p] = order.qty
                
                logger.info(f"🟢 [FILL COMPRA en Vivo] {sym} | Px: {p:.5f} | Qty: {order.qty:.4f} | Fee: -{fee:.4f}€")
                
                # Colocar venta pareada exactamente a +step
                p_sell = round((p * (1.0 + step)) / tick) * tick
                self.asks[sym][p_sell] = ActiveOrder(
                    order_id=self.next_id(),
                    symbol=sym,
                    side="SELL",
                    order_type="CLOSE_SELL",
                    price=p_sell,
                    qty=order.qty,
                    parent_price=p
                )
                logger.info(f"   ↳ Orden venta pareada colocada a {p_sell:.5f} (+{cfg['step_bps']} bps)")
                
            # 2. Comprobar si el mercado cruzó alguno de nuestros ASKS
            # Si el mejor bid del mercado subió hasta o por encima de nuestro ask pasivo -> FILL DE VENTA (CICLO CERRADO!)
            hit_asks = [p for p in list(self.asks[sym].keys()) if best_bid >= p]
            for p in hit_asks:
                order = self.asks[sym].pop(p)
                fee = p * order.qty * self.maker_fee_rate
                self.total_fees += fee
                self.fills += 1
                self.cycles += 1
                
                buy_p = order.parent_price
                gross = (p - buy_p) * order.qty
                net = gross - (fee * 2)
                self.realized_profit += gross
                self.open_lots[sym].pop(buy_p, None)
                
                logger.info(f"💰 [CICLO CERRADO en Vivo] {sym} | Compra: {buy_p:.5f} -> Venta: {p:.5f} | Bruto: +{gross:.4f}€ | Neto: +{net:.4f}€")
                
                # Reabrir la compra original
                new_qty = lot / buy_p
                self.bids[sym][buy_p] = ActiveOrder(
                    order_id=self.next_id(),
                    symbol=sym,
                    side="BUY",
                    order_type="OPEN_BUY",
                    price=buy_p,
                    qty=new_qty
                )

    def calculate_equity(self) -> Tuple[float, float]:
        unrealized = 0.0
        for sym, lots in self.open_lots.items():
            curr_mid = self.last_mids.get(sym, self.initial_prices.get(sym, 0.0))
            for p, q in lots.items():
                unrealized += (curr_mid - p) * q
        equity = self.capital_total + self.realized_profit - self.total_fees + unrealized
        return equity, unrealized

    def run(self):
        logger.info("=" * 80)
        logger.info(f"   INICIANDO PRUEBA DE TRADING EN TIEMPO REAL (DURACIÓN: {self.duration}s)")
        logger.info(f"   Capital: {self.capital_total:.2f} EUR | Pares: {list(CONFIG_PARES.keys())}")
        logger.info("=" * 80)
        
        self.start_time = time.time()
        
        # Conectar WebSocket de Bybit
        ws = WebSocket(channel_type="spot", testnet=False)
        
        def on_book(msg):
            data = msg.get("data", {})
            sym = data.get("s")
            b = data.get("b", [])
            a = data.get("a", [])
            if sym and b and a:
                best_bid = float(b[0][0])
                best_ask = float(a[0][0])
                self.process_bbo(sym, best_bid, best_ask)

        for sym in CONFIG_PARES.keys():
            ws.orderbook_stream(depth=1, symbol=sym, callback=on_book)

        # Bucle de monitorización
        while time.time() - self.start_time < self.duration:
            time.sleep(10)
            elapsed = int(time.time() - self.start_time)
            with self.lock:
                eq, unrl = self.calculate_equity()
                net_cash = self.realized_profit - self.total_fees
                logger.info(f"⏱️ [{elapsed:>2}s / {self.duration}s] Fills: {self.fills} | Ciclos: {self.cycles} | Beneficio Realizado: +{net_cash:.4f}€ | Flotante: {unrl:+.4f}€ | Equity: {eq:.4f}€")

        # Cierre y reporte final
        with self.lock:
            eq, unrl = self.calculate_equity()
            net_cash = self.realized_profit - self.total_fees
            logger.info("=" * 80)
            logger.info("   INFORME FINAL DE LA PRUEBA EN TIEMPO REAL (BYBIT LIVE)")
            logger.info("=" * 80)
            logger.info(f"Duración de la prueba:      {self.duration} segundos")
            logger.info(f"Capital Inicial:            {self.capital_total:.2f} EUR")
            logger.info(f"Total Fills Registrados:    {self.fills}")
            logger.info(f"Ciclos Completos Cerrados:  {self.cycles}")
            logger.info(f"Ganancia Bruta Realizada:  +{self.realized_profit:.4f} EUR")
            logger.info(f"Comisiones Pagadas Maker:  -{self.total_fees:.4f} EUR")
            logger.info(f"Beneficio Neto en Caja:    +{net_cash:.4f} EUR")
            logger.info(f"Flotante de Órdenes:       {unrl:+.4f} EUR")
            logger.info(f"Equity Final Total:         {eq:.4f} EUR")
            logger.info(f"Ratio Ganancia / Comisión:  {self.realized_profit / max(self.total_fees, 1e-9):.1f}x")
            logger.info("=" * 80)

if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    engine = RealTimeGridEngine(duration_seconds=dur)
    engine.run()
