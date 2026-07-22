"""FastMCP server — exposes ``generate_image`` as an MCP tool.

Uses Context injection from FastMCP for progress reporting and adds
Literal type hints so that ``inputSchema`` includes ``enum`` values
for better autocomplete in Claude Code.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context

from flow_mcp import __version__
from flow_mcp.constants import (
    ALLOWED_ASPECTS,
    ALLOWED_MODELS,
    ALLOWED_VIDEO_ASPECTS,
    ALLOWED_VIDEO_MODELS,
    ASPECT_RATIOS,
    MODELS,
    SERVER_NAME,
    VIDEO_ASPECT_RATIOS,
    VIDEO_DURATIONS,
    VIDEO_MODELS,
)
from flow_mcp.generator import (
    GenerationError,
    generate_images_with_fallback as generate_images,
    generate_video_with_fallback as generate_video,
)

log = structlog.get_logger("flow-mcp")


# ── Lifespan ────────────────────────────────────────────────────────────


@asynccontextmanager
async def server_lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Startup/shutdown: manage the browser pool lifecycle."""
    log.info("server.lifespan.start")
    try:
        yield
    finally:
        from flow_mcp.browser_pool import close_pool

        await close_pool()
        log.info("server.lifespan.shutdown")


# ── Server instance ─────────────────────────────────────────────────────

server = FastMCP(
    name=SERVER_NAME,
    lifespan=server_lifespan,
    instructions=(
        "Generate images via Google Flow's batchGenerateImages API. "
        "Uses a persistent Playwright browser session with your saved "
        "Flow authentication to call the API directly — no CLI "
        "quota consumption. "
        "Use `flow-mcp auth login` to authenticate."
    ),
)
server._mcp_server.version = __version__  # type: ignore[reportPrivateUsage]


# ── Tool ────────────────────────────────────────────────────────────────


@server.tool(
    name="generate_image",
    description=(
        "Generate images via Google Flow's batchGenerateImages API. "
        "Bypasses the Flow Agent chat quota (~10/day) by calling the "
        "API directly from a browser context with your saved authentication. "
        "Supports text-to-image and image-to-image (pass a reference_image path). "
        "Supports upscale to 2K/4K (pass resolution parameter). "
        "Models: nano-pro (default), nano2 (fast), narwhal, gem_pix_2. "
        "Aspects: 9:16 (default portrait), 16:9 (landscape), 1:1 (square), "
        "4:3, 3:4. "
        "Resolutions: 1k (default, original), 2k, 4k (requires Ultra)."
    ),
)
async def generate_image(
    prompt: str,
    model: ALLOWED_MODELS = "nano-pro",  # type: ignore[assignment]
    count: int = 1,
    aspect: ALLOWED_ASPECTS = "9:16",  # type: ignore[assignment]
    reference_image: str | None = None,
    resolution: str = "1k",
    ctx: Context | None = None,
) -> str:
    """Generate one or more images using Google Flow.

    Args:
        prompt: Text description of the image to generate.
        model: Model to use.
        count: Number of images (1–4, default 1).
        aspect: Aspect ratio.
        reference_image: Optional path to a local image file for I2I.
        resolution: Output resolution. "1k" (default, original), "2k", or "4k".
            4K requires a Flow Ultra subscription.
        ctx: FastMCP context (injected automatically) for progress reporting.

    Returns:
        Human-readable result with paths to the generated files.
    """
    # ── Validate ──────────────────────────────────────────────────────
    model = model.lower()
    if model not in MODELS:
        return json.dumps({
            "success": False,
            "error": f"Invalid model '{model}'. Valid: {', '.join(MODELS)}",
        })

    aspect = aspect.lower()
    if aspect not in ASPECT_RATIOS:
        return json.dumps({
            "success": False,
            "error": f"Invalid aspect '{aspect}'. Valid: {', '.join(ASPECT_RATIOS)}",
        })

    count = max(1, min(4, count))
    resolution = resolution.lower()
    if resolution not in ("1k", "2k", "4k"):
        return json.dumps({
            "success": False,
            "error": f"Invalid resolution '{resolution}'. Valid: 1k, 2k, 4k",
        })
    output_dir = os.environ.get("GFLOW_OUTPUT_DIR")

    log.info(
        "generate_image.called",
        prompt=prompt[:80],
        model=model,
        count=count,
        aspect=aspect,
    )

    # ── Progress callback ─────────────────────────────────────────────
    def _report(current: int, total: int, msg: str) -> None:
        if ctx is not None:
            try:
                import asyncio

                asyncio.ensure_future(
                    ctx.report_progress(
                        progress=float(current),
                        total=float(total),
                        message=msg,
                    )
                )
            except Exception:
                pass

    # ── Generate ──────────────────────────────────────────────────────
    try:
        result = await generate_images(
            prompt=prompt,
            model=model,
            count=count,
            aspect=aspect,
            output_dir=output_dir,
            reference_image=reference_image,
            resolution=resolution,
            _progress_cb=_report,
        )
        log.info("generate_image.complete", count=len(result.files))
        return result.describe()
    except GenerationError as exc:
        log.warning("generate_image.failed", error=str(exc))
        return f"❌ Generation failed: {exc}"
    except RuntimeError as exc:
        log.warning("generate_image.error", error=str(exc))
        return f"❌ Error: {exc}"
    except Exception as exc:
        log.error("generate_image.unexpected", error=str(exc))
        return f"❌ Unexpected error: {exc}"


