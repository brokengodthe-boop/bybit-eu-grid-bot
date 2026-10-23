"""
src/grid_v2/main.py — Punto de entrada unificado para el Bot de Grid Trading Institucional.
Soporta tres modos:
- `backtest`: Ejecuta simulación de 30 días históricos por par con informe honesto y detallado.
- `paper`: Operación en tiempo real con datos de mercado en vivo de Bybit Spot y fills simulados.
- `live`: Operación real con dinero en Bybit Spot (requiere flag --live Y confirmación en consola).
"""
import os
import sys
import time
import signal
import logging
import argparse
from decimal import Decimal
from typing import Dict, List

# Asegurar importación de módulos desde el directorio raíz
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.grid_v2.config import load_config, BotConfig
from src.grid_v2.models import OrderSide, OrderStatus
from src.grid_v2.exchange import BybitSpotExchange
from src.grid_v2.persistence import Database
from src.grid_v2.risk import RiskManager
from src.grid_v2.grid_engine import GridEngine
from src.grid_v2.backtest import fetch_klines, run_portfolio, format_report

# Asegurar UTF-8 en consola de Windows
sys.stdout.reconfigure(encoding='utf-8')

def setup_logging(config: BotConfig):
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler
    file_handler = logging.FileHandler(config.log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

def run_backtest_mode(config: BotConfig, days: int = 30):
    logger = logging.getLogger("MainBacktest")
    logger.info("=" * 80)
    logger.info(f"   INICIANDO MODO BACKTEST: {days} DÍAS HISTÓRICOS POR PAR EN BYBIT SPOT EU")
    logger.info("=" * 80)

    exchange = BybitSpotExchange(config.exchange)
    pairs = config.strategy.pairs

    inst = {}
    fees = {}
    bars = {}

    for symbol in pairs:
        try:
            logger.info(f"Obteniendo metadatos y datos de velas para {symbol}...")
            inst[symbol] = exchange.get_instrument_info(symbol)
            fees[symbol] = exchange.get_fee_rate(symbol)
            bars[symbol] = fetch_klines(symbol, "1", days, tld=config.exchange.tld)
        except Exception as e:
            logger.error(f"Error cargando {symbol}: {e}")

    if not inst:
        logger.error("No se pudo cargar ningún par válido para backtest.")
        return

    logger.info(f"Simulando cartera multiactivo ({len(inst)} pares) con capital de {config.strategy.initial_capital_eur} EUR...")
    res = run_portfolio(
        bars=bars,
        instruments=inst,
        fees=fees,
        strategy_cfg=config.strategy,
        risk_cfg=config.risk,
        capital=config.strategy.initial_capital_eur,
        fill_through_ticks=1
    )

    report_text = format_report(f"INFORME BACKTEST CONSOLIDADO ({days} DÍAS)", res)
    print("\n" + report_text + "\n")


def run_paper_mode(config: BotConfig, duration_seconds: int = 45):
    logger = logging.getLogger("MainPaper")
    logger.info("=" * 85)
    logger.info(f"   INICIANDO MODO PAPER TRADING: MERCADO EN VIVO ({duration_seconds}s)")
    logger.info("=" * 85)

    db = Database(config.db_path)
    exchange = BybitSpotExchange(config.exchange)
    risk = RiskManager(config.risk)
    engine = GridEngine(config.strategy, risk, db)

    # 1. Cargar y verificar pares
    for sym in config.strategy.pairs:
        inst = exchange.get_instrument_info(sym)
        fee = exchange.get_fee_rate(sym)
        engine.register_instrument(inst, fee)
        mid = exchange.get_ticker_mid(sym)
        engine.update_mid_price(sym, mid)

    # 2. Conectar WebSocket público para BBO en vivo
    from pybit.unified_trading import WebSocket
    ws_pub = WebSocket(channel_type="spot", testnet=False)

    def on_book(msg):
        data = msg.get("data", {})
        sym = data.get("s")
        b = data.get("b", [])
        a = data.get("a", [])
        if sym and b and a and sym in engine.instruments:
            best_bid = Decimal(str(b[0][0]))
            best_ask = Decimal(str(a[0][0]))
            mid = (best_bid + best_ask) / Decimal("2")
            engine.update_mid_price(sym, mid)

            # Comprobar fills simulados de compras (best_ask <= buy_price)
            active_buys = [
                o for o in list(engine.active_orders.values())
                if o.symbol == sym and o.side == OrderSide.BUY and o.status == OrderStatus.NEW
            ]
            for o in active_buys:
                if best_ask <= o.price:
                    inst = engine.instruments[sym]
                    fee_info = engine.fees[sym]
                    fee_base = o.qty * fee_info.maker_fee_rate
                    engine.on_order_filled(
                        order_link_id=o.order_link_id,
                        exec_price=o.price,
                        exec_qty=o.qty,
                        exec_fee=fee_base,
                        fee_coin=inst.base_coin
                    )

            # Comprobar fills simulados de ventas (best_bid >= sell_price)
            active_sells = [
                o for o in list(engine.active_orders.values())
                if o.symbol == sym and o.side == OrderSide.SELL and o.status == OrderStatus.NEW
            ]
            for o in active_sells:
                if best_bid >= o.price:
                    inst = engine.instruments[sym]
                    fee_info = engine.fees[sym]
                    fee_quote = (o.price * o.qty) * fee_info.maker_fee_rate
                    engine.on_order_filled(
                        order_link_id=o.order_link_id,
                        exec_price=o.price,
                        exec_qty=o.qty,
                        exec_fee=fee_quote,
                        fee_coin=inst.quote_coin
                    )

    for sym in config.strategy.pairs:
        ws_pub.orderbook_stream(depth=1, symbol=sym, callback=on_book)

    # 3. Generar rejilla inicial de compras
    time.sleep(1.0)
    # 3. Planificar rejilla inicial para cada par
    time.sleep(1.0)
    for sym in config.strategy.pairs:
        to_place, to_cancel = engine.plan(sym)
        for o in to_place:
            engine.mark_order_live(o.order_link_id)
        for o in to_cancel:
            engine.on_order_cancelled(o.order_link_id)
        buys = [o for o in engine.active_orders.values() if o.symbol == sym and o.side == OrderSide.BUY]
        logger.info(f"[{sym}] {len(buys)} órdenes de compra paper activadas en el libro simulado.")

    start_time = time.time()
    try:
        while time.time() - start_time < duration_seconds:
            time.sleep(5)
            # Re-planificar periódicamente para re-centrado dinámico
            for sym in config.strategy.pairs:
                to_place, to_cancel = engine.plan(sym)
                for o in to_place:
                    engine.mark_order_live(o.order_link_id)
                for o in to_cancel:
                    engine.on_order_cancelled(o.order_link_id)

            elapsed = int(time.time() - start_time)
            eq = engine.calculate_mark_to_market_equity()
            logger.info(
                f"⏱️ [{elapsed:>2}s / {duration_seconds}s] Fills={engine.metrics.total_fills} | "
                f"Ciclos={engine.metrics.completed_cycles} | PnL Realizado={engine.metrics.realized_pnl:+.4f}€ | "
                f"PnL Latente={engine.metrics.unrealized_pnl:+.4f}€ | Equity={eq:.4f}€"
            )
    except KeyboardInterrupt:
        logger.info("Detenido por usuario.")

    eq = engine.calculate_mark_to_market_equity()
    logger.info("=" * 85)
    logger.info(f"RESUMEN PAPER TRADING: Fills={engine.metrics.total_fills} | Ciclos={engine.metrics.completed_cycles} | "
                f"PnL Realizado=+{engine.metrics.realized_pnl:.4f}€ | PnL Latente={engine.metrics.unrealized_pnl:+.4f}€ | Equity={eq:.4f}€")
    logger.info("=" * 85)

def run_live_mode(config: BotConfig):
    logger = logging.getLogger("MainLive")
    logger.critical("=" * 95)
    logger.critical("            ⚠️ ATENCIÓN: MODO LIVE CON CAPITAL REAL SELECCIONADO ⚠️")
    logger.critical("=" * 95)
    print("\n[CONFIRMACIÓN DE SEGURIDAD REQUERIDA]")
    print(f"Está a punto de iniciar el bot con DINERO REAL en Bybit Europa.")
    print(f"Capital asignado: {config.strategy.initial_capital_eur} {config.strategy.base_currency}")
    print(f"Pares a operar: {config.strategy.pairs}")
    confirm = input("\n¿Está completamente seguro de que desea operar con DINERO REAL? [s/N]: ").strip().lower()
    if confirm not in ("s", "si", "yes", "y"):
        logger.info("Operación LIVE cancelada por el usuario.")
        return

    logger.info("Inicializando conexión segura con Bybit EU...")
    db = Database(config.db_path)
    exchange = BybitSpotExchange(config.exchange)
    risk = RiskManager(config.risk)
    engine = GridEngine(config.strategy, risk, db)

    # 1. Cargar estado previo si existía
    engine.load_state()

    # 2. Registrar instrumentos y comisiones reales
    for sym in config.strategy.pairs:
        inst = exchange.get_instrument_info(sym)
        fee = exchange.get_fee_rate(sym)
        engine.register_instrument(inst, fee)
        mid = exchange.get_mid(sym)
        engine.update_mid_price(sym, mid)

    # 3. Reconciliar órdenes vivas en el exchange
    all_open = []
    for sym in config.strategy.pairs:
        all_open.extend(exchange.get_open_orders(sym))
    to_cancel_ex, to_place_ex = engine.reconcile(all_open)
    for sym, link in to_cancel_ex:
        exchange.cancel_order(sym, link)
    for o in to_place_ex:
        st, oid, msg = exchange.place_post_only(o.symbol, o.side, o.price, o.qty, o.order_link_id)
        if st == "ok":
            engine.mark_order_live(o.order_link_id, oid)
        else:
            engine.on_order_rejected(o.order_link_id)

    logger.info("Modo LIVE en ejecución con protección de PostOnly y Kill-Switch.")

def main():
    parser = argparse.ArgumentParser(description="Bot Institucional de Grid Trading (Bybit EU Spot)")
    parser.add_argument("--mode", default="paper", choices=["backtest", "paper", "live"], help="Modo de ejecución")
    parser.add_argument("--days", type=int, default=30, help="Días de histórico para backtest (por defecto 30)")
    parser.add_argument("--duration", type=int, default=30, help="Duración en segundos para modo paper")
    parser.add_argument("--live", action="store_true", help="Flag obligatorio para modo live")
    args = parser.parse_args()

    config = load_config("config.yaml")
    setup_logging(config)

    if args.mode == "backtest":
        run_backtest_mode(config, days=args.days)
    elif args.mode == "paper":
        run_paper_mode(config, duration_seconds=args.duration)
    elif args.mode == "live":
        if not args.live:
            print("ERROR: Para ejecutar en vivo debe incluir explícitamente el flag --live.")
            sys.exit(1)
        run_live_mode(config)

if __name__ == "__main__":
    main()
