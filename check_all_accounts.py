from src.bybit_client import BybitClient
import json
c = BybitClient()

for acc in ['UNIFIED', 'FUND']:
    print(f"--- {acc} ---")
    try:
        response = c.session.get_wallet_balance(accountType=acc)
        if response.get('retCode') == 0:
            for coin in response['result']['list'][0]['coin']:
                print(f"{coin['coin']}: {coin['walletBalance']}")
        else:
            print(f"Error: {response}")
    except Exception as e:
        print(e)
