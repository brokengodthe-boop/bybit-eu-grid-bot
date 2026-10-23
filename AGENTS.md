# AGENTS.md - Ecosistema de Trading Autónomo Antigravity & Grok

Este documento sirve como puente de sincronización operativa entre **Google Antigravity** y **Grok (xAI)**.

## 1. Ruta del Proyecto en Local
`C:\Users\estre\.gemini\antigravity-ide\scratch\grvt_trading_bot`

## 2. Estado de la Infraestructura
- **Orquestador Central:** [`main.py`](file:///C:/Users/estre/.gemini/antigravity-ide/scratch/grvt_trading_bot/main.py) se encuentra activo ejecutando en segundo plano con una cadencia de **5 minutos**.
- **Ingesta de Datos:** [`src/cmc_client.py`](file:///C:/Users/estre/.gemini/antigravity-ide/scratch/grvt_trading_bot/src/cmc_client.py) conectado a CoinMarketCap Pro API (Top 100 en vivo).
- **Alfas Formulaicos:** [`estrategias/logica_anomalias.py`](file:///C:/Users/estre/.gemini/antigravity-ide/scratch/grvt_trading_bot/estrategias/logica_anomalias.py) implementando reversión a la media basada en DeepSeek-R1.
- **Filtro Cognitivo Grok:** [`src/grok_client.py`](file:///C:/Users/estre/.gemini/antigravity-ide/scratch/grvt_trading_bot/src/grok_client.py) preparado para consultar sentimientos, noticias y vetos de trades en X.
- **Exchange GRVT:** [`src/grvt_client.py`](file:///C:/Users/estre/.gemini/antigravity-ide/scratch/grvt_trading_bot/src/grvt_client.py) preparado para Isolated Margin, órdenes Post-Only y control de apalancamiento.
- **Registro y Auditoría:** [`logs/auditoria.json`](file:///C:/Users/estre/.gemini/antigravity-ide/scratch/grvt_trading_bot/logs/auditoria.json) se actualiza tras cada ciclo con los precios, métricas, veredictos de Grok y PnL.

## 3. Reglas de Supervivencia y Preservación de Capital
1. **Apalancamiento Máximo:** 5x (prohibido superar este nivel).
2. **Circuit Breaker:** Si el Margin Ratio supera el 40%, se prohíben nuevas compras y solo se permite reducir exposición.
3. **Margen Aislado:** Todas las operaciones deben configurarse exclusivamente en 'Isolated Margin'.
4. **Entorno Actual:** Modo Simulación / Testnet (`GRVT_ENV=testnet`).
