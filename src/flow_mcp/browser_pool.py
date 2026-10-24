"""Singleton browser pool — keeps a Playwright context alive across calls.

Reusing the browser context avoids the ~15s overhead of launching Chrome,
loading the profile, and navigating to Flow on every generation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator

import structlog
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from flow_mcp.chrome_helpers import channel_for_profile
from flow_mcp.constants import BROWSER_ARGS, BROWSER_IDLE_TIMEOUT_S, VIEWPORT
from flow_mcp.profile import resolve_profile

log = structlog.get_logger("flow-mcp")


def _headless_mode() -> bool:
    """Resolve headless mode from FLOW_HEADLESS (default: real window).

    Google Flow's reCAPTCHA scores pure-headless Chromium as
    ``PUBLIC_ERROR_UNUSUAL_ACTIVITY`` and refuses to generate, so the default
    is a real window on a virtual display (Xvfb). Set ``FLOW_HEADLESS=1`` only
    for diagnostics where generation is not needed.
    """
    raw = os.getenv("FLOW_HEADLESS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return False


def _display_alive() -> bool:
    """True when the DISPLAY from the environment points to a live X server."""
    disp = os.getenv("DISPLAY", "")
    m = re.match(r"^:(\d+)", disp)
    if not m:
        return False
    return Path(f"/tmp/.X11-unix/X{m.group(1)}").exists()


_STATE_FILE = Path.home() / ".flow-mcp" / "xvfb.json"


def _own_display() -> str | None:
    """Return our own previously started Xvfb display, if still alive."""
    try:
        data = json.loads(_STATE_FILE.read_text())
        disp = str(data["display"])
        num = disp.lstrip(":")
        if Path(f"/tmp/.X11-unix/X{num}").exists() and Path(f"/proc/{int(data['pid'])}").exists():
            return disp
    except Exception:
        pass
    return None


def _ensure_display() -> None:
    """Point DISPLAY at a private Xvfb we control (never a foreign server).

    A stale/foreign X socket in the environment makes Chromium die with
    ``No protocol specified`` / ``Missing X server``, so we only inherit it when
    FLOW_USE_EXISTING_DISPLAY=1 and the socket is actually there. Otherwise we
    reuse our own Xvfb (tracked in ~/.flow-mcp/xvfb.json) or start a fresh one
    on the first clean display number.
    """
    if os.getenv("FLOW_USE_EXISTING_DISPLAY", "").strip().lower() in ("1", "true", "yes") and _display_alive():
        return

    own = _own_display()
    if own:
        os.environ["DISPLAY"] = own
        os.environ["XAUTHORITY"] = ""
        log.info("browser.xvfb_reused", display=own)
        return

    os.environ.pop("DISPLAY", None)
    os.environ.pop("XAUTHORITY", None)
    for num in range(99, 140):
        sock = Path(f"/tmp/.X11-unix/X{num}")
        # Skip sockets from servers we do not own: we only take a clean number.
        if sock.exists() or Path(f"/tmp/.X{num}-lock").exists():
            continue
        try:
            proc = subprocess.Popen(
                ["Xvfb", f":{num}", "-screen", "0", "1280x800x24",
                 "-nolisten", "tcp", "-ac"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            log.warning("browser.xvfb_missing")
            return
        for _ in range(120):
            if proc.poll() is not None:
                break
            if sock.exists():
                os.environ["DISPLAY"] = f":{num}"
                os.environ["XAUTHORITY"] = ""
                try:
                    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    _STATE_FILE.write_text(
                        json.dumps({"display": f":{num}", "pid": proc.pid}))
                except Exception:
                    pass
                log.info("browser.xvfb_started", display=f":{num}", pid=proc.pid)
                return
            time.sleep(0.1)
        try:
            proc.kill()
        except Exception:
            pass
    log.warning("browser.xvfb_unavailable")


class _BrowserPool:
    """Reusable Playwright + Chromium context pool.

    Usage:
        async with BrowserPool.get_context() as (page, close):
            # ... do stuff with page ...
            # close() marks the context available for reuse
    """

    _LOCK_PATTERNS: tuple[str, ...] = (
        "SingletonLock", "SingletonCookie", "SingletonSocket",
    )

    @staticmethod
    def _cleanup_profile_locks(profile_dir: Path) -> None:
        """Remove Chrome singleton locks that prevent profile reuse."""
        for pattern in _BrowserPool._LOCK_PATTERNS:
            lock_path = profile_dir / pattern
            if lock_path.exists():
                try:
                    lock_path.unlink()
                except OSError:
                    pass
            # Also check Default subdir
            default_lock = profile_dir / "Default" / pattern
            if default_lock.exists():
                try:
                    default_lock.unlink()
                except OSError:
                    pass

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._profile_dir: Path | None = None
        self._lock = asyncio.Lock()
        self._in_use = False
        self._expiry_task: asyncio.Task[None] | None = None
        self._pw_headless: bool = True
        self._closed = False

    # ── Public API ──────────────────────────────────────────────────────

    async def get_page(self) -> tuple[Page, BrowserContext]:
        """Return a ready (page, context) pair, creating or reusing as needed.

        The caller MUST call :meth:`release` when done so the pool can reuse
        the browser.
        """

        async with self._lock:
            if self._in_use:
                # Already in use — create a temporary incognito context
                # so the caller doesn't block. This is rare (shouldn't happen
                # with MCP's serial tool execution).
                temp_ctx = await self._start_new_context()
                page = temp_ctx.pages[0] if temp_ctx.pages else await temp_ctx.new_page()
                return page, temp_ctx

            if self._ctx is None or self._closed:
                await self._start()

            self._in_use = True
            self._cancel_expiry()
            assert self._page is not None
            assert self._ctx is not None
            return self._page, self._ctx

    async def release(self, ctx: BrowserContext) -> None:
        """Return the context to the pool or close it if it's not ours."""
        async with self._lock:
            if ctx is self._ctx:
                self._in_use = False
                self._schedule_expiry()
                log.debug("browser.context_released")
            else:
                # Temp context — close the browser too, not just the context
                browser = getattr(ctx, "_pool_browser", None)
                await ctx.close()
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

    async def close(self) -> None:
        """Shut down the pool and release all resources."""
        async with self._lock:
            self._closed = True
            self._cancel_expiry()
            if self._ctx:
                try:
                    await self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
                self._page = None
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            log.info("browser.pool_closed")

    # ── Internal ────────────────────────────────────────────────────────

    async def _start(self) -> None:
        """Create the persistent browser context, auto-cleaning locks."""
        self._profile_dir = resolve_profile()
        # Clean stale singleton locks before launch
        self._cleanup_profile_locks(self._profile_dir)
        channel = channel_for_profile(self._profile_dir)

        self._pw_headless = _headless_mode()
        if not self._pw_headless:
            # Must happen BEFORE async_playwright().start(): the Node driver
            # snapshots the environment at spawn time.
            _ensure_display()

        self._pw = await async_playwright().start()
        headless = self._pw_headless
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=headless,
            channel=channel or None,
            args=BROWSER_ARGS,
            viewport=VIEWPORT,
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        log.info("browser.pool_started", profile=str(self._profile_dir),
                 channel=channel, headless=headless)

    async def _start_new_context(self) -> BrowserContext:
        """Create a temporary non-persistent context (fallback when pool busy)."""
        if self._pw is None:
            self._pw = await async_playwright().start()
        browser = await self._pw.chromium.launch(
            headless=self._pw_headless,
            args=["--no-sandbox", "--disable-gpu"],
        )
        ctx = await browser.new_context(viewport=VIEWPORT)
        # Store a reference so release() can close both context + browser
        ctx._pool_browser = browser  # type: ignore[attr-defined]
        return ctx  # caller must release

    def _schedule_expiry(self) -> None:
        if BROWSER_IDLE_TIMEOUT_S > 0:
            self._expiry_task = asyncio.create_task(self._expiry_loop())

    def _cancel_expiry(self) -> None:
        if self._expiry_task:
            self._expiry_task.cancel()
            self._expiry_task = None

    async def _expiry_loop(self) -> None:
        """Close the pool after idle timeout."""
        try:
            await asyncio.sleep(BROWSER_IDLE_TIMEOUT_S)
            async with self._lock:
                if not self._in_use and not self._closed:
                    await self.close()
        except asyncio.CancelledError:
            pass

    # ── Context manager helper ──────────────────────────────────────────

    async def __aenter__(self) -> _BrowserPool:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


# Module-level singleton
_pool = _BrowserPool()


async def acquire_page() -> tuple[Page, BrowserContext]:
    """Acquire a (page, context) from the pool.

    Call ``release_context(ctx)`` when done.
    """
    return await _pool.get_page()


async def release_context(ctx: BrowserContext) -> None:
    """Return a context to the pool."""
    await _pool.release(ctx)


async def close_pool() -> None:
    """Shut down the browser pool (call on server shutdown)."""
    await _pool.close()
