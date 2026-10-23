"""
tests/test_grid_v2.py — Batería de pruebas unitarias exhaustivas para grid_v2.
"""
import os
import tempfile
from decimal import Decimal
import pytest

from src.grid_v2.models import InstrumentInfo, FeeRate, Lot, Order, OrderSide, OrderStatus, LotStatus
from src.grid_v2.config import StrategyConfig, RiskConfig
from src.grid_v2.risk import RiskManager
from src.grid_v2.persistence import Database
from src.grid_v2.grid_engine import (
    GridEngine, quantize_price_down, quantize_price_up, quantize_qty_down
)

@pytest.fixture
def temp_db():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_grid.db")
    db = Database(db_path)
    yield db


@pytest.fixture
def mock_instrument():
    return InstrumentInfo(
        symbol="ADAEUR",
        base_coin="ADA",
        quote_coin="EUR",
        tick_size=Decimal("0.0001"),
        qty_step=Decimal("0.1"),
        min_order_qty=Decimal("0.1"),
        min_order_amt=Decimal("1.0")
    )

@pytest.fixture
def mock_fee():
    return FeeRate(
        symbol="ADAEUR",
        maker_fee_rate=Decimal("0.0025"), # 25 bps
        taker_fee_rate=Decimal("0.0025")
    )

# 1. Test de Redondeo de Tick y Qty
def test_tick_and_qty_rounding():
    tick = Decimal("0.0001")
    qty_step = Decimal("0.1")

    # Compra hacia abajo: 0.21458 -> 0.2145
    p_buy = quantize_price_down(Decimal("0.21458"), tick)
    assert p_buy == Decimal("0.2145")

    # Venta hacia arriba: 0.21451 -> 0.2146
    p_sell = quantize_price_up(Decimal("0.21451"), tick)
    assert p_sell == Decimal("0.2146")

    # Cantidad hacia abajo: 10.58 -> 10.5
    q = quantize_qty_down(Decimal("10.58"), qty_step)
    assert q == Decimal("10.5")

# 2. Test de Cantidad Neta tras Comisión de Compra en Moneda Base
def test_net_quantity_after_fee(mock_instrument):
    buy_qty = Decimal("100.0")
    fee_rate = Decimal("0.0025") # 0.25%
    fee_base = buy_qty * fee_rate # 0.25 ADA
    net_qty = buy_qty - fee_base # 99.75 ADA
    
    # Redondeado a qty_step (0.1)
    sell_qty = quantize_qty_down(net_qty, mock_instrument.qty_step)
    assert sell_qty == Decimal("99.7")
    assert sell_qty < buy_qty

# 3. Test de Emparejamiento Estricto por Lote (FIFO)
def test_lot_pairing(temp_db, mock_instrument, mock_fee):
    strat_cfg = StrategyConfig(
        base_currency="EUR",
        initial_capital_eur=Decimal("50.0"),
        pairs=["ADAEUR"],
        num_levels=5,
        step_min_bps=Decimal("25.0"),
        k_fee_multiplier=Decimal("2.5"),
        min_expected_net_bps=Decimal("5.0")
    )
    risk_cfg = RiskConfig(
        max_inv_pct_pair=Decimal("0.30"),
        max_inv_pct_global=Decimal("0.70"),
        slope_threshold_bps=Decimal("35.0"),
        slope_cooldown_seconds=300,
        order_size_factor=Decimal("1.5"),
        max_drawdown_pct=Decimal("15.0"),
        emergency_liquidate=False
    )
    risk_mgr = RiskManager(risk_cfg)
    engine = GridEngine(strat_cfg, risk_mgr, temp_db)
    engine.register_instrument(mock_instrument, mock_fee)

    # Simular orden de compra
    buy_order = Order(
        order_link_id="buy_test_1",
        symbol="ADAEUR",
        side=OrderSide.BUY,
        price=Decimal("0.2000"),
        qty=Decimal("50.0")
    )
    engine.active_orders["buy_test_1"] = buy_order
    temp_db.save_order(buy_order)

    # Ejecutar compra
    sell_order = engine.on_order_filled(
        order_link_id="buy_test_1",
        exec_price=Decimal("0.2000"),
        exec_qty=Decimal("50.0"),
        exec_fee=Decimal("0.125"), # 50 * 0.0025 ADA
        fee_coin="ADA"
    )
    assert sell_order is not None
    assert sell_order.side == OrderSide.SELL
    # Neto = 50 - 0.125 = 49.875 -> 49.8
    assert sell_order.qty == Decimal("49.8")
    assert len(engine.open_lots) == 1

    # Ejecutar venta pareada
    engine.on_order_filled(
        order_link_id=sell_order.order_link_id,
        exec_price=sell_order.price,
        exec_qty=sell_order.qty,
        exec_fee=Decimal("0.025"),
        fee_coin="EUR"
    )
    assert len(engine.open_lots) == 0
    assert engine.metrics.completed_cycles == 1
    assert engine.metrics.realized_pnl > Decimal("0")

