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
from flow_mcp.generator import GenerationError, chunk_prompts, generate_images

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
        "For MORE than 4 images use `generate_batch` instead. "
        "`seeds` pins the RNG (same prompt+model+seed = reproducible image). "
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
    seeds: list[int] | None = None,
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
        seeds: Optional list of seeds, one per image (per prompt when ``prompts``
            is given). Same prompt + model + seed reproduces the image.
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
            seeds=seeds,
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


# ── Batch tool (lotes de más de 4 imágenes) ──────────────────────────────

#: Tope de prompts por lote: protege contra llamadas que durarían horas.
_MAX_BATCH = int(os.environ.get("FLOW_MAX_BATCH", "40"))


@server.tool(
    name="generate_batch",
    description=(
        "Generate MANY images from a list of prompts of ANY length. Splits the "
        "list into rounds of 4 (Flow's per-request limit) and runs them "
        "sequentially on the same authenticated browser session. Flow sometimes "
        "returns fewer images than items in a round, so missing prompts get ONE "
        "automatic retry. Returns JSON with one entry per prompt (index, prompt, "
        "path, kb) plus `missing` for anything that still failed; a failed round "
        "does not abort the rest. ~20-25 s per round of 4. Use `generate_image` "
        "for 1–4 images (one round, cheaper); use this for big variation sets, "
        "storyboards or logo sheets. `seeds` pins one RNG seed per prompt."
    ),
)
async def generate_batch(
    prompts: list[str],
    model: ALLOWED_MODELS = "nano-pro",  # type: ignore[assignment]
    aspect: ALLOWED_ASPECTS = "9:16",  # type: ignore[assignment]
    reference_image: str | None = None,
    resolution: str = "1k",
    seeds: list[int] | None = None,
    ctx: Context | None = None,
) -> str:
    """Generate any number of images (4 per Flow round, sequential + retry)."""
    clean = [p.strip() for p in prompts if isinstance(p, str) and p.strip()]
    if not clean:
        return json.dumps({"success": False, "error": "`prompts` está vacío."})
    if len(clean) > _MAX_BATCH:
        return json.dumps({
            "success": False,
            "error": f"Máximo {_MAX_BATCH} prompts por lote (recibí {len(clean)}).",
        })

    mdl = model.lower()
    if mdl not in MODELS:
        return json.dumps({
            "success": False,
            "error": f"Invalid model '{mdl}'. Valid: {', '.join(MODELS)}",
        })
    asp = aspect.lower()
    if asp not in ASPECT_RATIOS:
        return json.dumps({
            "success": False,
            "error": f"Invalid aspect '{asp}'. Valid: {', '.join(ASPECT_RATIOS)}",
        })
    resn = resolution.lower()
    if resn not in ("1k", "2k", "4k"):
        return json.dumps({
            "success": False,
            "error": f"Invalid resolution '{resn}'. Valid: 1k, 2k, 4k",
        })
    if seeds is not None and len(seeds) < len(clean):
        return json.dumps({
            "success": False,
            "error": f"seeds trae {len(seeds)} valores para {len(clean)} prompts.",
        })

    output_dir = os.environ.get("GFLOW_OUTPUT_DIR")
    rounds = chunk_prompts(clean, 4)
    log.info(
        "generate_batch.called",
        prompts=len(clean),
        rounds=len(rounds),
        model=mdl,
        aspect=asp,
    )

    images: list[dict[str, object]] = []
    round_errors: list[dict[str, object]] = []
    done = 0

    def _report(current: int, total: int, msg: str) -> None:
        if ctx is None:
            return
        try:
            import asyncio

            asyncio.ensure_future(
                ctx.report_progress(
                    progress=float(min(done + current, len(clean))),
                    total=float(len(clean)),
                    message=msg,
                )
            )
        except Exception:
            pass

    async def _round(
        chunk: list[str],
        chunk_seeds: list[int] | None,
        label: str,
    ) -> tuple[list[Path], str | None]:
        """Una tanda de ≤4 prompts; devuelve (archivos, error)."""
        try:
            res = await generate_images(
                prompts=chunk,
                seeds=chunk_seeds,
                model=mdl,
                aspect=asp,
                output_dir=output_dir,
                reference_image=reference_image,
                resolution=resn,
                _progress_cb=lambda c, t, m: _report(c, t, f"{label} {m}"),
            )
            return list(res.files), None
        except Exception as exc:
            log.warning("generate_batch.round_failed", label=label, error=str(exc)[:300])
            return [], str(exc)

    def _collect(chunk: list[str], files: list[Path], indexes: list[int]) -> None:
        for offset, path in enumerate(files):
            if offset >= len(chunk):
                break
            images.append({
                "index": indexes[offset],
                "prompt": chunk[offset],
                "path": str(path.resolve()),
                "kb": round(path.stat().st_size / 1024),
            })

    # ── Rondas principales ────────────────────────────────────────────
    for round_no, chunk in enumerate(rounds, start=1):
        files, err = await _round(
            chunk,
            seeds[done:done + len(chunk)] if seeds else None,
            f"[{round_no}/{len(rounds)}]",
        )
        _collect(chunk, files, list(range(done, done + len(chunk))))
        if err:
            round_errors.append({"round": round_no, "prompts": chunk, "error": err})
        elif len(files) < len(chunk):
            round_errors.append({
                "round": round_no,
                "error": f"Flow devolvió {len(files)} de {len(chunk)}",
                "prompts": chunk[len(files):],
                "_short": True,
            })
        done += len(chunk)

    # ── Un reintento para lo que quedó sin imagen ─────────────────────
    produced = {img["prompt"] for img in images}
    missing = [(i, p) for i, p in enumerate(clean) if p not in produced]
    retried = len(missing)
    if missing:
        log.info("generate_batch.retry", missing=len(missing))
        for start in range(0, len(missing), 4):
            pair = missing[start:start + 4]
            chunk = [p for _, p in pair]
            chunk_seeds = [seeds[i] for i, _ in pair] if seeds else None
            files, err = await _round(chunk, chunk_seeds, f"[reintento {start // 4 + 1}]")
            _collect(chunk, files, [i for i, _ in pair])
            if err:
                round_errors.append({"round": "retry", "prompts": chunk, "error": err})

    produced = {img["prompt"] for img in images}
    unresolved = [p for p in clean if p not in produced]

    payload: dict[str, object] = {
        "success": not unresolved,
        "requested": len(clean),
        "generated": len(images),
        "rounds": len(rounds),
        "model": mdl,
        "aspect": asp,
        "resolution": resn,
        "images": images,
    }
    if retried:
        payload["retried"] = retried
    if unresolved:
        payload["missing"] = unresolved
        # Solo los fallos que dejaron prompts sin imagen: las rondas cortas que
        # el reintento resolvió son ruido para quien lee el resultado.
        real_errors = [e for e in round_errors if not e.get("_short")]
        if real_errors:
            payload["errors"] = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in real_errors
            ]
    log.info(
        "generate_batch.complete",
        requested=len(clean),
        generated=len(images),
        missing=len(unresolved),
    )
    return json.dumps(payload, ensure_ascii=False)


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
