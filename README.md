# Bot Institucional de Grid Trading Spot (Bybit EU)

Sistema cuantitativo de Rejilla (Grid Trading) para **Bybit Europa (API v5)**, adaptado a la normativa MiCA (operación exclusiva en **SPOT**, pares contra **EUR** y **USDC**, sin derivados ni apalancamiento).

---

## 1. Arquitectura y Principios de Diseño

1. **Datos del Exchange Dinámicos:**
   - Consulta dinámica de `tickSize`, `qtyStep`, `minOrderQty` y `minOrderAmt` al arrancar.
   - Consulta la comisión **REAL** de la cuenta (`/v5/account/fee-rate`). Prohibido asumir 2 bps.
   - Sincronización dinámica de reloj con `/v5/market/time` calculando el RTT y offset real, con `recv_window` configurable en `config.yaml`.
2. **Espaciado Derivado de Comisiones:**
   - $\text{step\_bps} = \max(\text{step\_min}, k \times 2 \times \text{maker\_fee\_bps})$ con $k \ge 2.5$.
   - Si el beneficio neto por ciclo es menor a 5 bps tras comisiones, el par se descarta automáticamente.
   - Redondeo con `Decimal` (compras hacia abajo con `ROUND_FLOOR`, ventas hacia arriba con `ROUND_CEILING`).
3. **Paireo Estricto FIFO por Lote:**
   - Deducción exacta de la comisión de compra en moneda base: la orden de venta pareada usa la cantidad **NETA** recibida redondeada al `qtyStep`.
   - Indexación estricta por `orderLinkId` único (nunca por precio).
   - Manejo de fills parciales y persistencia transaccional en SQLite (`data/grid_state.db`).
4. **Gestión de Riesgo:**
   - Tope de inventario por par (30% del equity total) y global (70%) valorados a precio de mercado (`mid`).
   - Filtro de tendencia con pendiente a 5 minutos (`slope < -35 bps` pausa compras) con tiempo de enfriamiento configurable.
   - Dimensionamiento de lote proporcional (`lot = equity / (num_levels * factor)`), ajustando niveles si cae por debajo de `minOrderAmt`.
   - Kill switch automático por drawdown (>15%).
5. **Métricas Honestas:**
   - Desglose estricto entre **PnL Realizado** (caja en cuenta) y **PnL Latente** (flotante a precio de mercado).

---

## 2. Instalación y Requisitos

Requiere Python 3.11+.

```bash
# 1. Crear y activar entorno virtual
python -m venv venv
.\venv\Scripts\activate

# 2. Instalar dependencias
pip install -r requirements.txt
pip install pytest pyyaml
```

Configurar las credenciales en `.env` (nunca en el código):
```env
BYBIT_API_KEY=tu_api_key
BYBIT_API_SECRET=tu_api_secret
```

---

## 3. Modos de Ejecución

### Modo Backtest (Histórico de 30 días con datos reales de Bybit)
```bash
.\venv\Scripts\python.exe src/grid_v2/main.py --mode backtest --days 30
```

### Modo Paper Trading (Mercado en vivo y fills simulados)
```bash
.\venv\Scripts\python.exe src/grid_v2/main.py --mode paper --duration 60
```

### Modo Live (Dinero Real)
Requiere flag explícito `--live` **Y** confirmación interactiva por consola:
```bash
.\venv\Scripts\python.exe src/grid_v2/main.py --mode live --live
```

---

## 4. Tests Unitarios

Ejecutar la suite completa de pruebas:
```bash
.\venv\Scripts\python.exe -m pytest tests/test_grid_v2.py -v
```

---

## 5. Divulgación de Riesgos
- **Sin rentabilidad garantizada:** El grid trading genera flujo de caja en mercados laterales y oscilantes, pero en caídas prolongadas acumula inventario que experimenta pérdida latente hasta que el precio se recupera.
- **Riesgo de liquidez y spreads:** En momentos de alta volatilidad, las órdenes PostOnly pueden tardar en llenarse o requerir recálculo de precio.
