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


# ── Video constants ─────────────────────────────────────────────────────
# Wire format from the API captured during a real Flow video generation
# (Jul 2026, ceasr1 profile, model "abra_t2v_4s" = Veo 2 Fast, 4s, 720p).
#
# Models: each key is what the user passes to the MCP tool; the value is
# the ``videoModelKey`` the API expects. "veo-fast" is the cheapest, "veo"
# is the standard, "veo-hq" is the highest quality (and most expensive).
#
# We only know one model key for sure ("abra_t2v_4s") but the API also
# accepts "veo-3.0-generate-preview" / "veo-3.0-fast-generate-preview" for
# the public Veo 3 family. We expose them under friendly aliases.
VIDEO_MODELS: Final[dict[str, str]] = {
    "veo-fast":      "veo-3.0-fast-generate-preview",   # cheapest, fastest
    "veo-2-fast":    "abra_t2v_4s",                     # confirmed working
    "veo":           "veo-3.0-generate-preview",        # standard quality
    "veo-hq":        "veo-3.0-generate-preview",        # alias; same as veo
}
ALLOWED_VIDEO_MODELS = Literal["veo-fast", "veo-2-fast", "veo", "veo-hq"]

VIDEO_ASPECT_RATIOS: Final[dict[str, str]] = {
    "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
    "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
    "1:1":  "VIDEO_ASPECT_RATIO_SQUARE",
}
ALLOWED_VIDEO_ASPECTS = Literal["9:16", "16:9", "1:1"]

# 4s is the cheapest; 6s and 8s cost more credits. We default to 4s.
VIDEO_DURATIONS: Final[tuple[int, ...]] = (4, 6, 8)

# Status values returned by batchCheckAsyncVideoGenerationStatus
VIDEO_STATUS_PENDING = "MEDIA_GENERATION_STATUS_SCHEDULED"
VIDEO_STATUS_ACTIVE  = "MEDIA_GENERATION_STATUS_ACTIVE"
VIDEO_STATUS_DONE    = "MEDIA_GENERATION_STATUS_SUCCESSFUL"
VIDEO_STATUS_FAILED  = "MEDIA_GENERATION_STATUS_FAILED"

# Timeouts — video is async and slow. 4s video typically takes 30-60s
# to render; 8s can take 90s+ on busy servers. 4 minutes is a safe upper
# bound.
VIDEO_POLL_INTERVAL_MS: Final[int] = 3_000
VIDEO_TIMEOUT_MS: Final[int] = 4 * 60 * 1000  # 4 minutes

# Audio: Flow's default. "BLOCK_SILENCED_VIDEOS" means if audio gen fails
# the whole clip is rejected (safer — never produces a silent video).
VIDEO_AUDIO_FAILURE_PREFERENCE: Final[str] = "BLOCK_SILENCED_VIDEOS"
