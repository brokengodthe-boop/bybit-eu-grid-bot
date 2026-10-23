#!/usr/bin/env python3
"""
scripts/preflight_live_check.py — Verificación Pre-Vuelo para Bot Institucional Bybit Spot EU.

Comprueba en un solo comando todos los requisitos antes de lanzar con dinero real:
1. Sincronización precisa del reloj local con Bybit (/v5/market/time).
2. Autenticación y saldo libre en moneda base (EUR/USDC).
3. Tarifas reales de la cuenta (maker/taker) leídas por API.
4. Filtros de pares (tickSize, qtyStep, nocional mínimo).
5. Persistencia SQLite y permisos de escritura.
"""
import os
import sys
import time
from decimal import Decimal

# Asegurar importación de módulos desde el directorio raíz
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.grid_v2.config import load_config
from src.grid_v2.exchange import BybitSpotExchange, ClockError
from src.grid_v2.persistence import Database

# Asegurar UTF-8 en consola de Windows
sys.stdout.reconfigure(encoding="utf-8")


def main():
    print("\n" + "=" * 80)
    print("      🔍 COMPROBACIÓN PRE-VUELO INSTITUCIONAL (BYBIT SPOT EU)")
    print("=" * 80)

    all_passed = True

    # 1. Cargar Configuración
    print("\n[1/5] Cargando configuración (config.yaml y .env)...")
    try:
        config = load_config("config.yaml")
        print("  ✅ config.yaml cargado correctamente.")
        if config.exchange.api_key and config.exchange.api_secret:
            masked_key = config.exchange.api_key[:4] + "..." + config.exchange.api_key[-4:]
            print(f"  ✅ Claves API detectadas: {masked_key}")
        else:
            print("  ⚠️ Claves API ausentes en .env (solo lectura pública disponible).")
            all_passed = False
    except Exception as e:
        print(f"  ❌ Error cargando configuración: {e}")
        return

    # 2. Sincronización del Reloj
    print("\n[2/5] Comprobando sincronización del reloj con Bybit EU...")
    try:
        exchange = BybitSpotExchange(config.exchange, check_clock=False)
        offset = exchange.clock_offset_ms
        if abs(offset) <= 800:
            print(f"  ✅ Reloj sincronizado: desfase de {offset:+d} ms (límite Bybit 1000 ms).")
        else:
            print(f"  ❌ Desfase excesivo: {offset:+d} ms. Ejecuta 'w32tm /resync' en Windows como Administrador.")
            all_passed = False
    except Exception as e:
        print(f"  ❌ Error consultando tiempo del servidor: {e}")
        all_passed = False

    # 3. Saldos de la Cuenta
    print("\n[3/5] Consultando balances en Bybit EU Spot...")
    try:
        if config.exchange.api_key:
            bals = exchange.get_balances()
            base_curr = config.strategy.base_currency
            bal_base = bals.get(base_curr, Decimal("0"))
            print(f"  💰 Saldo en {base_curr}: {bal_base:.2f} {base_curr} (Requerido: {config.strategy.initial_capital_eur:.2f} {base_curr})")
            for coin, amount in bals.items():
                if coin != base_curr and amount > Decimal("0"):
                    print(f"     - {coin}: {amount}")
            if bal_base < config.strategy.initial_capital_eur:
                print(f"  ⚠️ Saldo en {base_curr} ({bal_base:.2f}) inferior al capital asignado ({config.strategy.initial_capital_eur:.2f}).")
                all_passed = False
            else:
                print("  ✅ Saldo suficiente para cubrir el capital inicial.")
        else:
            print("  ⏭️ Omitido por falta de claves API.")
    except Exception as e:
        print(f"  ❌ Error al consultar balances: {e}")
        all_passed = False

    # 4. Comisiones y Filtros de Pares
    print("\n[4/5] Verificando pares configurados y comisiones reales...")
    for sym in config.strategy.pairs:
        try:
            inst = exchange.get_instrument_info(sym)
            mid = exchange.get_mid(sym)
            fee_str = "N/A"
            net_bps = "N/A"
            if config.exchange.api_key:
                fee = exchange.get_fee_rate(sym)
                roundtrip = fee.maker_fee_bps * Decimal("2")
                step = max(config.strategy.step_min_bps, config.strategy.k_fee_multiplier * roundtrip)
                expected_net = step - roundtrip
                fee_str = f"Maker {fee.maker_fee_bps:.1f}b / Taker {fee.taker_fee_bps:.1f}b"
                net_bps = f"+{expected_net:.1f} bps"
            print(f"  📊 {sym:<8} | Mid: {mid:<9.4f} | Tick: {inst.tick_size} | MinQty: {inst.min_order_qty} | MinAmt: {inst.min_order_amt} {inst.quote_coin} | Tarifa: {fee_str} | Margen Neto: {net_bps}")
        except Exception as e:
            print(f"  ❌ Error al consultar {sym}: {e}")
            all_passed = False

    # 5. Persistencia y Base de Datos SQLite
    print("\n[5/5] Verificando persistencia local SQLite...")
    try:
        db = Database(config.db_path)
        db.set_state("preflight_check", str(int(time.time())))
        val = db.get_state("preflight_check")
        db.close()
        print(f"  ✅ Base de datos {config.db_path} lista con permisos de lectura y escritura.")
    except Exception as e:
        print(f"  ❌ Error de persistencia SQLite: {e}")
        all_passed = False

    print("\n" + "=" * 80)
    if all_passed:
        print("  🟢 VEREDICTO: SISTEMA 100% LISTO PARA OPERAR EN MODO LIVE")
        print(f"  Comando para arrancar: python -m src.grid_v2.main --mode live --live")
    else:
        print("  🟡 VEREDICTO: REVISAR ADVERTENCIAS ANTES DE OPERAR CON DINERO REAL")
        print("  Puedes operar en modo simulado sin riesgo: python -m src.grid_v2.main --mode paper --duration 60")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
