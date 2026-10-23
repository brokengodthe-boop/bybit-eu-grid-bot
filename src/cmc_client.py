import os
import requests
import pandas as pd
from dotenv import load_dotenv
import asyncio

class CMCClient:
    """Cliente asíncrono para interactuar con la API de CoinMarketCap."""
    
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("CMC_API_KEY")
        self.base_url = "https://pro-api.coinmarketcap.com"
        self.headers = {
            "Accepts": "application/json",
            "X-CMC_PRO_API_KEY": self.api_key
        }
        
    async def get_gainers_losers(self, limit: int = 100) -> pd.DataFrame:
        """
        Consume el endpoint listings/latest de CoinMarketCap para extraer los activos con
        sus métricas de precio, volumen y variación porcentual en tiempo real.
        """
        url = f"{self.base_url}/v1/cryptocurrency/listings/latest"
        params = {
            "start": "1",
            "limit": str(limit),
            "convert": "USD"
        }
        
        try:
            response = await asyncio.to_thread(requests.get, url, headers=self.headers, params=params, timeout=10)
            
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", [])
                if not data:
                    print("[CMC] Respuesta vacía de CoinMarketCap.")
                    return pd.DataFrame()
                
                rows = []
                for item in data:
                    quote = item.get("quote", {}).get("USD", {})
                    # Ignorar stablecoins atadas a $1 para buscar volatilidad real
                    sym = item.get("symbol", "")
                    if sym in ["USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE"]:
                        continue
                        
                    rows.append({
                        "symbol": sym,
                        "close_price": float(quote.get("price", 0.0)),
                        "volume": float(quote.get("volume_24h", 0.0)),
                        "percent_change": float(quote.get("percent_change_1h", 0.0)),
                        "percent_change_24h": float(quote.get("percent_change_24h", 0.0)),
                        "market_cap": float(quote.get("market_cap", 0.0))
                    })
                
                df = pd.DataFrame(rows)
                print(f"[CMC] Ingesta exitosa: {len(df)} criptoactivos en tiempo real procesados.")
                return df
            else:
                print(f"[CMC] Error API: {response.status_code} - {response.text}")
                return pd.DataFrame()
                
        except Exception as e:
            print(f"[CMC] Excepción de conexión: {e}")
            return pd.DataFrame()

    async def get_quote_by_symbol(self, symbol: str) -> dict:
        """
        Consulta las métricas en tiempo real de un token específico desde CoinMarketCap.
        """
        symbol_upper = symbol.strip().upper()
        url = f"{self.base_url}/v1/cryptocurrency/quotes/latest"
        params = {
            "symbol": symbol_upper,
            "convert": "USD"
        }
        try:
            await asyncio.sleep(0.1)
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", {})
                coin_data = data.get(symbol_upper)
                if isinstance(coin_data, list) and len(coin_data) > 0:
                    coin_data = coin_data[0]
                elif isinstance(coin_data, dict):
                    pass
                else:
                    return {}
                    
                quote = coin_data.get("quote", {}).get("USD", {})
                return {
                    "symbol": symbol_upper,
                    "name": coin_data.get("name", symbol_upper),
                    "close_price": float(quote.get("price", 0.0)),
                    "volume": float(quote.get("volume_24h", 0.0)),
                    "percent_change_1h": float(quote.get("percent_change_1h", 0.0)),
                    "percent_change_24h": float(quote.get("percent_change_24h", 0.0)),
                    "market_cap": float(quote.get("market_cap", 0.0))
                }
            else:
                print(f"[CMC] Error al obtener quote de {symbol_upper}: {response.status_code}")
                return {}
        except Exception as e:
            print(f"[CMC] Excepción en quote de {symbol_upper}: {e}")
            return {}


if __name__ == "__main__":
    client = CMCClient()
    df = asyncio.run(client.get_gainers_losers())
    print(df.head(10))
