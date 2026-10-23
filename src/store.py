"""Almacén JSON seguro para auditoria.json (y otros archivos de lista).

- Escritura atómica (archivo temporal + os.replace): un corte de luz no deja el JSON a medias.
- Un JSON corrupto LANZA AuditoriaCorrupta; nunca se trata como lista vacía.
- Cada modificación es leer -> cambiar SOLO el registro afectado -> escribir, todo bajo el lock,
  así el escáner (que agrega) y el monitor (que actualiza) no se pisan.

Crea el JsonListStore DENTRO de main() (con el event loop ya corriendo).
"""
import asyncio
import json
import os
import tempfile


class AuditoriaCorrupta(Exception):
    """El archivo existe pero no se puede leer. NO se sobrescribe: revísalo a mano."""


def clave(reg: dict) -> str:
    """Identificador de un registro. Los antiguos (sin 'id') usan timestamp|symbol."""
    return reg.get("id") or f"{reg.get('timestamp')}|{reg.get('symbol')}"


def escribir_json_atomico(path: str, data, indent: int = 4) -> None:
    carpeta = os.path.dirname(path) or "."
    os.makedirs(carpeta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=carpeta, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def leer_estado(path: str) -> dict:
    """Lee un JSON pequeño de estado (dict). Si no existe o está roto, devuelve {}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class JsonListStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def _leer(self) -> list:
        try:
            if os.path.getsize(self.path) == 0:
                return []
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as e:
            raise AuditoriaCorrupta(f"{self.path} está corrupto: {e}") from e
        if not isinstance(data, list):
            raise AuditoriaCorrupta(f"{self.path}: se esperaba una lista")
        return data

    async def leer(self) -> list:
        async with self.lock:
            return self._leer()

    async def agregar(self, registro: dict) -> None:
        async with self.lock:
            data = self._leer()
            data.append(registro)
            escribir_json_atomico(self.path, data)

    async def actualizar(self, key: str, cambios: dict) -> bool:
        """Aplica 'cambios' al registro con esa clave. False si no existe."""
        async with self.lock:
            data = self._leer()
            for r in reversed(data):
                if clave(r) == key:
                    r.update(cambios)
                    escribir_json_atomico(self.path, data)
                    return True
            return False
