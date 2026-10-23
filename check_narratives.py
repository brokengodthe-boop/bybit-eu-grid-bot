import re

try:
    with open('veto_lines.txt', 'r', encoding='utf-16') as f:
        lines = f.readlines()
        
    keywords = {
        "Duda/Ambigüedad/Evidencia insuficiente": ["duda", "ambigu", "insuficiente", "falta de evidencia", "evidencia es ambigua", "evidencia ambigua"],
        "Fallo de Volumen": ["volumen"],
        "Cima/Resistencia/Sobrecompra": ["resistencia", "cima", "sobrecompra", "extensión", "rally"],
        "Caída Vertical/Cuchillo": ["caída vertical", "sin freno", "sin soporte", "cuchillo"],
        "Agotamiento/Debilidad": ["agotamiento", "rechazo", "pérdida de momentum", "debilidad"]
    }
    
    tally = {k: 0 for k in keywords}
    
    for line in lines:
        n = line.lower()
        for k, words in keywords.items():
            if any(w in n for w in words):
                tally[k] += 1
                
    print(f"Total de líneas VETO parseadas: {len(lines)}\n")
    print("Menciones por categoría (un veto puede tener varias):")
    for k, v in tally.items():
        print(f"- {k}: {v}")
        
except Exception as e:
    print(f"Error: {e}")
