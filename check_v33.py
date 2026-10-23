import json
from collections import Counter

try:
    with open('logs/sombra.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    # Filtrar versión y que tengan narrativa
    v3 = [x for x in data if x.get('version') == 'v3.3_shadow_cot' and x.get('narrative', '') != '']
    
    total = len(v3)
    aprobadas = sum(1 for x in v3 if x.get('allow_trade'))
    fallbacks = sum(1 for x in v3 if "Fallback de seguridad" in x.get('narrative', ''))
    
    print(f"Total v3.3 evaluadas CON narrativa: {total}")
    print(f"Aprobadas: {aprobadas}")
    print(f"Rechazadas reales (VETO de Gemini): {total - aprobadas - fallbacks}")
    print(f"Fallbacks por cuota/timeout: {fallbacks}")
    
    risk_scores = [x.get('risk_score') for x in v3 if x.get('risk_score') is not None]
    counts = Counter(risk_scores)
    
    print("\nDistribucion de Risk Score:")
    for score in sorted(counts.keys()):
        print(f"Riesgo {score}/10: {counts[score]} operaciones")
        
except Exception as e:
    print(f"Error: {e}")
