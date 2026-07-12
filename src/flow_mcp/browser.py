"""Playwright browser lifecycle — context creation, token capture, cleanup."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import AsyncIterator

import structlog
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from flow_mcp.chrome_helpers import channel_for_profile
from flow_mcp.constants import BROWSER_ARGS, VIEWPORT

log = structlog.get_logger("flow-mcp")

_XVFB_STARTED: bool = False


def ensure_xvfb() -> None:
    """Start Xvfb on ``:99`` when running on Linux without a display.

    No-op on Windows/macOS or if Xvfb is already running.
    """
    global _XVFB_STARTED
    if _XVFB_STARTED or sys.platform != "linux":
        return

    import subprocess

    ret = subprocess.run(
        ["pgrep", "-a", "Xvfb"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if "Xvfb" in ret.stdout:
        _XVFB_STARTED = True
        return

    subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x720x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log.info("xvfb.started", display=":99")
    time.sleep(1)
    _XVFB_STARTED = True


async def create_browser_context(
    pw: Playwright,
    profile_dir: Path,
    *,
    headless: bool = False,
) -> BrowserContext:
    """Launch a persistent browser context using the gflow-cli profile."""
    channel = channel_for_profile(profile_dir)

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        channel=channel or None,
        args=BROWSER_ARGS,
        viewport=VIEWPORT,
    )
    log.info("browser.launched", profile=str(profile_dir), channel=channel)
    return context


async def capture_bearer_token(page: Page, timeout_ms: int = 60_000) -> str:
    """Navigate to Flow and capture the Bearer token from request headers.

    Raises ``RuntimeError`` if no token is found.
    """
    bearer: str | None = None

    def _on_request(req):
        nonlocal bearer
        if bearer is not None:
            return
        auth = req.headers.get("authorization", "")
        if auth.startswith("Bearer ya29"):
            bearer = auth[7:]

    page.on("request", _on_request)

    # Navigate — timeouts are non-fatal; the page may already be cached.
    try:
        await page.goto(
            "https://labs.google/fx/tools/flow",
            wait_until="networkidle",
            timeout=timeout_ms,
        )
    except Exception as exc:
        log.warning("navigation.timeout", error=str(exc))

    await page.wait_for_timeout(8_000)
    if not bearer:
        await page.wait_for_timeout(5_000)
    if not bearer:
        raise RuntimeError(
            "Failed to capture Bearer token — auth session may be expired. "
            "Run `flow-mcp auth login` to refresh."
        )

    log.info("auth.token_captured")
    return bearer
