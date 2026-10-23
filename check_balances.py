from src.bybit_client import BybitClient
c = BybitClient()
response = c.session.get_wallet_balance(accountType='UNIFIED')
for coin in response['result']['list'][0]['coin']:
    print(f"{coin['coin']}: {coin['walletBalance']}")
