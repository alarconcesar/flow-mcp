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

from flow_mcp.account_manager import AccountCycleError, AccountManager
from flow_mcp.browser import capture_bearer_token
from flow_mcp.browser_pool import acquire_page, release_context
from flow_mcp.constants import (
    ASPECT_RATIOS,
    GEN_POLL_INTERVAL_MS,
    GEN_TIMEOUT_MS,
    MODELS,
    UPLOAD_POLL_INTERVAL_MS,
    UPLOAD_TIMEOUT_MS,
    VIDEO_ASPECT_RATIOS,
    VIDEO_AUDIO_FAILURE_PREFERENCE,
    VIDEO_DURATIONS,
    VIDEO_MODELS,
    VIDEO_POLL_INTERVAL_MS,
    VIDEO_STATUS_ACTIVE,
    VIDEO_STATUS_DONE,
    VIDEO_STATUS_FAILED,
    VIDEO_STATUS_PENDING,
    VIDEO_TIMEOUT_MS,
)
from flow_mcp.js_templates import (
    DOWNLOAD_IMAGE_JS,
    LIST_PROJECTS_JS,
    check_session_js,
    check_video_status_js,
    create_project_js,
    generate_images_js,
    generate_video_js,
    get_video_url_js,
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

# ── Errors ────────────────────────────────────────────────────────────────


class GenerationError(RuntimeError):
    """Base for generation failures."""


class ContentFilteredError(GenerationError):
    """Prompt was silently blocked by Google's content filter."""


class AuthError(GenerationError):
    """Bearer token or session expired."""


class RateLimitedError(GenerationError):
    """HTTP 429 — too many requests."""


class CreditExhaustedError(GenerationError):
    """Account has run out of credits — switch to next account."""


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


async def _find_existing_project(page: Page) -> str | None:
    """Try to find a reusable Flow project ID from ``project.listProjects``.

    Returns the first project ID found, or ``None``.
    """
    try:
        projects = await page.evaluate(LIST_PROJECTS_JS)
        if isinstance(projects, list) and len(projects) > 0:
            pid = projects[0].get("projectId")
            if pid:
                log.info("project.reused", pid=pid)
                return pid
    except Exception:
        pass
    return None


async def _create_project(page: Page, timestamp: str) -> str:
    """Create a new Flow project via tRPC.

    Returns the project ID.
    """
    js = create_project_js(timestamp)
    await page.add_script_tag(content=js)
    # Use polling for project creation too
    st = await _await_window_var(page, "__st", poll_ms=1000, timeout_ms=10_000)
    if not st or not st.get("pid"):
        raise GenerationError(f"Project creation failed: {st}")
    pid = st["pid"]
    log.info("project.created", pid=pid)
    return pid


async def _ensure_project(page: Page, timestamp: str) -> str:
    """Get or create a Flow project for generation.

    If ``FLOW_REUSE_PROJECTS=1`` (default), tries to reuse an existing
    project to avoid accumulating garbage projects in the Flow UI.
    """
    if _REUSE_PROJECTS:
        pid = await _find_existing_project(page)
        if pid:
            return pid
    return await _create_project(page, timestamp)


# ── Reference image upload (with polling) ─────────────────────────────────


async def _upload_reference_image(
    page: Page,
    bearer: str,
    pid: str,
    ref_path: Path,
) -> list[dict[str, Any]]:
    """Upload a reference image via ``POST /v1/flow/uploadImage``.

    Returns the ``imageInputs`` list to include in the generation request.
    """
    _prog_local(4, 8, f"Uploading reference image ({ref_path.name})...")
    try:
        img_bytes = ref_path.read_bytes()
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        upload_body = json.dumps({
            "clientContext": {"projectId": pid, "tool": "PINHOLE"},
            "imageBytes": img_b64,
        })

        js = upload_image_js(bearer, json.dumps(upload_body))
        await page.add_script_tag(content=js)

        # Poll for upload result instead of fixed sleep
        up = await _await_window_var(
            page, "__up",
            poll_ms=UPLOAD_POLL_INTERVAL_MS,
            timeout_ms=UPLOAD_TIMEOUT_MS,
        )
        if not up or up.get("error"):
            raise GenerationError(
                f"Failed to upload reference image: {up}"
            )
        asset_name = (up.get("media") or up).get("name")
        if not asset_name:
            raise GenerationError(
                f"Upload response missing 'name': {json.dumps(up)[:300]}"
            )
        log.info("i2i.reference_uploaded", name=asset_name, path=str(ref_path))
        return [
            {
                "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                "name": asset_name,
            }
        ]
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError(f"Failed to process reference image: {exc}") from exc


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
            # 403 can mean: forbidden access OR credits exhausted
            # Check the response body for credit-related messages
            body = raw[len("HTTP_403:"):]
            if any(kw in body.lower() for kw in ("credit", "quota", "billing", "trial", "subscription")):
                raise CreditExhaustedError(
                    f"Account credits exhausted (HTTP 403): {body[:200]}"
                )
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
    """Generate images via Google Flow's ``batchGenerateImages`` API.

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
    wire_model = MODELS.get(model.lower(), "NARWHAL")
    wire_aspect = ASPECT_RATIOS.get(aspect, "IMAGE_ASPECT_RATIO_PORTRAIT")
    timestamp = str(int(time.time() * 1000))
    do_upscale = resolution.lower() in ("2k", "4k")
    # Only used when do_upscale is True. For "1k" (default), this is never
    # read — we download the original. Keep a default so the dict access
    # is safe even if resolution is unexpected.
    wire_resolution = {
        "1k": "UPSAMPLE_IMAGE_RESOLUTION_2K",  # unused for 1k, but defined
        "2k": "UPSAMPLE_IMAGE_RESOLUTION_2K",
        "4k": "UPSAMPLE_IMAGE_RESOLUTION_4K",
    }.get(resolution.lower(), "UPSAMPLE_IMAGE_RESOLUTION_2K")

    (page, ctx) = await acquire_page()

    def _prog(n: int, t: int, msg: str) -> None:
        if _progress_cb:
            _progress_cb(n, t, msg)

    try:
        _prog(1, 7, "Authenticating with Google Flow...")
        try:
            bearer = await capture_bearer_token(page)
        except RuntimeError:
            await page.goto(
                "https://labs.google/fx/tools/flow",
                wait_until="networkidle",
                timeout=_NAV_TIMEOUT,
            )
            bearer = await capture_bearer_token(page)

        # Mint reCAPTCHA token
        _prog(2, 7, "Minting reCAPTCHA token...")
        try:
            recaptcha_token = await TokenMinter(page).mint("IMAGE_GENERATION")
            log.info("recaptcha.minted")
        except Exception as exc:
            raise GenerationError(f"reCAPTCHA minting failed: {exc}") from exc

        # Create or reuse project
        _prog(3, 7, "Setting up Flow project...")
        pid = await _ensure_project(page, timestamp)

        # Upload reference image (I2I) if provided
        image_inputs: list[dict[str, Any]] = []
        if reference_image:
            ref_path = Path(reference_image)
            if not ref_path.exists():
                raise GenerationError(f"Reference image not found: {reference_image}")

            _prog(4, 7, f"Uploading reference image ({ref_path.name})...")
            image_inputs = await _upload_reference_image(page, bearer, pid, ref_path)

        # Call batchGenerateImages (with retry on 401/429/5xx)
        _prog(5, 7, "Requesting image generation...")
        session_id = ";" + timestamp
        client_context: dict[str, Any] = {
            "tool": "PINHOLE",
            "projectId": pid,
            "sessionId": session_id,
            "recaptchaContext": {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
        }
        request_body = {
            "clientContext": client_context,
            "mediaGenerationContext": {"batchId": f"g_{timestamp}"},
            "useNewMedia": True,
            "requests": [
                {
                    "clientContext": client_context,
                    "imageModelName": wire_model,
                    "imageAspectRatio": wire_aspect,
                    "structuredPrompt": {"parts": [{"text": prompt}]},
                    "seed": random.randint(1, 2_147_483_647),
                    "imageInputs": image_inputs,
                }
            ],
        }
        api_url = (
            "https://aisandbox-pa.googleapis.com/v1/projects"
            f"/{pid}/flowMedia:batchGenerateImages"
        )
        body_json = json.dumps(json.dumps(request_body))

        raw, bearer = await _call_api_with_retry(page, api_url, bearer, body_json)
        if raw is None:
            raise ContentFilteredError(
                "API returned None — prompt may have been blocked by "
                "content filter, or bearer token expired."
            )

        # Parse fifeUrls and media UUIDs
        _prog(6, 7, "Processing API response...")
        urls = re.findall(r'"fifeUrl"\s*:\s*"([^"]+)"', raw)
        if not urls:
            raise GenerationError(
                f"No fifeUrl in API response. Raw: {raw[:500]}"
            )

        media_ids: list[str] = re.findall(
            r'"mediaId"\s*:\s*"([0-9a-fA-F-]{36})"', raw
        )
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
                    up_b64 = await _upscale_image_with_retry(
                        page, bearer, pid, media_ids[idx],
                        wire_resolution, recaptcha_token,
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
            raise GenerationError(
                f"Downloaded 0 images. Raw: {raw[:500]}"
            )

        return GenerationResult(files=saved)

    finally:
        await release_context(ctx)


async def generate_images_with_fallback(
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
    """Wrap ``generate_images`` with automatic account fallback.

    If one account runs out of credits (``CreditExhaustedError``), switches
    to the next configured account and retries. Continues until an account
    succeeds or all are exhausted.

    The bounce pool is closed between account switches so the next
    account's browser profile is loaded fresh.
    """
    from flow_mcp.browser_pool import close_pool

    mgr = AccountManager.get_instance()
    attempted_accounts: list[str] = []
    max_attempts = mgr.account_count + 1  # +1 so we try each once, then raise

    for attempt in range(max_attempts):
        account = mgr.active_name
        if account:
            attempted_accounts.append(account)

        log.info(
            "generate_images.attempt",
            account=account,
            attempt=attempt + 1,
            total=mgr.account_count,
        )

        try:
            return await generate_images(
                prompt=prompt,
                model=model,
                count=count,
                aspect=aspect,
                output_dir=output_dir,
                _progress_cb=_progress_cb,
                reference_image=reference_image,
                resolution=resolution,
            )
        except CreditExhaustedError as exc:
            log.warning(
                "account.credits_exhausted",
                account=account,
                error=str(exc),
            )
            # Close the pool so the next account gets a fresh browser
            await close_pool()

            try:
                next_account = mgr.switch_to_next()
            except AccountCycleError:
                # No more accounts to try — fall through to the
                # GenerationError below with the list of tried accounts.
                raise GenerationError(
                    f"All {len(attempted_accounts)} account(s) exhausted "
                    f"({', '.join(attempted_accounts)}). "
                    "Add more accounts via `flow-mcp auth login` or "
                    "set GFLOW_ACCOUNTS=name1,name2,name3."
                ) from exc

            if next_account is None:
                # Empty account list — should be rare (env gave no accounts
                # AND no authenticated profiles found).
                raise GenerationError(
                    f"No Flow accounts available. "
                    "Run `flow-mcp auth login` to add one."
                ) from exc

            log.info(
                "account.switching",
                from_account=account,
                to_account=next_account,
            )
            # Continue the for loop — try the next account.
            continue

    raise GenerationError(
        f"Tried {len(attempted_accounts)} account(s) "
        f"({', '.join(attempted_accounts)}) without success."
    )


# ═════════════════════════════════════════════════════════════════════════
#  Video generation (async API with polling)
# ═════════════════════════════════════════════════════════════════════════


class VideoResult:
    """Result of a successful video generation.

    NOTE: As of Jul 2026, Google Flow does NOT expose a programmatic
    download URL for videos via the public tRPC API — the
    ``media.getMediaUrlRedirect`` endpoint that works for images
    returns 400 for video media items. The video bytes are stored in
    Google's CDN but the signed URL can only be generated by the Flow
    web UI, not by API calls from the browser session.

    So instead of returning a local MP4 path, we return:
    - ``media_name``: the UUID of the generated video
    - ``project_url``: the Flow web URL where the user can view & download

    The user opens the project URL in their browser and clicks the
    download button manually. (This matches the behavior of Flow's own
    chat UI when generating videos.)
    """

    def __init__(
        self,
        media_name: str,
        project_id: str,
        model: str,
        duration_s: int,
        media_blob_size: int | None = None,
    ) -> None:
        self.media_name = media_name
        self.project_id = project_id
        self.model = model
        self.duration_s = duration_s
        self.media_blob_size = media_blob_size
        self.files: list[Path] = []  # always empty for video

    @property
    def paths(self) -> list[str]:
        return []  # No local file — see class docstring

    @property
    def project_url(self) -> str:
        return f"https://labs.google/fx/tools/flow/project/{self.project_id}"

    def describe(self) -> str:
        lines = [
            f"✅ Video generation complete [{self.model}, {self.duration_s}s]",
            f"  🎬 Media ID:  {self.media_name}",
            f"  📁 Project ID: {self.project_id}",
        ]
        if self.media_blob_size:
            lines.append(
                f"  📊 Size:       {self.media_blob_size / 1024 / 1024:.1f} MB"
            )
        lines += [
            "",
            "  ⚠️  flow-mcp cannot download the MP4 directly. Google Flow's",
            "      public tRPC API does not expose a video download URL —",
            "      only thumbnails/images work via the API.",
            "",
            "  To download your video, open the project in Flow:",
            f"      {self.project_url}",
            "",
            "  Or view it in your Flow library:",
            "      https://labs.google/fx/tools/flow",
        ]
        return "\n".join(lines)


async def _await_window_var_ms(
    page: Page,
    var_name: str,
    *,
    poll_ms: int = VIDEO_POLL_INTERVAL_MS,
    timeout_ms: int = VIDEO_TIMEOUT_MS,
) -> Any:
    """Poll ``window[var_name]`` until it is not ``None`` (ms-based)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        val = await page.evaluate(f"window[{json.dumps(var_name)}]")
        if val is not None:
            return val
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_ms / 1000)


