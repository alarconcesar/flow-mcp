"""Profile resolution — find the gflow-cli profile directory.

Sin dependencias externas. Usa ``platformdirs`` para determinar el
directorio de perfiles de gflow-cli (misma lógica que usa gflow-cli internamente).

Resolution order:
1. ``GFLOW_PROFILE`` env var (nombre del perfil).
2. ``GFLOW_CLI_HOME`` env var (ruta directa al directorio de perfiles).
3. Escanear perfiles y elegir el primero con ``.gflow_account``.
4. Fallback a ``default`` (da error si no existe).
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from platformdirs import user_data_dir

log = structlog.get_logger("flow-mcp")


def default_home() -> Path:
    """Retorna el directorio base de gflow-cli (misma lógica que gflow-cli)."""
    return Path(user_data_dir("gflow-cli", "ffroliva", ensure_exists=False))


def _list_profiles(root: Path) -> list[Path]:
    """Retorna todos los directorios ``profile_<name>`` en ``root``."""
    if not root.is_dir():
        return []
    return sorted(
        [p for p in root.iterdir() if p.name.startswith("profile_")],
    )


def _profile_name_from_dir(profile_dir: Path) -> str:
    """Extrae el nombre del perfil del path del directorio."""
    return profile_dir.name[len("profile_"):]


def _find_authenticated_profile(root: Path) -> str | None:
    """Retorna el nombre del primer perfil que tenga archivo ``.gflow_account``."""
    for profile_dir in _list_profiles(root):
        if (profile_dir / ".gflow_account").exists():
            name = _profile_name_from_dir(profile_dir)
            log.info("profile.auto_detected", name=name)
            return name
    return None


def _find_authenticated_profiles(root: Path) -> list[str]:
    """Return names of ALL profiles that have a ``.gflow_account`` file.

    The list is ordered by filesystem sort (typically creation order).
    """
    result: list[str] = []
    for profile_dir in _list_profiles(root):
        if (profile_dir / ".gflow_account").exists():
            name = _profile_name_from_dir(profile_dir)
            result.append(name)
    if result:
        log.info("profile.auto_detected_all", names=result)
    return result


def resolve_profile() -> Path:
    """Resuelve el directorio del perfil de gflow-cli.

    Returns
        ``Path`` al directorio del perfil.

    Raises
        RuntimeError: si no existe el directorio o no hay autenticación.
    """
    # 1. Home directory
    home_str = os.environ.get("GFLOW_CLI_HOME")
    home: Path = Path(home_str) if home_str else default_home()

    # 2. Profile name
    name: str | None = os.environ.get("GFLOW_PROFILE")
    if not name:
        name = _find_authenticated_profile(home) or "default"

    # 3. Profile directory
    profile_dir = home / f"profile_{name}"

    if not profile_dir.exists():
        raise RuntimeError(
            f"gflow-cli profile '{name}' not found at {profile_dir}. "
            "Run `flow-mcp auth login` first."
        )

    log.info("profile.resolved", path=str(profile_dir), name=name)
    return profile_dir
