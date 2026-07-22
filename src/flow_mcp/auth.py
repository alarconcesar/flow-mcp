"""Autenticación con Google Flow — login, listado y verificación de sesión.

Reemplaza completamente ``gflow auth login`` para no depender de gflow-cli.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright

from flow_mcp.browser import ensure_xvfb
from flow_mcp.chrome_helpers import _is_playwright_chrome_channel_available
from flow_mcp.constants import BROWSER_ARGS, VIEWPORT
from flow_mcp.profile import (
    _find_authenticated_profile,
    _list_profiles,
    _profile_name_from_dir,
    default_home,
)

log = structlog.get_logger("flow-mcp")

SESSION_API_URL = "https://labs.google/fx/api/auth/session"
FLOW_URL = "https://labs.google/fx/tools/flow"


# ── Profile info ────────────────────────────────────────────────────────


class ProfileInfo:
    """Información de un perfil de Flow."""

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path

    @property
    def email(self) -> str | None:
        acc = self.path / ".gflow_account"
        if acc.exists():
            return acc.read_text(encoding="utf-8").strip()
        return None

    @property
    def is_authenticated(self) -> bool:
        return self.email is not None


# ─── Commands ────────────────────────────────────────────────────────────


async def _check_session(page: Page) -> str | None:
    """Verifica si hay sesión activa en Flow.

    Returns el email del usuario si está autenticado, ``None`` si no.
    """
    try:
        result = await page.evaluate(
            f"""
            async () => {{
                try {{
                    const r = await fetch('{SESSION_API_URL}', {{
                        credentials: 'include'
                    }});
                    const data = await r.json();
                    if (data && data.user && data.user.email) {{
                        return data.user.email;
                    }}
                    return null;
                }} catch(e) {{
                    return null;
                }}
            }}
            """
        )
        return result if isinstance(result, str) else None
    except Exception:
        return None


async def _wait_for_login(
    page: Page,
    ctx: BrowserContext,
    profile_dir: Path,
    profile_name: str,
    *,
    poll_seconds: int = 3,
    timeout_seconds: int = 600,
) -> None:
    """Espera a que el usuario inicie sesión y guarda el perfil."""
    deadline = time.monotonic() + timeout_seconds
    last_check = 0.0

    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║     Flow MCP — Autenticación con Google         ║")
    print("  ╠══════════════════════════════════════════════════╣")
    print("  ║  Se abrió una ventana de Chrome.               ║")
    print("  ║  Inicia sesión con tu cuenta de Google         ║")
    print("  ║  y navega hasta el editor de Google Flow.     ║")
    print("  ║                                                ║")
    print("  ║  La autenticación se detectará automáticamente ║")
    print("  ║  cuando llegues al editor.                     ║")
    print("  ╚══════════════════════════════════════════════════╝\n")

    while time.monotonic() < deadline:
        elapsed = int(time.monotonic() - (deadline - timeout_seconds))
        # Poll cada poll_seconds
        if time.monotonic() - last_check >= poll_seconds:
            last_check = time.monotonic()
            email = await _check_session(page)
            if email:
                # Guardar perfil
                profile_dir.mkdir(parents=True, exist_ok=True)
                (profile_dir / ".gflow_account").write_text(
                    email, encoding="utf-8"
                )
                # Marcar estrategia de Chrome si está disponible
                if _is_playwright_chrome_channel_available():
                    (profile_dir / ".gflow_browser_strategy").write_text(
                        "chrome", encoding="utf-8"
                    )
                print(f"\n  ✅ Sesión iniciada como: {email}")
                print(f"  📁 Perfil guardado en: {profile_dir}\n")
                return
            print(
                f"  ⏳ Esperando inicio de sesión... ({elapsed}s)",
                end="\r",
                flush=True,
            )

        await asyncio.sleep(1)

    print("\n\n  ❌ Tiempo de espera agotado.")
    print("  Ejecuta `flow-mcp auth login` de nuevo e inicia sesión en Chrome.\n")
    raise TimeoutError("Login timeout")


async def cmd_login(profile_name: str | None = None) -> None:
    """Abre Chrome para iniciar sesión en Google Flow.

    El usuario debe iniciar sesión manualmente en la ventana que se abre.
    La herramienta detecta automáticamente cuando la sesión está activa.
    """
    home = default_home()
    name = profile_name or os.environ.get("GFLOW_PROFILE") or "default"
    profile_dir = home / f"profile_{name}"

    print(f"  📁 Perfil: {name}")
    print(f"  📁 Directorio: {profile_dir}")

    ensure_xvfb()

    channel = "chrome" if _is_playwright_chrome_channel_available() else None

    async with async_playwright() as pw:
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                channel=channel,
                args=[
                    "--no-sandbox",
                    "--password-store=basic",
                    "--disable-blink-features=AutomationControlled",
                ],
                viewport=VIEWPORT,
            )
        except Exception as exc:
            print(f"\n  ❌ Error al lanzar Chrome: {exc}")
            if "channel" in str(exc).lower():
                print(
                    "  💡 Intenta con Chromium interno:\n"
                    "     flow-mcp auth login --browser internal\n"
                    "     O instala Google Chrome."
                )
            sys.exit(1)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass  # No fatal — la página puede tardar en cargar

        try:
            await _wait_for_login(page, ctx, profile_dir, name)
        except TimeoutError:
            await ctx.close()
            sys.exit(1)

        await ctx.close()


async def cmd_login_internal(profile_name: str | None = None) -> None:
    """Igual que ``login`` pero usando Chromium interno de Playwright.

    Útil si Google Chrome no está instalado o da problemas.
    """
    home = default_home()
    name = profile_name or os.environ.get("GFLOW_PROFILE") or "default"
    profile_dir = home / f"profile_{name}"

    print(f"  📁 Perfil: {name}")
    print(f"  📁 Directorio: {profile_dir}")
    print("  🌐 Usando Chromium interno de Playwright\n")

    ensure_xvfb()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            channel=None,
            args=[
                "--no-sandbox",
                "--password-store=basic",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport=VIEWPORT,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        try:
            await _wait_for_login(page, ctx, profile_dir, name)
        except TimeoutError:
            await ctx.close()
            sys.exit(1)

        await ctx.close()


async def cmd_list() -> None:
    """Lista los perfiles de Flow disponibles."""
    home = default_home()
    profiles = _list_profiles(home)

    if not profiles:
        print("  No hay perfiles. Ejecuta: flow-mcp auth login\n")
        return

    print(f"\n  Perfiles en {home}\n")
    print(f"  {'Pred.':<8} {'Nombre':<25} {'Email':<35} {'Estado':<12}")
    print(f"  {'-'*8} {'-'*25} {'-'*35} {'-'*12}")

    default_name = os.environ.get("GFLOW_PROFILE")
    if not default_name:
        # El perfil default es el primero autenticado o "default"
        default_name = _find_authenticated_profile(home) or "default"

    for p in profiles:
        name = _profile_name_from_dir(p)
        info = ProfileInfo(name, p)
        is_default = "✓" if name == default_name else ""
        email = info.email or "(sin sesión)"
        status = "activo" if info.is_authenticated else "inactivo"
        print(f"  {is_default:<8} {name:<25} {email:<35} {status:<12}")

    print()
