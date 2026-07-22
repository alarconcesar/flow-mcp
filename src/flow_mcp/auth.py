"""Google Flow authentication — login, logout, list, credits, session checks.

Replaces ``gflow auth login`` entirely — no dependency on gflow-cli.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright

from flow_mcp.browser import ensure_xvfb
from flow_mcp.chrome_helpers import _is_playwright_chrome_channel_available
from flow_mcp.constants import BROWSER_ARGS, VIEWPORT
from flow_mcp.js_templates import CREDIT_BALANCE_JS, check_session_js
from flow_mcp.profile import (
    _find_authenticated_profile,
    _list_profiles,
    _profile_name_from_dir,
    default_home,
)

log = structlog.get_logger("flow-mcp")

SESSION_API_URL = "https://labs.google/fx/api/auth/session"
FLOW_URL = "https://labs.google/fx/tools/flow"


# ── Profile info ──────────────────────────────────────────────────────────


class ProfileInfo:
    """Information about a Flow profile."""

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


# ─── Shared helpers ───────────────────────────────────────────────────────


async def _check_session(page: Page) -> str | None:
    """Check if there's an active Flow session.

    Returns the user's email if authenticated, ``None`` otherwise.
    """
    try:
        js = check_session_js(SESSION_API_URL)
        result = await page.evaluate(js)
        return result if isinstance(result, str) else None
    except Exception:
        return None


async def _launch_login_context(
    pw,
    profile_dir: Path,
    channel: str | None,
) -> tuple[BrowserContext, Page]:
    """Launch a persistent Chromium context for login (headed, needs Xvfb)."""
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
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return ctx, page


async def _wait_for_login(
    page: Page,
    ctx: BrowserContext,
    profile_dir: Path,
    profile_name: str,
    *,
    poll_seconds: int = 3,
    timeout_seconds: int = 600,
) -> None:
    """Wait for the user to log in through the browser window."""
    deadline = time.monotonic() + timeout_seconds
    last_check = 0.0

    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║     Flow MCP — Google Authentication           ║")
    print("  ╠══════════════════════════════════════════════════╣")
    print("  ║  A Chrome window has been opened.               ║")
    print("  ║  Sign in with your Google account              ║")
    print("  ║  and navigate to the Google Flow editor.       ║")
    print("  ║                                                ║")
    print("  ║  Auth will be detected automatically           ║")
    print("  ║  when you reach the editor.                    ║")
    print("  ╚══════════════════════════════════════════════════╝\n")

    while time.monotonic() < deadline:
        elapsed = int(time.monotonic() - (deadline - timeout_seconds))
        if time.monotonic() - last_check >= poll_seconds:
            last_check = time.monotonic()
            email = await _check_session(page)
            if email:
                profile_dir.mkdir(parents=True, exist_ok=True)
                (profile_dir / ".gflow_account").write_text(email, encoding="utf-8")
                if _is_playwright_chrome_channel_available():
                    (profile_dir / ".gflow_browser_strategy").write_text(
                        "chrome", encoding="utf-8"
                    )
                print(f"\n  ✅ Signed in as: {email}")
                print(f"  📁 Profile saved at: {profile_dir}\n")
                return
            print(
                f"  ⏳ Waiting for sign-in... ({elapsed}s)",
                end="\r",
                flush=True,
            )

        await asyncio.sleep(1)

    print("\n\n  ❌ Timeout reached.")
    print("  Run `flow-mcp auth login` again and sign in through Chrome.\n")
    raise TimeoutError("Login timeout")


# ─── Commands ─────────────────────────────────────────────────────────────


async def cmd_login(profile_name: str | None = None) -> None:
    """Open Chrome to sign in to Google Flow.

    The user must sign in manually in the browser window that opens.
    Authentication is detected automatically when the Flow editor loads.
    """
    home = default_home()
    name = profile_name or os.environ.get("GFLOW_PROFILE") or "default"
    profile_dir = home / f"profile_{name}"

    print(f"  📁 Profile: {name}")
    print(f"  📁 Directory: {profile_dir}")

    ensure_xvfb()

    channel = "chrome" if _is_playwright_chrome_channel_available() else None

    async with async_playwright() as pw:
        try:
            ctx, page = await _launch_login_context(pw, profile_dir, channel)
        except Exception as exc:
            print(f"\n  ❌ Failed to launch Chrome: {exc}")
            if "channel" in str(exc).lower():
                print(
                    "  💡 Try internal Chromium instead:\n"
                    "     flow-mcp auth login --browser internal\n"
                    "     Or install Google Chrome."
                )
            sys.exit(1)

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


async def cmd_login_internal(profile_name: str | None = None) -> None:
    """Same as ``login`` but uses Playwright's bundled Chromium.

    Useful when Google Chrome is not installed or has issues.
    """
    home = default_home()
    name = profile_name or os.environ.get("GFLOW_PROFILE") or "default"
    profile_dir = home / f"profile_{name}"

    print(f"  📁 Profile: {name}")
    print(f"  📁 Directory: {profile_dir}")
    print("  🌐 Using Playwright's internal Chromium\n")

    ensure_xvfb()

    async with async_playwright() as pw:
        ctx, page = await _launch_login_context(pw, profile_dir, None)

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
    """List available Flow profiles."""
    home = default_home()
    profiles = _list_profiles(home)

    if not profiles:
        print("  No profiles found. Run: flow-mcp auth login\n")
        return

    print(f"\n  Profiles in {home}\n")
    print(f"  {'Def.':<6} {'Name':<25} {'Email':<35} {'Status':<12}")
    print(f"  {'-'*6} {'-'*25} {'-'*35} {'-'*12}")

    default_name = os.environ.get("GFLOW_PROFILE")
    if not default_name:
        default_name = _find_authenticated_profile(home) or "default"

    for p in profiles:
        name = _profile_name_from_dir(p)
        info = ProfileInfo(name, p)
        is_default = "✓" if name == default_name else ""
        email = info.email or "(no session)"
        status = "active" if info.is_authenticated else "inactive"
        print(f"  {is_default:<6} {name:<25} {email:<35} {status:<12}")

    print()


async def cmd_logout(profile_name: str | None = None) -> None:
    """Remove the current profile's authentication data.

    Deletes the profile directory and all stored credentials.
    """
    home = default_home()
    name = profile_name or os.environ.get("GFLOW_PROFILE")
    if not name:
        name = _find_authenticated_profile(home)

    if not name:
        print("  No authenticated profile found.\n")
        return

    profile_dir = home / f"profile_{name}"
    if not profile_dir.exists():
        print(f"  Profile '{name}' not found at {profile_dir}")
        return

    try:
        shutil.rmtree(profile_dir)
        print(f"  ✅ Logged out: profile '{name}' removed.")
        print(f"  📁 Deleted: {profile_dir}\n")
    except OSError as exc:
        print(f"  ❌ Failed to remove profile: {exc}")
        sys.exit(1)


async def cmd_credits(profile_name: str | None = None) -> None:
    """Check remaining credits via the creditBalance API.

    Requires an active browser session — opens the Flow page if needed.
    """
    from flow_mcp.browser_pool import acquire_page, release_context

    print("  Checking credit balance...\n")

    page, ctx = await acquire_page()
    try:
        # Ensure we have a valid session
        try:
            await page.goto(
                FLOW_URL, wait_until="domcontentloaded", timeout=30000
            )
        except Exception:
            pass

        await asyncio.sleep(3)
        result = await page.evaluate(CREDIT_BALANCE_JS)

        if result and isinstance(result, dict):
            print(f"  {'Item':<30} {'Value':<15}")
            print(f"  {'-'*30} {'-'*15}")

            for key, value in result.items():
                label = key.replace("_", " ").title()
                # Format nicely
                if isinstance(value, (int, float)) and "credit" in key.lower():
                    print(f"  {label:<30} {value:<15}")
                elif isinstance(value, str):
                    print(f"  {label:<30} {value:<30}")
                elif value is None:
                    print(f"  {label:<30} {'-':<15}")
                else:
                    print(f"  {label:<30} {str(value):<15}")

            print()
        else:
            print("  No credit info available. Are you logged in?\n")

    except Exception as exc:
        print(f"  ❌ Failed to check credits: {exc}\n")
    finally:
        await release_context(ctx)
