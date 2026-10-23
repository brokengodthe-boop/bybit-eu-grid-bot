# GEMINI.md - Directrices de Riesgo y Operación del Sistema

## Contexto para Agentes de IA (Antigravity & Grok)
Este repositorio aloja un bot de trading algorítmico autónomo de alta frecuencia (intervalos de 5 minutos) desarrollado bajo la filosofía cuantitativa de Outliners (KManuS88).

### Parámetros Estrictos de Ejecución
- **Frecuencia del Bucle:** 300 segundos (5 minutos).
- **Nocional Mínimo:** 100 USDT por orden en GRVT.
- **Riesgo por Operación:**
  - Take Profit: +6% adaptativo.
  - Stop Loss: -2% estricto de preservación de capital.
- **Circuit Breaker:** Margin Ratio > 40% congela la apertura de posiciones.
- **Monitoreo de Grok:** Grok valida si existen reportes de hacks, exploits o quiebras en X antes de autorizar cualquier entrada.

### Modificaciones y Colaboración
Cualquier cambio realizado por Grok o Antigravity en los módulos de `src/`, `config/` o `estrategias/` debe mantener la integridad de los tipos, respetar la firma EIP-712 de GRVT y no alterar las reglas de seguridad sin consentimiento explícito.
