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
try:
    from fastmcp import Context, FastMCP
except ImportError:  # mcp<2, ruta original
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Context

from flow_mcp import __version__
from flow_mcp.constants import (
    ALLOWED_ASPECTS,
    ALLOWED_MODELS,
    ASPECT_RATIOS,
    MODELS,
    SERVER_NAME,
)
from flow_mcp.generator import GenerationError, generate_images

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
        "Generate images with Google Flow (flow.google.com) through its "
        "batchexecute transport, driven from a persistent Playwright browser "
        "session with your saved authentication — no Flow Agent chat quota. "
        "Use `flow-mcp auth login` to authenticate."
    ),
)
server._mcp_server.version = __version__  # type: ignore[reportPrivateUsage]


# ── Tool ────────────────────────────────────────────────────────────────


@server.tool(
    name="generate_image",
    description=(
        "Generate images with Google Flow (batchexecute transport). "
        "Bypasses the Flow Agent chat quota (~10/day) by driving the real "
        "Flow page from a browser context with your saved authentication. "
        "Supports text-to-image and image-to-image (pass a reference_image path). "
        "Supports upscale to 2K/4K (pass resolution parameter). "
        "BATCH: pass `prompts` (1–4 different prompts) to get several distinct "
        "images from ONE batched call (fastest for variants); or use `count` "
        "(1–4) for N takes of the same prompt. Max 4 images per call. "
        "Models: nano-pro (default), nano2 (fast), narwhal, gem_pix_2. "
        "Aspects: 9:16 (default portrait), 16:9 (landscape), 1:1 (square), "
        "4:3, 3:4. "
        "Resolutions: 1k (default, original), 2k, 4k (requires Ultra)."
    ),
)
async def generate_image(
    prompt: str | None = None,
    model: ALLOWED_MODELS = "nano-pro",  # type: ignore[assignment]
    count: int = 1,
    aspect: ALLOWED_ASPECTS = "9:16",  # type: ignore[assignment]
    prompts: list[str] | None = None,
    reference_image: str | None = None,
    resolution: str = "1k",
    ctx: Context | None = None,
) -> str:
    """Generate one or more images using Google Flow.

    Args:
        prompt: Text description of the image to generate.
        model: Model to use.
        count: Number of images (1–4, default 1) for the same prompt.
        aspect: Aspect ratio.
        prompts: List of 1–4 different prompts — each yields its own image in
            one batched call. Overrides ``prompt`` and ``count``.
        reference_image: Optional path to a local image file for I2I.
        resolution: Output resolution. "1k" (default, original), "2k", or "4k".
            4K requires a Flow Ultra subscription.
        ctx: FastMCP context (injected automatically) for progress reporting.

    Returns:
        Human-readable result with paths to the generated files.
    """
    # ── Validate ──────────────────────────────────────────────────────
    if prompts:
        prompts = [p.strip() for p in prompts if isinstance(p, str) and p.strip()]
        if not prompts:
            return json.dumps({
                "success": False,
                "error": "`prompts` está vacío: pasa al menos un prompt.",
            })
        if len(prompts) > 4:
            return json.dumps({
                "success": False,
                "error": f"Máximo 4 prompts por llamada (recibí {len(prompts)}).",
            })
        prompt = prompts[0]
        count = len(prompts)
    elif not prompt or not prompt.strip():
        return json.dumps({
            "success": False,
            "error": "Falta `prompt` (o pasa `prompts` para varias imágenes).",
        })

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
        prompt=(prompt or "")[:80],
        model=model,
        count=count,
        aspect=aspect,
        batch=len(prompts) if prompts else 1,
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
            prompts=prompts,
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


if __name__ == "__main__":
    main()
