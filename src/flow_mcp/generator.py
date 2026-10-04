"""Core image generation logic — project creation, API call, download."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

import structlog
from playwright.async_api import Page

from flow_mcp import flow_batch as fb
from flow_mcp.browser import capture_bearer_token
from flow_mcp.browser_pool import acquire_page, release_context
from flow_mcp.constants import (
    ASPECT_RATIOS,
    GEN_POLL_INTERVAL_MS,
    GEN_TIMEOUT_MS,
    MODELS,
    UPLOAD_POLL_INTERVAL_MS,
    UPLOAD_TIMEOUT_MS,
)
from flow_mcp.js_templates import (
    DOWNLOAD_IMAGE_JS,
    LIST_PROJECTS_JS,
    check_session_js,
    create_project_js,
    generate_images_js,
    upscale_image_js,
    upload_image_js,
)
from flow_mcp.recaptcha import TokenMinter

log = structlog.get_logger("flow-mcp")

# ── Env-based overrides ───────────────────────────────────────────────────
_GEN_TIMEOUT = int(os.environ.get("FLOW_GEN_TIMEOUT_MS", str(GEN_TIMEOUT_MS)))
_GEN_POLL = int(os.environ.get("FLOW_GEN_POLL_MS", str(GEN_POLL_INTERVAL_MS)))
_NAV_TIMEOUT = int(os.environ.get("FLOW_NAV_TIMEOUT_MS", "60000"))
_REUSE_PROJECTS = os.environ.get("FLOW_REUSE_PROJECTS", "1") == "1"
#: Intentos por generación: cada uno mintea un captcha nuevo (son de un solo uso).
_GEN_ATTEMPTS = int(os.environ.get("FLOW_GEN_ATTEMPTS", "2"))
#: Tope de la respuesta de generación (las URLs firmadas del CDN van inline).
_MAX_GEN_CHARS = int(os.environ.get("FLOW_GEN_MAX_CHARS", "2000000"))

# ── Errors ────────────────────────────────────────────────────────────────


class GenerationError(RuntimeError):
    """Base for generation failures."""


class ContentFilteredError(GenerationError):
    """Prompt was silently blocked by Google's content filter."""


class AuthError(GenerationError):
    """Bearer token or session expired."""


class RateLimitedError(GenerationError):
    """HTTP 429 — too many requests."""


# ── Result type ───────────────────────────────────────────────────────────


class GenerationResult:
    """Result of a successful image generation."""

    def __init__(self, files: list[Path]) -> None:
        self.files = files

    @property
    def paths(self) -> list[str]:
        return [str(f.resolve()) for f in self.files]

    def describe(self) -> str:
        lines = [f"✅ Generated {len(self.files)} image(s):"]
        for f in self.files:
            size = f.stat().st_size
            lines.append(f"  📁 {f.resolve()} ({size / 1024:.0f} KB)")
        lines.append(
            "\nYou can view these files in your file manager or reference them by path."
        )
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────