# 4. Test de Fills Parciales
def test_partial_fills(temp_db, mock_instrument, mock_fee):
    strat_cfg = StrategyConfig(
        base_currency="EUR",
        initial_capital_eur=Decimal("50.0"),
        pairs=["ADAEUR"],
        num_levels=5,
        step_min_bps=Decimal("25.0"),
        k_fee_multiplier=Decimal("2.5"),
        min_expected_net_bps=Decimal("5.0")
    )
    risk_cfg = RiskConfig(
        max_inv_pct_pair=Decimal("0.30"),
        max_inv_pct_global=Decimal("0.70"),
        slope_threshold_bps=Decimal("35.0"),
        slope_cooldown_seconds=300,
        order_size_factor=Decimal("1.5"),
        max_drawdown_pct=Decimal("15.0"),
        emergency_liquidate=False
    )
    engine = GridEngine(strat_cfg, RiskManager(risk_cfg), temp_db)
    engine.register_instrument(mock_instrument, mock_fee)

    buy_order = Order(
        order_link_id="buy_part_1",
        symbol="ADAEUR",
        side=OrderSide.BUY,
        price=Decimal("0.2000"),
        qty=Decimal("100.0")
    )
    engine.active_orders["buy_part_1"] = buy_order
    temp_db.save_order(buy_order)

    # Fill parcial del 40%
    sell_order = engine.on_order_filled(
        order_link_id="buy_part_1",
        exec_price=Decimal("0.2000"),
        exec_qty=Decimal("40.0"),
        exec_fee=Decimal("0.1"),
        fee_coin="ADA",
        is_partial=True
    )
    assert buy_order.status == OrderStatus.PARTIALLY_FILLED
    assert buy_order.cum_exec_qty == Decimal("40.0")
    assert sell_order is not None
    # 40 - 0.1 = 39.9
    assert sell_order.qty == Decimal("39.9")

# 5. Test de Topes de Inventario Valorado a Mid
def test_inventory_caps():
    risk_cfg = RiskConfig(
        max_inv_pct_pair=Decimal("0.30"), # 30% = 15 EUR en 50 EUR equity
        max_inv_pct_global=Decimal("0.70"),
        slope_threshold_bps=Decimal("35.0"),
        slope_cooldown_seconds=300,
        order_size_factor=Decimal("1.5"),
        max_drawdown_pct=Decimal("15.0"),
        emergency_liquidate=False
    )
    risk_mgr = RiskManager(risk_cfg)

    # 14 EUR inventario actual en ADAEUR y global
    pair_exp = Decimal("14.0")
    total_exp = Decimal("14.0")
    equity = Decimal("50.0")

    # Orden adicional de 0.5 EUR (14 + 0.5 = 14.5 <= 15.0) -> Permitido
    ok, _ = risk_mgr.check_inventory_limits("ADAEUR", pair_exp, total_exp, equity, additional_order_quote=Decimal("0.5"))
    assert ok is True

    # Orden adicional de 2.0 EUR (14 + 2.0 = 16.0 > 15.0) -> Rechazado por tope de par
    ok, reason = risk_mgr.check_inventory_limits("ADAEUR", pair_exp, total_exp, equity, additional_order_quote=Decimal("2.0"))
    assert ok is False
    assert "Tope de inventario de ADAEUR excedido" in reason

