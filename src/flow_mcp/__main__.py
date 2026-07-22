"""Entry point — detecta subcomandos o inicia el MCP server.

Uso:
    flow-mcp                   → Inicia MCP server (stdio)
    flow-mcp auth login        → Autenticar con Google Flow via Chrome
    flow-mcp auth login --browser internal → Autenticar via Chromium interno
    flow-mcp auth list         → Listar perfiles
    flow-mcp auth logout       → Eliminar perfil y credenciales
    flow-mcp credits           → Consultar créditos restantes
    flow-mcp --version         → Mostrar versión
    flow-mcp help              → Mostrar ayuda
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    """Entry point principal."""
    # --version flag
    if "--version" in sys.argv:
        from flow_mcp import __version__

        print(f"flow-mcp v{__version__}")
        sys.exit(0)

    # Parse --browser flag from raw argv before filtering
    has_browser_flag = False
    browser_type = "chrome"
    if "--browser" in sys.argv:
        idx = sys.argv.index("--browser")
        if idx + 1 < len(sys.argv) and sys.argv[idx + 1] in ("chrome", "internal"):
            has_browser_flag = True
            browser_type = sys.argv[idx + 1]

    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    # Auth login with --browser flag
    if has_browser_flag and len(args) >= 2 and args[0] == "auth" and args[1] == "login":
        if browser_type == "internal":
            _run_async(_auth_login_internal())
        else:
            _run_async(_auth_login())
    elif len(args) >= 2 and args[0] == "auth" and args[1] == "login-internal":
        _run_async(_auth_login_internal())
    elif len(args) >= 2 and args[0] == "auth" and args[1] == "login":
        _run_async(_auth_login())
    elif len(args) >= 2 and args[0] == "auth" and args[1] == "list":
        _run_async(_auth_list())
    elif len(args) >= 2 and args[0] == "auth" and args[1] == "logout":
        _run_async(_auth_logout())
    elif len(args) >= 2 and args[0] == "auth" and args[1] in ("accounts", "list-accounts"):
        _run_async(_auth_accounts())
    elif len(args) >= 2 and args[0] == "auth" and args[1] == "switch":
        name = args[2] if len(args) >= 3 else None
        _run_async(_auth_switch(name))
    elif len(args) >= 1 and args[0] == "credits":
        _run_async(_credits())
    elif len(args) >= 1 and args[0] == "help":
        _print_help()
    else:
        from flow_mcp.server import main as server_main

        server_main()


def _run_async(coro) -> None:
    """Run an async command."""
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print("\n  Cancelado.")
        sys.exit(0)


async def _auth_login() -> None:
    from flow_mcp.auth import cmd_login
    await cmd_login()


async def _auth_login_internal() -> None:
    from flow_mcp.auth import cmd_login_internal
    await cmd_login_internal()


async def _auth_list() -> None:
    from flow_mcp.auth import cmd_list
    await cmd_list()


async def _auth_logout() -> None:
    from flow_mcp.auth import cmd_logout
    await cmd_logout()


async def _auth_accounts() -> None:
    from flow_mcp.auth import cmd_accounts
    await cmd_accounts()


async def _auth_switch(name: str | None = None) -> None:
    from flow_mcp.auth import cmd_switch_account
    await cmd_switch_account(name)


async def _credits() -> None:
    from flow_mcp.auth import cmd_credits
    await cmd_credits()


def _print_help() -> None:
    print("Flow MCP v0.3.0 — Google Flow image generation for Claude Code\n")
    print("USO:")
    print("  flow-mcp                    Iniciar MCP server (modo stdio)")
    print("  flow-mcp auth login         Iniciar sesión en Google Flow")
    print("  flow-mcp auth login --browser internal  Login con Chromium interno")
    print("  flow-mcp auth list          Listar perfiles guardados")
    print("  flow-mcp auth accounts      Listar cuentas con orden de prioridad")
    print("  flow-mcp auth switch <name> Cambiar a cuenta activa manualmente")
    print("  flow-mcp auth logout        Eliminar perfil activo")
    print("  flow-mcp credits            Consultar créditos restantes")
    print("  flow-mcp --version          Mostrar versión")
    print("  flow-mcp help               Mostrar ayuda\n")


if __name__ == "__main__":
    main()