async def _await_window_var(
    page: Page,
    var_name: str,
    *,
    poll_ms: int = _GEN_POLL,
    timeout_ms: int = _GEN_TIMEOUT,
) -> Any:
    """Poll ``window[var_name]`` until it is not ``None``."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        val = await page.evaluate(f"window[{json.dumps(var_name)}]")
        if val is not None:
            return val
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_ms / 1000)


async def _refresh_token(page: Page) -> str:
    """Re-capture the Bearer token by navigating to Flow again."""
    log.info("auth.refreshing")
    return await capture_bearer_token(page)


def _sleep_with_backoff(attempt: int, base: float = 2.0, max_sleep: float = 30.0) -> float:
    """Exponential backoff: 2^attempt seconds, capped at max_sleep, with jitter."""
    delay = min(base ** attempt, max_sleep)
    jitter = random.uniform(0, delay * 0.25)
    return delay + jitter


# ── Project management ────────────────────────────────────────────────────


# ── Reference image upload ────────────────────────────────────────────────


_MIME_EXT_BY_SUFFIX: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


async def _upload_reference_image(
    page: Page,
    pid: str,
    ref_path: Path,
    minter: TokenMinter,
) -> str:
    """Sube una imagen local al proyecto y devuelve su media id.

    Los bytes viajan dentro del RPC ``maseQ`` como base64 plano (sin endpoint
    aparte) y la llamada lleva captcha, igual que una generación. El media id
    resultante se pasa como imagen base de la generación (I2I).
    """
    _prog_local(3, 8, f"Uploading reference image ({ref_path.name})...")
    mime = _MIME_EXT_BY_SUFFIX.get(ref_path.suffix.lower(), "image/jpeg")
    try:
        img_b64 = base64.b64encode(ref_path.read_bytes()).decode("ascii")
        captcha = await minter.mint(fb.CAPTCHA_IMAGE)
        envelope = fb.apply_captcha(
            fb.upload_request(img_b64, pid, mime, ref_path.name), captcha
        )
        payload = await fb.rpc_payload(
            page, fb.RPC_UPLOAD_IMAGE, envelope, max_chars=1_000_000
        )
        media_id = fb.read_uploaded_media_id(payload)
    except Exception as exc:
        raise GenerationError(f"Failed to upload reference image: {exc}") from exc
    log.info("i2i.reference_uploaded", media_id=media_id, path=str(ref_path))
    return media_id


_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _guess_ext_from_mime(b64_data: str) -> str:
    """Guess file extension from base64 data URL or raw base64."""
    if b64_data.startswith("data:"):
        mime = b64_data.split(";")[0].split(":")[1] if ":" in b64_data else ""
        return _MIME_EXT.get(mime, "jpg")
    return "jpg"


def tempfile_dir() -> str:
    """Cross-platform temp directory path."""
    import tempfile as _tf
    return _tf.gettempdir()


def _pinned_seed() -> int | None:
    """Semilla fija opcional para reproducir la misma cara entre generaciones.

    Se lee de ``~/.flow-mcp/seed`` (un entero). Sin archivo → semilla aleatoria
    (cada tanda sale distinta). Con archivo → reproducible: mismo prompt y misma
    semilla dan la misma imagen, que es lo que mantiene consistente un set.
    """
    try:
        raw = (Path.home() / ".flow-mcp" / "seed").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


async def _upscale_image_batch(
    page: Page,
    media_id: str,
    resolution: str,
    minter: TokenMinter,
) -> str:
    """Escala una imagen ya generada (RPC ``SPrCad``) y devuelve su base64.

    Flow devuelve el bitmap escalado dentro del propio envelope; no hay
    endpoint de descarga que consultar.
    """
    captcha = await minter.mint(fb.CAPTCHA_IMAGE)
    envelope = fb.apply_captcha(fb.image_upscale_request(media_id, resolution), captcha)
    payload = await fb.rpc_payload(
        page, fb.RPC_UPSCALE_IMAGE, envelope, max_chars=32_000_000
    )
    return fb.read_upscaled_image(payload)


# ═════════════════════════════════════════════════════════════════════════
#  API call with retry, backoff for 429, and token refresh for 401
# ═════════════════════════════════════════════════════════════════════════


async def _call_api_with_retry(
    page: Page,
    api_url: str,
    bearer: str,
    body_json: str,
    *,
    max_retries: int = 3,
) -> tuple[str | None, str]:
    """Call ``batchGenerateImages`` with intelligent retry logic.

    Retry strategy:
    - 401 → refresh token, retry immediately
    - 429 → exponential backoff (2s → 4s → 8s), then retry
    - 5xx → exponential backoff, then retry
    - 403 → raise immediately (no point retrying)
    - Other → raise immediately

    Returns ``(raw_response_text_or_None, updated_bearer)``.
    """
    current_bearer = bearer
    for attempt in range(max_retries + 1):
        js = generate_images_js(api_url, current_bearer, body_json)
        await page.add_script_tag(content=js)

        raw = await _await_window_var(
            page, "__gen",
            poll_ms=_GEN_POLL,
            timeout_ms=_GEN_TIMEOUT,
        )

        if raw is None:
            return None, current_bearer

        if not isinstance(raw, str):
            return raw, current_bearer

        if raw.startswith("HTTP_401:"):
            if attempt < max_retries:
                log.info("auth.401_retry", attempt=attempt + 1)
                current_bearer = await _refresh_token(page)
                continue
            raise AuthError(
                f"HTTP 401 persisted after {max_retries} retries — session expired. "
                "Run `flow-mcp auth login` to refresh."
            )

        if raw.startswith("HTTP_429:"):
            if attempt < max_retries:
                delay = _sleep_with_backoff(attempt, base=2.0)
                log.info("api.429_retry", attempt=attempt + 1, delay_s=round(delay, 1))
                await asyncio.sleep(delay)
                continue
            raise RateLimitedError(
                "HTTP 429 Too Many Requests persisted after retries. "
                "Wait a few minutes before generating again."
            )

        if raw.startswith("HTTP_403:"):
            raise GenerationError(
                "HTTP 403 Forbidden — your account may not have access "
                "to this model or feature."
            )

        if raw.startswith("HTTP_5XX:"):
            if attempt < max_retries:
                delay = _sleep_with_backoff(attempt, base=3.0)
                log.info("api.5xx_retry", attempt=attempt + 1, delay_s=round(delay, 1))
                await asyncio.sleep(delay)
                continue
            raise GenerationError(
                f"Server error persisted after retries: {raw[:200]}"
            )

        if raw.startswith("ERR:"):
            raise GenerationError(f"API fetch failed: {raw}")

        return raw, current_bearer

    return None, current_bearer


# ═════════════════════════════════════════════════════════════════════════
#  Upscale
# ═════════════════════════════════════════════════════════════════════════


async def _upscale_image(
    page: Page,
    bearer: str,
    project_id: str,
    media_id: str,
    target_resolution: str,
    recaptcha_token: str,
) -> str | None:
    """Upscale a generated image via ``POST /v1/flow/upsampleImage``.

    Returns the base64-encoded image data, or ``None`` if the upscale failed
    (e.g. 4K requires Ultra subscription).
    """
    sid = ";" + str(int(time.time() * 1000))
    body = json.dumps({
        "mediaId": media_id,
        "targetResolution": target_resolution,
        "clientContext": {
            "recaptchaContext": {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
            "projectId": project_id,
            "sessionId": sid,
            "tool": "PINHOLE",
        },
    })

    js = upscale_image_js(bearer, json.dumps(body))
    await page.add_script_tag(content=js)

    result = await _await_window_var(
        page, "__up_img",
        poll_ms=2000, timeout_ms=60_000,
    )
    if not result or (isinstance(result, str) and (result.startswith("HTTP_") or result.startswith("ERR:"))):
        log.warning("upscale.api_failed", media_id=media_id, result=str(result)[:100])
        return None

    return result


async def _upscale_image_with_retry(
    page: Page,
    bearer: str,
    project_id: str,
    media_id: str,
    target_resolution: str,
    recaptcha_token: str,
    *,
    max_retries: int = 2,
) -> str | None:
    """Wrap ``_upscale_image`` with bearer token refresh on failure."""
    current_bearer = bearer
    for attempt in range(max_retries + 1):
        try:
            result = await _upscale_image(
                page, current_bearer, project_id, media_id,
                target_resolution, recaptcha_token,
            )
            if result is not None:
                return result
        except Exception:
            pass
        if attempt < max_retries:
            log.info("upscale.retry", attempt=attempt + 1)
            current_bearer = await _refresh_token(page)
        else:
            break
    return None


# _prog_local is used by _upload_reference_image before generate_images is called
# It's a no-op placeholder since we don't have a progress callback at that scope
def _prog_local(n: int, t: int, msg: str) -> None:
    pass


# ═════════════════════════════════════════════════════════════════════════
#  Main generator
# ═════════════════════════════════════════════════════════════════════════


async def generate_images(
    prompt: str,
    model: str = "nano-pro",
    count: int = 1,
    aspect: str = "9:16",
    output_dir: str | Path | None = None,
    *,
    _progress_cb: Callable[[int, int, str], None] | None = None,
    reference_image: str | None = None,
    resolution: str = "1k",
) -> GenerationResult:
    """Generate images through Google Flow's batchexecute transport.

    Uses the pooled browser context — first call opens the browser,
    subsequent calls reuse it.

    Parameters
    ----------
    prompt:
        Text description of the image to generate.
    model:
        Model name (nano-pro, nano2, narwhal, gem_pix_2).
    count:
        Maximum number of images to download (1–4). The API typically
        returns a single image per call; any excess fifeUrls in the
        response are used to fulfill the requested count.
    aspect:
        Aspect ratio (9:16, 16:9, 1:1, etc).
    output_dir:
        Where to save generated images.
    reference_image:
        Optional path to a local image file to use as Image-to-Image reference.
    resolution:
        Output resolution: "1k" (default, original), "2k", "4k".
        4K requires a Flow Ultra subscription.
    _progress_cb:
        Optional callback ``(current, total, message)`` for progress reporting.

    Raises
        AuthError, ContentFilteredError, GenerationError
    """
    timestamp = str(int(time.time() * 1000))
    do_upscale = resolution.lower() in ("2k", "4k")
    wire_resolution = {"2k": "2K", "4k": "4K", "1k": ""}[resolution.lower()]

    (page, ctx) = await acquire_page()

    def _prog(n: int, t: int, msg: str) -> None:
        if _progress_cb:
            _progress_cb(n, t, msg)

    try:
        # 1. Pestaña de Flow lista: tokens `at` + grecaptcha (migración sep-2026).
        _prog(1, 7, "Opening Google Flow project...")
        await fb.wait_for_flow_ready(page)
        pid = await fb.ensure_project(page, "flow-mcp")
        await fb.open_project(page, pid)
        minter = TokenMinter(page)

        # 2. Imagen de referencia (I2I) opcional: se sube al proyecto y viaja
        #    como media id dentro del request de generación.
        base_media_id: str | None = None
        if reference_image:
            ref_path = Path(reference_image)
            if not ref_path.exists():
                raise GenerationError(f"Reference image not found: {reference_image}")

            _prog(3, 7, f"Uploading reference image ({ref_path.name})...")
            base_media_id = await _upload_reference_image(page, pid, ref_path, minter)

        # 4. Generación por batchexecute (ogiZ0b): un captcha nuevo por intento,
        #    porque los tokens de reCAPTCHA son de un solo uso.
        _prog(4, 7, "Requesting image generation...")
        images: list[fb.GeneratedImage] = []
        last_error = "sin intentos"
        for attempt in range(1, _GEN_ATTEMPTS + 1):
            if attempt > 1:
                await asyncio.sleep(_sleep_with_backoff(attempt))
                _prog(4, 7, f"Retrying generation ({attempt}/{_GEN_ATTEMPTS})...")
            try:
                captcha = await minter.mint(fb.CAPTCHA_IMAGE)
                envelope = fb.apply_captcha(
                    fb.image_request(
                        prompt, pid, count=count, aspect=aspect, model=model,
                        base_media_id=base_media_id, seed=_pinned_seed(),
                    ),
                    captcha,
                )
                _, raw = await fb.run_rpc(
                    page, fb.RPC_GEN_IMAGE, envelope, max_chars=_MAX_GEN_CHARS
                )
                images = fb.read_images(fb.first_payload(raw, fb.RPC_GEN_IMAGE))
                if images:
                    break
                last_error = f"respuesta sin imágenes ({len(raw)} chars)"
            except fb.RpcError as exc:
                detail = str(exc)
                # Solo SAFETY/FILTER son de contenido (no insistir). PUBLIC_ERROR_* es
                # transitorio (captcha/actividad): va al retry con captcha nuevo.
                if any(k in detail.upper() for k in ("SAFETY", "FILTER", "PROHIBITED")):
                    raise ContentFilteredError(f"Flow rechazó el prompt: {detail}") from exc
                last_error = detail
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            log.warning("gen.attempt_failed", attempt=attempt, error=last_error[:300])

        if not images:
            raise GenerationError(f"Flow no devolvió imágenes — {last_error}")
        urls = [img.url for img in images]
        media_ids = [img.media_id for img in images]

        _prog(5, 7, "Processing API response...")
        log.info("images.found", count=len(urls), media_ids=len(media_ids))

        # Download (and optionally upscale) each image
        _prog(7, 7, f"Downloading {min(len(urls), count)} image(s)...")
        out = Path(output_dir) if output_dir else Path(tempfile_dir())
        out.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        max_dl = min(len(urls), count)

        for idx, url in enumerate(urls):
            url = url.replace("\\u0026", "&")

            if do_upscale and idx < len(media_ids):
                try:
                    up_b64 = await _upscale_image_batch(
                        page, media_ids[idx], wire_resolution, minter,
                    )
                except Exception as exc:
                    log.warning("upscale.failed", idx=idx, error=str(exc))
                    up_b64 = None

                if up_b64:
                    ext = _guess_ext_from_mime(up_b64)
                    filepath = out / f"flow-gen-{timestamp}-{idx}-{resolution}.{ext}"
                    filepath.write_bytes(base64.b64decode(up_b64))
                    saved.append(filepath)
                    log.info("image.upscaled", path=str(filepath), idx=idx, resolution=resolution)
                    _prog(idx + 1, max_dl, f"Downloading upscaled image {idx + 1}...")
                    if len(saved) >= count:
                        break
                    continue

            # Download original
            try:
                b64_data = await page.evaluate(DOWNLOAD_IMAGE_JS, url)
            except Exception as exc:
                log.warning("download.failed", url=url[:80], error=str(exc))
                continue

            if b64_data and b64_data.startswith("data:"):
                img_raw = b64_data.split(",", 1)[1]
                ext = b64_data.split(";")[0].split("/")[1] or "webp"
                filepath = out / f"flow-gen-{timestamp}-{idx}.{ext}"
                filepath.write_bytes(base64.b64decode(img_raw))
                saved.append(filepath)
                log.info("image.saved", path=str(filepath), idx=idx)
                _prog(idx + 1, max_dl, f"Saving image {idx + 1}...")
                if len(saved) >= count:
                    break

        if not saved:
            raise GenerationError(f"Downloaded 0 images — {last_error}")

        return GenerationResult(files=saved)

    finally:
        await release_context(ctx)
