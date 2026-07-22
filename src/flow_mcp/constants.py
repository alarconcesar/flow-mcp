"""Constants shared across the flow-mcp package."""

from __future__ import annotations

from typing import Final, Literal

# ── Aspect ratio mapping ────────────────────────────────────────────────
ASPECT_RATIOS: Final[dict[str, str]] = {
    "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
    "4:3": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
    "3:4": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
}

ALLOWED_ASPECTS = Literal["9:16", "16:9", "1:1", "4:3", "3:4"]

# ── Model mapping ───────────────────────────────────────────────────────
MODELS: Final[dict[str, str]] = {
    "nano2": "NARWHAL",
    "nano-pro": "GEM_PIX_2",
    "narwhal": "NARWHAL",
    "gem_pix_2": "GEM_PIX_2",
}

ALLOWED_MODELS = Literal["nano2", "nano-pro", "narwhal", "gem_pix_2"]

# ── Playwright launch defaults ──────────────────────────────────────────
BROWSER_ARGS: Final[list[str]] = [
    "--no-sandbox",
    "--password-store=basic",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]

VIEWPORT: Final[dict[str, int]] = {"width": 1280, "height": 720}

# ── Timeouts ────────────────────────────────────────────────────────────
NAVIGATION_TIMEOUT_MS: Final[int] = 60_000
GEN_POLL_INTERVAL_MS: Final[int] = 2_000   # check every 2s instead of fixed 25s
GEN_TIMEOUT_MS: Final[int] = 90_000        # max total wait for generation
PROJECT_CREATE_WAIT_MS: Final[int] = 5_000

# ── MCP server identity ────────────────────────────────────────────────
SERVER_NAME: Final[str] = "flow-image-server"

# ── Upload ────────────────────────────────────────────────────────────────
UPLOAD_POLL_INTERVAL_MS: Final[int] = 1_000    # check every 1s
UPLOAD_TIMEOUT_MS: Final[int] = 20_000         # max wait for upload

# ── Browser pool ────────────────────────────────────────────────────────
BROWSER_IDLE_TIMEOUT_S: Final[int] = 0   # 0 = never expire (keeps MCP alive)