# 6. Test de Filtro de Tendencia (Pendiente a 5m y Enfriamiento)
def test_slope_trend_filter():
    risk_cfg = RiskConfig(
        max_inv_pct_pair=Decimal("0.30"),
        max_inv_pct_global=Decimal("0.70"),
        slope_threshold_bps=Decimal("35.0"),
        slope_cooldown_seconds=2, # 2 segundos para test rápido
        order_size_factor=Decimal("1.5"),
        max_drawdown_pct=Decimal("15.0"),
        emergency_liquidate=False
    )
    risk_mgr = RiskManager(risk_cfg)

    t0 = 1000000
    # Precio estable
    risk_mgr.record_price("ADAEUR", Decimal("1.0000"), ts_ms=t0)
    risk_mgr.record_price("ADAEUR", Decimal("1.0000"), ts_ms=t0 + 300000)
    assert risk_mgr.update_trend_filter("ADAEUR") is True

    # Caída brusca de 50 bps (-0.5%): 1.0000 -> 0.9950 en 5 min
    risk_mgr.record_price("ADAEUR", Decimal("0.9950"), ts_ms=t0 + 310000)
    assert risk_mgr.update_trend_filter("ADAEUR") is False

# 7. Test de Reconciliación y Persistencia tras Reinicio
def test_persistence_reconciliation(temp_db, mock_instrument, mock_fee):
    # Guardar lote abierto en DB
    lot = Lot(
        lot_id="lot_recover_1",
        symbol="ADAEUR",
        buy_order_link_id="buy_rec_1",
        buy_price=Decimal("0.2000"),
        buy_qty=Decimal("50.0"),
        fee_base_coin=Decimal("0.125"),
        net_qty=Decimal("49.8"),
        cost_quote=Decimal("10.0"),
        sell_order_link_id="sell_rec_1",
        target_sell_price=Decimal("0.2050"),
        status=LotStatus.OPEN
    )
    temp_db.save_lot(lot)

    # Crear nuevo motor desde cero simulando reinicio
    strat_cfg = StrategyConfig(
        base_currency="EUR",
        initial_capital_eur=Decimal("50.0"),
        pairs=["ADAEUR"],
        num_levels=5,
        step_min_bps=Decimal("25.0"),
        k_fee_multiplier=Decimal("2.5"),
        min_expected_net_bps=Decimal("5.0")
    )
    risk_cfg = RiskConfig(
        max_inv_pct_pair=Decimal("0.30"),
        max_inv_pct_global=Decimal("0.70"),
        slope_threshold_bps=Decimal("35.0"),
        slope_cooldown_seconds=300,
        order_size_factor=Decimal("1.5"),
        max_drawdown_pct=Decimal("15.0"),
        emergency_liquidate=False
    )
    new_engine = GridEngine(strat_cfg, RiskManager(risk_cfg), temp_db)
    new_engine.register_instrument(mock_instrument, mock_fee)
    new_engine.load_state()

    # Reconciliar
    exchange_orders = [{"orderLinkId": "sell_rec_1", "symbol": "ADAEUR", "price": "0.2050", "qty": "49.8"}]
    to_cancel, to_place = new_engine.reconcile(exchange_orders)

    # Verificar que el lote se cargó intacto sin duplicar
    assert len(new_engine.open_lots) == 1
    assert "lot_recover_1" in new_engine.open_lots
    assert new_engine.open_lots["lot_recover_1"].net_qty == Decimal("49.8")
    assert len(to_cancel) == 0

# 8. Test de Arrastre Contable de Polvo (Dust Carryover Accounting)
def test_dust_carryover_accounting(temp_db, mock_instrument, mock_fee):
    strat_cfg = StrategyConfig(
        base_currency="EUR",
        initial_capital_eur=Decimal("50.0"),
        pairs=["ADAEUR"],
        num_levels=3,
        step_min_bps=Decimal("35.0"),
        k_fee_multiplier=Decimal("3.5"),
        min_expected_net_bps=Decimal("15.0")
    )
    risk_cfg = RiskConfig(
        max_inv_pct_pair=Decimal("0.30"),
        max_inv_pct_global=Decimal("0.70"),
        slope_threshold_bps=Decimal("35.0"),
        order_size_factor=Decimal("3.34")
    )
    engine = GridEngine(strat_cfg, RiskManager(risk_cfg), temp_db)
    engine.register_instrument(mock_instrument, mock_fee)

    # Compra 1: 50 ADA -> fee 0.125 ADA -> neto 49.875 -> vende 49.8 ADA, polvo = 0.075 ADA
    buy_1 = Order(order_link_id="b1", symbol="ADAEUR", side=OrderSide.BUY, price=Decimal("0.2000"), qty=Decimal("50.0"))
    engine.active_orders["b1"] = buy_1
    s1 = engine.on_order_filled("b1", exec_price=Decimal("0.2000"), exec_qty=Decimal("50.0"),
                                exec_fee=Decimal("0.125"), fee_coin="ADA")
    assert s1.qty == Decimal("49.8")
    assert engine.residual_qty["ADAEUR"] == Decimal("0.075")

    # Compra 2: 50 ADA -> fee 0.125 ADA -> neto 49.875 + polvo 0.075 = 49.950 ADA -> vende 49.9 ADA!
    buy_2 = Order(order_link_id="b2", symbol="ADAEUR", side=OrderSide.BUY, price=Decimal("0.2000"), qty=Decimal("50.0"))
    engine.active_orders["b2"] = buy_2
    s2 = engine.on_order_filled("b2", exec_price=Decimal("0.2000"), exec_qty=Decimal("50.0"),
                                exec_fee=Decimal("0.125"), fee_coin="ADA")
    assert s2.qty == Decimal("49.9")
    assert engine.residual_qty["ADAEUR"] == Decimal("0.050")