async def _upload_video_reference_image(
    page: Page,
    bearer: str,
    pid: str,
    ref_path: Path,
) -> list[dict[str, Any]]:
    """Upload a reference image for I2V (image-to-video) generation.

    Same endpoint as for images — returns imageInputs format the video
    API also accepts (with ``IMAGE_INPUT_TYPE_REFERENCE``).
    """
    try:
        img_bytes = ref_path.read_bytes()
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        upload_body = json.dumps({
            "clientContext": {"projectId": pid, "tool": "PINHOLE"},
            "imageBytes": img_b64,
        })
        js = upload_image_js(bearer, json.dumps(upload_body))
        await page.add_script_tag(content=js)
        up = await _await_window_var(
            page, "__up",
            poll_ms=UPLOAD_POLL_INTERVAL_MS,
            timeout_ms=UPLOAD_TIMEOUT_MS,
        )
        if not up or up.get("error"):
            raise GenerationError(f"Failed to upload reference image: {up}")
        asset_name = (up.get("media") or up).get("name")
        if not asset_name:
            raise GenerationError(
                f"Upload response missing 'name': {json.dumps(up)[:300]}"
            )
        log.info("video.reference_uploaded", name=asset_name, path=str(ref_path))
        return [
            {
                "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                "name": asset_name,
            }
        ]
    except GenerationError:
        raise
    except Exception as exc:
        raise GenerationError(f"Failed to process reference image: {exc}") from exc


