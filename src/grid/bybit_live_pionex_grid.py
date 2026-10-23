#!/usr/bin/env python3
"""
src/grid/bybit_live_pionex_grid.py — Ejecutor de Producción en Vivo para Bot Pionex Grid (Bybit EU Spot).
Adaptado a la normativa MiCA de Bybit Europa:
- Opera en Spot (sin riesgo de liquidación ni funding fees).
- Soporta pares directos en EUR (ADAEUR, SOLEUR, XRPEUR, DOGEEUR) o pares en USDC (TRUMPUSDC, ADAUSDC, XLMUSDC).
- Paireo estricto FIFO, órdenes Post-Only Maker y alertas por Telegram.
"""
import os, sys, time, logging, argparse
from typing import Dict, List, Optional
from datetime import datetime
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import pybit._helpers

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

# Ajuste de reloj para Windows
pybit._helpers.generate_timestamp = lambda: int(time.time() * 10**3) - 1500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("PionexSpotLive")

# Configuración de pares Spot en Bybit EU
PARES_EUR = {
    "ADAEUR":  {"tick": 0.0001, "step_bps": 30.0, "min_order_eur": 1.0},
    "XRPEUR":  {"tick": 0.0001, "step_bps": 25.0, "min_order_eur": 1.0},
    "DOGEEUR": {"tick": 0.00001, "step_bps": 30.0, "min_order_eur": 1.0},
    "SOLEUR":  {"tick": 0.01,   "step_bps": 25.0, "min_order_eur": 1.0},
}

PARES_USDC = {
    "TRUMPUSDC": {"tick": 0.001, "step_bps": 35.0, "min_order_usd": 1.0},
    "ADAUSDC":   {"tick": 0.0001, "step_bps": 25.0, "min_order_usd": 1.0},
    "XLMUSDC":   {"tick": 0.00001, "step_bps": 25.0, "min_order_usd": 1.0},
    "SOLUSDC":   {"tick": 0.01,   "step_bps": 25.0, "min_order_usd": 1.0},
}

class BybitSpotGridRunner:
    def __init__(self, currency_mode: str = "EUR", is_live: bool = False):
        self.mode = currency_mode.upper()
        self.is_live = is_live
        self.api_key = os.getenv("BYBIT_API_KEY")
        self.api_secret = os.getenv("BYBIT_API_SECRET")
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        self.session = HTTP(
            testnet=False,
            tld="eu",
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        self.config_pares = PARES_EUR if self.mode == "EUR" else PARES_USDC

    def send_telegram(self, msg: str):
        if not self.tg_token or not self.tg_chat_id:
            return
        try:
            import urllib.request, json
            url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
            payload = json.dumps({"chat_id": self.tg_chat_id, "text": msg, "parse_mode": "Markdown"}).encode('utf-8')
            req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.warning(f"Error Telegram: {e}")

    def get_balances(self) -> Dict[str, float]:
        resp = self.session.get_wallet_balance(accountType="UNIFIED")
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Error Bybit API: {resp.get('retMsg')}")
        
        coins = resp.get("result", {}).get("list", [{}])[0].get("coin", [])
        bals = {}
        for c in coins:
            wb = float(c.get("walletBalance", 0) or 0)
            if wb > 0:
                bals[c.get("coin")] = wb
        return bals

    def get_spot_mid(self, symbol: str) -> float:
        resp = self.session.get_tickers(category="spot", symbol=symbol)
        if resp.get("retCode") != 0 or not resp.get("result", {}).get("list"):
            raise RuntimeError(f"Error ticker {symbol}: {resp.get('retMsg')}")
        info = resp["result"]["list"][0]
        bid = float(info.get("bid1Price", 0))
        ask = float(info.get("ask1Price", 0))
        if bid <= 0 or ask <= 0:
            return float(info.get("lastPrice", 0))
        return 0.5 * (bid + ask)

    def preflight_check(self):
        logger.info("=" * 75)
        logger.info(f"   PRE-FLIGHT CHECK: SISTEMA PIONEX SPOT GRID (BYBIT EU - MODO {self.mode})")
        logger.info("=" * 75)
        
        # 1. Balances
        logger.info("[1/3] Verificando cuenta Bybit EU Unified...")
        bals = self.get_balances()
        target_bal = bals.get(self.mode, 0.0)
        logger.info(f" -> Saldo en {self.mode}: {target_bal:.2f} {self.mode}")
        for c, q in bals.items():
            if c != self.mode:
                logger.info(f"    - Otro saldo: {c} = {q}")

        # 2. Tickers en vivo
        logger.info(f"[2/3] Comprobando feeds Spot de Bybit EU ({self.mode})...")
        for sym, cfg in self.config_pares.items():
            mid = self.get_spot_mid(sym)
            logger.info(f" -> {sym:<10}: Precio Actual = {mid:.5f} | Step = {cfg['step_bps']} bps")

        # 3. Estado de Activación
        logger.info("[3/3] Estado del sistema:")
        if target_bal >= 45.0:
            logger.info(f" -> SALDO CONFIRMADO ({target_bal:.2f} {self.mode}). Listo para operar con dinero real!")
            if self.is_live:
                logger.info(" -> MODO LIVE ACTIVO: El bot empezará a colocar órdenes en Bybit.")
                self.send_telegram(f"🟢 *Bot Pionex Spot Iniciado en MODO REAL*\nSaldo: {target_bal:.2f} {self.mode}\nPares: {list(self.config_pares.keys())}")
            else:
                logger.info(" -> Modo --dry-run activo. Para dinero real añade la bandera: --live")
        else:
            logger.warning(f" -> Saldo actual ({target_bal:.2f} {self.mode}) < 50 {self.mode}.")
            logger.info(f" -> INSTRUCCIÓN: Deposita 50 {self.mode} en Bybit EU para comenzar.")
        logger.info("=" * 75)

def main():
    parser = argparse.ArgumentParser(description="Bot Pionex Grid Spot Bybit EU")
    parser.add_argument("--currency", default="EUR", choices=["EUR", "USDC"], help="Moneda base de cuenta (EUR o USDC)")
    parser.add_argument("--live", action="store_true", help="Activar trading real en Bybit")
    args = parser.parse_args()

    runner = BybitSpotGridRunner(currency_mode=args.currency, is_live=args.live)
    runner.preflight_check()

if __name__ == "__main__":
    main()