# 9. Test de Kill-Switch y Liquidación de Emergencia (g2x-)
def test_kill_switch_and_emergency_liquidation(temp_db, mock_instrument, mock_fee):
    strat_cfg = StrategyConfig(
        base_currency="EUR",
        initial_capital_eur=Decimal("50.0"),
        pairs=["ADAEUR"],
        num_levels=3
    )
    risk_cfg = RiskConfig(
        max_drawdown_pct=Decimal("15.0"),
        emergency_liquidate=True
    )
    risk_mgr = RiskManager(risk_cfg)
    engine = GridEngine(strat_cfg, risk_mgr, temp_db)
    engine.register_instrument(mock_instrument, mock_fee)

    # Crear lote abierto
    lot = Lot(
        lot_id="lot_liq_1",
        symbol="ADAEUR",
        buy_order_link_id="b_liq",
        buy_price=Decimal("0.2000"),
        buy_qty=Decimal("50.0"),
        fee_base_coin=Decimal("0.125"),
        net_qty=Decimal("49.8"),
        cost_quote=Decimal("10.0"),
        status=LotStatus.OPEN
    )
    engine.open_lots["lot_liq_1"] = lot
    engine.update_mid_price("ADAEUR", Decimal("0.1500")) # Caída fuerte

    # Establecer marca máxima de equity en 50 EUR
    risk_mgr.check_drawdown_kill_switch(Decimal("50.0"))
    assert risk_mgr.peak_equity == Decimal("50.0")

    # Equity cae a 35 EUR desde 50 EUR (30% drawdown > 15% límite)
    killed, dd = risk_mgr.check_drawdown_kill_switch(Decimal("35.0"))
    assert killed is True
    assert dd == Decimal("30.0")

    # Generar órdenes de liquidación
    liq_orders = engine.emergency_liquidation_orders()
    assert len(liq_orders) == 1
    assert liq_orders[0].order_link_id.startswith("g2x")
    assert liq_orders[0].side == OrderSide.SELL
    assert liq_orders[0].qty == Decimal("49.8")

# 10. Test de Repricing Automático en Rechazo PostOnly de Venta
def test_sell_repricing_on_postonly_reject(temp_db, mock_instrument, mock_fee):
    strat_cfg = StrategyConfig(base_currency="EUR", initial_capital_eur=Decimal("50.0"), pairs=["ADAEUR"])
    engine = GridEngine(strat_cfg, RiskManager(RiskConfig()), temp_db)
    engine.register_instrument(mock_instrument, mock_fee)

    lot = Lot(
        lot_id="lot_reprice_1",
        symbol="ADAEUR",
        buy_order_link_id="b_rep",
        buy_price=Decimal("0.2000"),
        buy_qty=Decimal("50.0"),
        fee_base_coin=Decimal("0.125"),
        net_qty=Decimal("49.8"),
        cost_quote=Decimal("10.0"),
        target_sell_price=Decimal("0.2050"),
        status=LotStatus.OPEN
    )
    engine.open_lots["lot_reprice_1"] = lot
    sell = engine._new_sell_for_lot(lot, Decimal("0.2050"))

    # El mercado salta a best_bid = 0.2060 EUR (> 0.2050)
    best_bid = Decimal("0.2060")
    new_sell = engine.reprice_sell(sell.order_link_id, best_bid)

    assert new_sell is not None
    # Debe ser al menos best_bid + 1 tick (0.2060 + 0.0001 = 0.2061)
    assert new_sell.price == Decimal("0.2061")
    assert new_sell.qty == Decimal("49.8")
    assert new_sell.order_link_id in engine.active_orders