async def _check_video_status(
    page: Page,
    bearer: str,
    project_id: str,
    media_name: str,
) -> dict:
    """Poll batchCheckAsyncVideoGenerationStatus once.

    Returns the parsed media item dict. Raises GenerationError on HTTP
    failure.
    """
    body = json.dumps(json.dumps({"media": [{"name": media_name, "projectId": project_id}]}))
    js = check_video_status_js(
        "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus",
        bearer, body,
    )
    await page.add_script_tag(content=js)
    raw = await _await_window_var_ms(page, "__vid_status", poll_ms=500, timeout_ms=15_000)
    if raw is None:
        raise GenerationError("Video status check timed out")
    if isinstance(raw, str):
        if raw.startswith("HTTP_401:"):
            raise AuthError("Video status: 401 — token expired")
        if raw.startswith("HTTP_403:"):
            body_txt = raw[len("HTTP_403:"):]
            if any(kw in body_txt.lower() for kw in ("credit", "quota", "billing")):
                raise CreditExhaustedError(f"Video: {body_txt[:200]}")
            raise GenerationError(f"Video status: 403 — {body_txt[:200]}")
        if raw.startswith("HTTP_429:"):
            raise RateLimitedError("Video status: 429")
        if raw.startswith("HTTP_"):
            raise GenerationError(f"Video status: {raw[:200]}")
        if raw.startswith("ERR:"):
            raise GenerationError(f"Video status fetch error: {raw[:200]}")
        # Unexpected: try to parse as JSON anyway
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise GenerationError(f"Video status returned non-JSON: {raw[:200]}")
    else:
        data = raw

    media_list = data.get("media") or []
    if not media_list:
        raise GenerationError(f"Video status returned no media items: {json.dumps(data)[:300]}")
    return media_list[0]


