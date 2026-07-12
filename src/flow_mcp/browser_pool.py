"""Singleton browser pool — keeps a Playwright context alive across calls.

Reusing the browser context avoids the ~15s overhead of launching Chrome,
loading the profile, and navigating to Flow on every generation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import structlog
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from flow_mcp.browser import ensure_xvfb
from flow_mcp.chrome_helpers import channel_for_profile
from flow_mcp.constants import BROWSER_ARGS, BROWSER_IDLE_TIMEOUT_S, VIEWPORT
from flow_mcp.profile import resolve_profile

log = structlog.get_logger("flow-mcp")


class _BrowserPool:
    """Reusable Playwright + Chromium context pool.

    Usage:
        async with BrowserPool.get_context() as (page, close):
            # ... do stuff with page ...
            # close() marks the context available for reuse
    """

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._profile_dir: Path | None = None
        self._lock = asyncio.Lock()
        self._in_use = False
        self._expiry_task: asyncio.Task[None] | None = None
        self._closed = False

    # ── Public API ──────────────────────────────────────────────────────

    async def get_page(self) -> tuple[Page, BrowserContext]:
        """Return a ready (page, context) pair, creating or reusing as needed.

        The caller MUST call :meth:`release` when done so the pool can reuse
        the browser.
        """
        ensure_xvfb()

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
                # Temp context, close it
                await ctx.close()

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
        """Create the persistent browser context."""
        self._profile_dir = resolve_profile()
        channel = channel_for_profile(self._profile_dir)

        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=False,
            channel=channel or None,
            args=BROWSER_ARGS,
            viewport=VIEWPORT,
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        log.info("browser.pool_started", profile=str(self._profile_dir), channel=channel)

    async def _start_new_context(self) -> BrowserContext:
        """Create a temporary non-persistent context (fallback when pool busy)."""
        if self._pw is None:
            self._pw = await async_playwright().start()
        browser = await self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        ctx = await browser.new_context(viewport=VIEWPORT)
        return ctx  # caller must close

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
