import os
from dotenv import load_dotenv

def validate_environment():
    """
    Verifica en el arranque la existencia de las variables ambientales críticas.
    Lanza RuntimeError si falta alguna.
    """
    load_dotenv()
    
    required_vars = [
        "CMC_API_KEY",
        "GRVT_TRADING_API_KEY",
        "GRVT_TRADING_PRIVATE_KEY",
        "GRVT_ENV"
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        raise RuntimeError(f"Faltan variables de entorno críticas: {', '.join(missing_vars)}")
        
    if os.getenv("GRVT_ENV") != "testnet":
        raise RuntimeError("Regla de Seguridad Violada: GRVT_ENV debe ser 'testnet'.")
        
    print("Validación de entorno exitosa.")
    
if __name__ == "__main__":
    validate_environment()