async def _wait_for_video_completion(
    page: Page,
    bearer: str,
    project_id: str,
    media_name: str,
    *,
    poll_ms: int = VIDEO_POLL_INTERVAL_MS,
    timeout_ms: int = VIDEO_TIMEOUT_MS,
) -> dict:
    """Poll status until SUCCESSFUL or FAILED, or timeout.

    Returns the final media item dict. Raises GenerationError on failure
    or timeout.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last_status = "UNKNOWN"
    while time.monotonic() < deadline:
        try:
            media = await _check_video_status(page, bearer, project_id, media_name)
        except AuthError:
            # Token expired mid-poll — refresh once and retry
            log.info("video.status_token_expired_refreshing")
            bearer = await _refresh_token(page)
            continue
        last_status = (
            media.get("mediaMetadata", {}).get("mediaStatus", {}).get("mediaGenerationStatus", "?")
        )
        if last_status == VIDEO_STATUS_DONE:
            return media
        if last_status == VIDEO_STATUS_FAILED:
            err = media.get("mediaMetadata", {}).get("mediaStatus", {}).get("error", {})
            raise GenerationError(
                f"Video generation FAILED: {err or json.dumps(media)[:300]}"
            )
        # PENDING / ACTIVE / unknown — keep polling
        log.debug("video.status_poll", status=last_status)
        await asyncio.sleep(poll_ms / 1000)
    raise GenerationError(
        f"Video generation timed out after {timeout_ms/1000:.0f}s "
        f"(last status: {last_status})"
    )


async def _resolve_video_url(page: Page, media_name: str) -> str:
    """Call ``media.getMediaUrlRedirect`` to get a download URL for the MP4.

    As of Jul 2026, this endpoint returns HTTP 400 for video media items
    (it works only for image thumbnails). The function is kept here as a
    reference / fallback — the main flow no longer calls it because the
    signed download URL for videos is not available via the public tRPC
    API. Kept for debugging and in case Google exposes a working
    endpoint later.
    """
    js = get_video_url_js(media_name)
    await page.add_script_tag(content=js)
    raw = await _await_window_var_ms(page, "__vid_url", poll_ms=500, timeout_ms=15_000)
    if raw is None:
        raise GenerationError("Video URL fetch timed out")
    if isinstance(raw, str):
        if raw.startswith("HTTP_"):
            raise GenerationError(f"Video URL fetch returned {raw}")
        if raw.startswith("ERR:"):
            raise GenerationError(f"Video URL fetch error: {raw}")
        return raw
    raise GenerationError(f"Video URL fetch returned unexpected: {raw}")


def _resolve_video_model_key(model: str, duration: int, is_i2v: bool) -> str:
    """Resolve the wire videoModelKey based on parameters.

    Flow's backend names keys explicitly. We translate our clean aliases:
    - omni-flash (abra):
      - T2V: ``abra_t2v_<duration>s``
      - I2V: ``abra_i2v_<duration>s``
    - veo-lite:
      - T2V: ``veo_3_1_t2v_lite_<duration>s``
      - I2V: ``veo_3_1_i2v_s_lite_<duration>s``
    - veo-fast:
      - T2V: ``veo_3_1_t2v_fast_<duration>s``
      - I2V: ``veo_3_1_i2v_s_fast_<duration>s``
    - veo-quality:
      - T2V: ``veo_3_1_t2v_quality_<duration>s``
      - I2V: ``veo_3_1_i2v_s_quality_<duration>s``
    """
    base = VIDEO_MODELS[model]
    
    # 10s only supported by omni-flash (abra)
    if duration == 10 and model != "omni-flash":
        raise GenerationError("10s duration is only supported by the 'omni-flash' model")

    # Mapping logic
    if model == "omni-flash":
        prefix = "abra_i2v" if is_i2v else "abra_t2v"
        return f"{prefix}_{duration}s"
        
    # Veo 3.1 family
    sub = "i2v_s" if is_i2v else "t2v"
    tier = {
        "veo-lite": "lite",
        "veo-fast": "fast",
        "veo-quality": "quality",
    }[model]
    
    # For Veo 3.1, 4s and 6s have explicit suffixes. Default fallback is 4s.
    dur_str = f"_{duration}s" if duration in (4, 6) else "_4s"
    return f"{base}_{sub}_{tier}{dur_str}"


async def generate_video(
    prompt: str,
    *,
    model: str = "omni-flash",
    aspect: str = "9:16",
    duration: int = 4,
    output_dir: str | Path | None = None,
    _progress_cb: Callable[[int, int, str], None] | None = None,
    reference_image: str | None = None,
) -> VideoResult:
    """Generate a video via Google Flow's async video API.

    Parameters
    ----------
    prompt:
        Text description of the video to generate.
    model:
        Model alias — see ``VIDEO_MODELS``. Default ``"omni-flash"`` is
        the cheapest (maps to ``abra``).
    aspect:
        ``9:16`` (portrait, default), ``16:9`` (landscape), ``1:1`` (square).
    duration:
        4, 6, 8, or 10 seconds. Longer = more credits. 10s only works for
        ``omni-flash``.
    output_dir:
        Where to save the metadata (kept for API parity).
    reference_image:
        Optional path to a local image to use as image-to-video reference.
    """
    if model not in VIDEO_MODELS:
        raise GenerationError(f"Unknown video model '{model}'. Valid: {', '.join(VIDEO_MODELS)}")
    if aspect not in VIDEO_ASPECT_RATIOS:
        raise GenerationError(f"Unknown video aspect '{aspect}'. Valid: {', '.join(VIDEO_ASPECT_RATIOS)}")
    if duration not in VIDEO_DURATIONS:
        raise GenerationError(f"Invalid duration {duration}s. Valid: {VIDEO_DURATIONS}")
    
    is_i2v = reference_image is not None
    wire_model = _resolve_video_model_key(model, duration, is_i2v)
    wire_aspect = VIDEO_ASPECT_RATIOS[aspect]
    timestamp = str(int(time.time() * 1000))
    seed = random.randint(1, 2_147_483_647)

    (page, ctx) = await acquire_page()

    def _prog(n: int, t: int, msg: str) -> None:
        if _progress_cb:
            _progress_cb(n, t, msg)

    try:
        _prog(1, 7, "Authenticating with Google Flow...")
        try:
            bearer = await capture_bearer_token(page)
        except RuntimeError:
            await page.goto(
                "https://labs.google/fx/tools/flow",
                wait_until="networkidle",
                timeout=_NAV_TIMEOUT,
            )
            bearer = await capture_bearer_token(page)

        _prog(2, 7, "Minting reCAPTCHA token...")
        # Video uses a different reCAPTCHA action than image generation.
        # Using the wrong one returns "reCAPTCHA evaluation failed" 403.
        try:
            recaptcha_token = await TokenMinter(page).mint("VIDEO_GENERATION")
            log.info("video.recaptcha.minted", action="VIDEO_GENERATION")
        except Exception as exc:
            raise GenerationError(f"reCAPTCHA minting failed: {exc}") from exc

        # Create or reuse project
        _prog(3, 7, "Setting up Flow project...")
        pid = await _ensure_project(page, timestamp)

        # Upload reference image (I2V) if provided
        image_inputs: list[dict[str, Any]] = []
        if reference_image:
            ref_path = Path(reference_image)
            if not ref_path.exists():
                raise GenerationError(f"Reference image not found: {reference_image}")
            _prog(4, 7, f"Uploading reference image ({ref_path.name})...")
            image_inputs = await _upload_video_reference_image(page, bearer, pid, ref_path)

        # Build the request
        session_id = ";" + timestamp
        client_context: dict[str, Any] = {
            "projectId": pid,
            "tool": "PINHOLE",
            "userPaygateTier": "PAYGATE_TIER_ZERO",
            "sessionId": session_id,
            "recaptchaContext": {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
        }
        # Single video request in the batch
        video_request: dict[str, Any] = {
            "aspectRatio": wire_aspect,
            "videoModelKey": wire_model,
            "seed": seed,
            "metadata": {},
        }
        if image_inputs:
            video_request["imageInputs"] = image_inputs
        else:
            video_request["textInput"] = {
                "structuredPrompt": {"parts": [{"text": prompt}]},
            }
        request_body = {
            "mediaGenerationContext": {
                "batchId": f"g_{timestamp}",
                "audioFailurePreference": VIDEO_AUDIO_FAILURE_PREFERENCE,
            },
            "clientContext": client_context,
            "requests": [video_request],
            "useV2ModelConfig": True,
        }
        api_url = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText"
        body_json = json.dumps(json.dumps(request_body))

        # Kick off generation
        _prog(5, 7, f"Requesting video generation ({wire_model}, {duration}s)...")
        js = generate_video_js(api_url, bearer, body_json)
        await page.add_script_tag(content=js)
        raw = await _await_window_var_ms(page, "__vid", poll_ms=500, timeout_ms=30_000)
        if raw is None:
            raise GenerationError("Video generate request timed out")
        if isinstance(raw, str):
            if raw.startswith("HTTP_401:"):
                raise AuthError("Video generate: 401 — session expired")
            if raw.startswith("HTTP_403:"):
                body_txt = raw[len("HTTP_403:"):]
                if any(kw in body_txt.lower() for kw in ("credit", "quota", "billing")):
                    raise CreditExhaustedError(f"Video: {body_txt[:200]}")
                raise GenerationError(f"Video generate: 403 — {body_txt[:200]}")
            if raw.startswith("HTTP_429:"):
                raise RateLimitedError("Video generate: 429")
            if raw.startswith("HTTP_5XX:"):
                raise GenerationError(f"Video generate: {raw[:200]}")
            if raw.startswith("ERR:"):
                raise GenerationError(f"Video generate fetch error: {raw[:200]}")
            # Unexpected — try parse
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise GenerationError(f"Video generate returned non-JSON: {raw[:200]}")
        else:
            data = raw

        media_list = data.get("media") or []
        if not media_list:
            raise GenerationError(
                f"Video generate returned no media: {json.dumps(data)[:500]}"
            )
        media_item = media_list[0]
        media_name = media_item.get("name")
        if not media_name:
            raise GenerationError(f"Media item missing 'name': {json.dumps(media_item)[:300]}")
        project_id = media_item.get("projectId") or pid
        log.info("video.queued", name=media_name, project=project_id, model=wire_model)

        # Poll until done
        _prog(6, 7, "Rendering video (this can take 30-90s)...")
        done_media = await _wait_for_video_completion(
            page, bearer, project_id, media_name,
        )
        log.info("video.rendered", name=media_name)

        # Download
        _prog(7, 7, "Finalising video result...")
        # As of Jul 2026, Flow's public tRPC API does not expose a
        # downloadable URL for video media (only images). The video is
        # rendered and stored in Google's CDN, but the signed URL can
        # only be obtained by the Flow web UI — not by API calls from
        # the current browser session. We confirm the render was
        # SUCCESSFUL via the status check above (which returned a
        # `mediaBlobSize`) and return the mediaName + project URL so the
        # user can open the project in Flow and download manually.

        media_blob_size = None
        try:
            blob = (
                done_media.get("mediaMetadata", {}).get("mediaBlobSize")
            )
            if blob is not None:
                media_blob_size = int(blob)
        except (ValueError, TypeError):
            pass

        log.info(
            "video.queued_done",
            name=media_name,
            project=project_id,
            size=media_blob_size,
        )

        return VideoResult(
            media_name=media_name,
            project_id=project_id,
            model=model,
            duration_s=duration,
            media_blob_size=media_blob_size,
        )

    finally:
        await release_context(ctx)


async def generate_video_with_fallback(
    prompt: str,
    *,
    model: str = "omni-flash",
    aspect: str = "9:16",
    duration: int = 4,
    output_dir: str | Path | None = None,
    _progress_cb: Callable[[int, int, str], None] | None = None,
    reference_image: str | None = None,
) -> VideoResult:
    """Wrap ``generate_video`` with automatic account fallback.

    Mirrors ``generate_images_with_fallback`` exactly: on
    ``CreditExhaustedError``, close the browser pool, switch to the next
    account in ``AccountManager``, and retry. If every account is
    exhausted, raise a ``GenerationError`` with the list of tried accounts.
    """
    from flow_mcp.browser_pool import close_pool

    mgr = AccountManager.get_instance()
    attempted_accounts: list[str] = []
    max_attempts = mgr.account_count + 1

    for attempt in range(max_attempts):
        account = mgr.active_name
        if account:
            attempted_accounts.append(account)

        log.info(
            "generate_video.attempt",
            account=account,
            attempt=attempt + 1,
            total=mgr.account_count,
        )

        try:
            return await generate_video(
                prompt=prompt,
                model=model,
                aspect=aspect,
                duration=duration,
                output_dir=output_dir,
                _progress_cb=_progress_cb,
                reference_image=reference_image,
            )
        except CreditExhaustedError as exc:
            log.warning(
                "account.credits_exhausted",
                account=account,
                video=True,
                error=str(exc),
            )
            await close_pool()

            try:
                next_account = mgr.switch_to_next()
            except AccountCycleError:
                raise GenerationError(
                    f"All {len(attempted_accounts)} account(s) exhausted "
                    f"({', '.join(attempted_accounts)}). "
                    "Add more accounts via `flow-mcp auth login` or "
                    "set GFLOW_ACCOUNTS=name1,name2,name3."
                ) from exc

            if next_account is None:
                raise GenerationError(
                    "No Flow accounts available. "
                    "Run `flow-mcp auth login` to add one."
                ) from exc

            log.info(
                "account.switching",
                from_account=account,
                to_account=next_account,
            )
            continue

    raise GenerationError(
        f"Tried {len(attempted_accounts)} account(s) "
        f"({', '.join(attempted_accounts)}) without success."
    )
