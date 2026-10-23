from src.bybit_client import BybitClient

b = BybitClient(testnet=False)
balances = b.get_all_balances()
usdc = b.get_balance("USDC")
equity = b.get_total_equity()

print(f"=== ESTADO ACTUAL DEL PORTAFOLIO EN BYBIT ===")
print(f"USDC Disponible: {usdc:.2f} USDC")
print(f"Equity Total: ${equity:.2f} USD\n")

for coin, amt in balances.items():
    if coin in ["USDC", "USDT", "EUR"]:
        continue
    sym = f"{coin}USDC"
    p = b.get_current_price(sym)
    if p > 0:
        val = amt * p
        if val > 0.10: # Solo mayores a 10 centavos
            orders = b.get_open_orders(sym)
            print(f"[*] Activo: {coin}")
            print(f"    - Cantidad: {amt}")
            print(f"    - Precio actual: ${p}")
            print(f"    - Valor actual: ${val:.2f} USD")
            print(f"    - Órdenes activas en Bybit: {len(orders)}")
            for o in orders:
                print(f"        -> [Limit Sell] Precio TP: ${o.get('price')} | Cantidad: {o.get('qty')}")
            print("")
