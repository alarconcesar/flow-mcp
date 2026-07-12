"""Helper para detectar el canal de Chrome a usar con Playwright.

Portado de gflow-cli para eliminar la dependencia externa.

Lee el archivo ``.gflow_browser_strategy`` del perfil para saber si
usar Google Chrome real (``channel="chrome"``) o el Chromium de Playwright.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog

log = structlog.get_logger("flow-mcp")


def _is_playwright_chrome_channel_available() -> bool:
    """True si Playwright puede encontrar Google Chrome para ``channel="chrome"``.

    Playwright busca Chrome en rutas fijas por plataforma — no acepta
    un Chromium genérico.
    """
    env_override = os.environ.get("CHROME_BINARY")
    if env_override:
        return True

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        chrome_paths: list[Path] = [
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path(local_app_data or "") / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    elif sys.platform == "darwin":
        chrome_paths = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        chrome_paths = [
            Path("/opt/google/chrome/chrome"),
        ]

    return any(p.exists() for p in chrome_paths)


def channel_for_profile(profile_dir: Path) -> str | None:
    """Retorna el canal de Playwright a usar para este perfil, o ``None``.

    Si el perfil tiene un archivo ``.gflow_browser_strategy`` con ``chrome``
    y Google Chrome está instalado, retorna ``"chrome"``.
    Si no, retorna ``None`` (usa el Chromium de Playwright).
    """
    marker = profile_dir / ".gflow_browser_strategy"
    if not marker.exists():
        return None
    strategy = marker.read_text(encoding="utf-8").strip()
    if strategy != "chrome":
        return None
    if _is_playwright_chrome_channel_available():
        return "chrome"
    log.warning(
        "chrome_helpers.chrome_marker_but_unavailable",
        profile_dir=str(profile_dir),
        hint="Profile was captured with system Chrome but Google Chrome is not "
        "found at the paths Playwright expects. "
        "Falling back to Playwright's bundled Chromium.",
    )
    return None