# ── Entry point ─────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over stdio transport."""
    log.info("server.starting", name=SERVER_NAME)

    # Ensure UTF-8 pipes on Windows (used by gflow-cli internally)
    if sys.platform == "win32":
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(sys, stream_name)
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")

    server.run(transport="stdio")


# ── Video tool ──────────────────────────────────────────────────────────


@server.tool(
    name="generate_video",
    description=(
        "Generate videos via Google Flow's async batchGenerateVideoText API. "
        "Bypasses the Flow Agent chat quota by calling the API directly "
        "from a browser context with your saved authentication. "
        "Supports text-to-video and image-to-video (pass reference_image path). "
        "Models: veo-fast (cheapest, ~4s, 720p), veo (standard quality), "
        "veo-hq (highest quality, most expensive). "
        "Aspects: 9:16 (default portrait), 16:9 (landscape), 1:1 (square). "
        "Durations: 4 (default, cheapest), 6, 8 seconds. "
        "Multi-account fallback: rotates across GFLOW_ACCOUNTS when one "
        "runs out of credits."
    ),
)
async def generate_video_tool(
    prompt: str,
    model: ALLOWED_VIDEO_MODELS = "veo-fast",  # type: ignore[assignment]
    aspect: ALLOWED_VIDEO_ASPECTS = "9:16",  # type: ignore[assignment]
    duration: int = 4,
    reference_image: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Generate a video using Google Flow.

    Args:
        prompt: Text description of the video to generate.
        model: Model alias (default: "veo-fast" — cheapest).
            Use "veo" for standard quality, "veo-2-fast" for the
            confirmed-working Veo 2 Fast.
        aspect: Aspect ratio (9:16 default, 16:9, 1:1).
        duration: Length in seconds — 4, 6, or 8. Default 4 (cheapest).
        reference_image: Optional path to a local image for I2V.
        ctx: FastMCP context (injected automatically) for progress reporting.

    Returns:
        Human-readable result with path to the generated MP4 file.

    Cost warning:
        Video is significantly more expensive than image generation. A
        single 4s veo-fast clip costs ~20-50 credits. Plan accordingly
        and prefer the "veo-fast" + 4s combo unless quality is critical.
    """
    # ── Validate ──────────────────────────────────────────────────────
    model = model.lower()
    if model not in VIDEO_MODELS:
        return json.dumps({
            "success": False,
            "error": f"Invalid video model '{model}'. Valid: {', '.join(VIDEO_MODELS)}",
        })

    aspect = aspect.lower()
    if aspect not in VIDEO_ASPECT_RATIOS:
        return json.dumps({
            "success": False,
            "error": f"Invalid video aspect '{aspect}'. Valid: {', '.join(VIDEO_ASPECT_RATIOS)}",
        })

    if duration not in VIDEO_DURATIONS:
        return json.dumps({
            "success": False,
            "error": f"Invalid duration {duration}s. Valid: {', '.join(str(d) for d in VIDEO_DURATIONS)}",
        })

    output_dir = os.environ.get("GFLOW_OUTPUT_DIR")

    log.info(
        "generate_video.called",
        prompt=prompt[:80],
        model=model,
        aspect=aspect,
        duration=duration,
    )

    # ── Progress callback ─────────────────────────────────────────────
    def _report(current: int, total: int, msg: str) -> None:
        if ctx is not None:
            try:
                import asyncio

                asyncio.ensure_future(
                    ctx.report_progress(
                        progress=float(current),
                        total=float(total),
                        message=msg,
                    )
                )
            except Exception:
                pass

    # ── Generate ──────────────────────────────────────────────────────
    try:
        result = await generate_video(
            prompt=prompt,
            model=model,
            aspect=aspect,
            duration=duration,
            output_dir=output_dir,
            reference_image=reference_image,
            _progress_cb=_report,
        )
        log.info("generate_video.complete", count=len(result.files))
        return result.describe()
    except GenerationError as exc:
        log.warning("generate_video.failed", error=str(exc))
        return f"❌ Video generation failed: {exc}"
    except RuntimeError as exc:
        log.warning("generate_video.error", error=str(exc))
        return f"❌ Error: {exc}"
    except Exception as exc:
        log.error("generate_video.unexpected", error=str(exc))
        return f"❌ Unexpected error: {exc}"


if __name__ == "__main__":
    main()
