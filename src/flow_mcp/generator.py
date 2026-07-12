"""Core image generation logic — project creation, API call, download."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import structlog
from playwright.async_api import Page

from flow_mcp.recaptcha import TokenMinter

from flow_mcp.browser import capture_bearer_token
from flow_mcp.browser_pool import acquire_page, release_context
from flow_mcp.constants import (
    ASPECT_RATIOS,
    GEN_POLL_INTERVAL_MS,
    GEN_TIMEOUT_MS,
    MODELS,
    PROJECT_CREATE_WAIT_MS,
)
from flow_mcp.profile import resolve_profile

log = structlog.get_logger("flow-mcp")

# ── Errors ──────────────────────────────────────────────────────────────


class GenerationError(RuntimeError):
    """Base for generation failures."""


class ContentFilteredError(GenerationError):
    """Prompt was silently blocked by Google's content filter."""


class AuthError(GenerationError):
    """Bearer token or session expired."""


# ── Result type ─────────────────────────────────────────────────────────


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


# ── Helpers ─────────────────────────────────────────────────────────────


async def _await_window_var(
    page: Page,
    var_name: str,
    *,
    poll_ms: int = GEN_POLL_INTERVAL_MS,
    timeout_ms: int = GEN_TIMEOUT_MS,
) -> Any:
    """Poll `window[var_name]` until it is not ``None``.

    This replaces a fixed ``wait_for_timeout`` with a loop that checks
    every ``poll_ms`` and returns as soon as the value is available —
    typically saving 5–15s per generation.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        val = await page.evaluate(f"window[{json.dumps(var_name)}]")
        if val is not None:
            return val
        elapsed = time.monotonic() - (deadline - timeout_ms / 1000)
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_ms / 1000)


async def _refresh_token(page: Page) -> str:
    """Re-capture the Bearer token by navigating to Flow again.

    Called when the previous token expired (HTTP 401).
    """
    log.info("auth.refreshing")
    # Remove the old listener and capture fresh
    return await capture_bearer_token(page)


# ── Generator ───────────────────────────────────────────────────────────


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
        Number of images to generate (1–4).
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
    wire_resolution = {
        "2k": "UPSAMPLE_IMAGE_RESOLUTION_2K",
        "4k": "UPSAMPLE_IMAGE_RESOLUTION_4K",
    }.get(resolution.lower())

    (page, ctx) = await acquire_page()

    def _prog(n: int, t: int, msg: str) -> None:
        if _progress_cb:
            _progress_cb(n, t, msg)

    try:
        # Ensure we're on the Flow page and have a bearer token.
        _prog(1, 8, "Authenticating with Google Flow...")
        try:
            bearer = await capture_bearer_token(page)
        except RuntimeError:
            # Pooled context may need a fresh navigate
            await page.goto(
                "https://labs.google/fx/tools/flow",
                wait_until="networkidle",
                timeout=60_000,
            )
            bearer = await capture_bearer_token(page)

        # Mint reCAPTCHA token
        _prog(2, 8, "Minting reCAPTCHA token...")
        try:
            recaptcha_token = await TokenMinter(page).mint("IMAGE_GENERATION")
            log.info("recaptcha.minted")
        except Exception as exc:
            raise GenerationError(f"reCAPTCHA minting failed: {exc}") from exc

        # Create project via tRPC
        _prog(3, 8, "Creating Flow project...")
        project_js = f"""(async() => {{
            try {{
                const r = await fetch(
                    'https://labs.google/fx/api/trpc/project.createProject',
                    {{
                        method: 'POST',
                        headers: {{'content-type': 'application/json'}},
                        body: JSON.stringify({{
                            "json": {{
                                "projectTitle": "g_{timestamp}",
                                "toolName": "TOOL_NAME_UNSPECIFIED"
                            }}
                        }}),
                        credentials: 'include'
                    }}
                );
                const p = JSON.parse(await r.text());
                window.__st = {{pid: p.result.data.json.result.projectId}};
            }} catch(e) {{
                window.__st = {{error: e.toString()}};
            }}
        }})();"""
        await page.add_script_tag(content=project_js)
        await page.wait_for_timeout(PROJECT_CREATE_WAIT_MS)

        st = await page.evaluate("window.__st")
        if not st or not st.get("pid"):
            raise GenerationError(f"Project creation failed: {st}")
        pid = st["pid"]
        log.info("project.created", pid=pid)

        # ── Upload reference image (I2I) if provided ──────────────────────
        image_inputs: list[dict[str, Any]] = []
        if reference_image:
            ref_path = Path(reference_image)
            if not ref_path.exists():
                raise GenerationError(f"Reference image not found: {reference_image}")

            _prog(4, 8, f"Uploading reference image ({ref_path.name})...")
            try:
                img_bytes = ref_path.read_bytes()
                img_b64 = base64.b64encode(img_bytes).decode("ascii")
                upload_body = json.dumps({
                    "clientContext": {"projectId": pid, "tool": "PINHOLE"},
                    "imageBytes": img_b64,
                })
                upload_js = f"""(async() => {{
                    try {{
                        const r = await fetch(
                            'https://aisandbox-pa.googleapis.com/v1/flow/uploadImage',
                            {{
                                method: 'POST',
                                headers: {{
                                    'Authorization': 'Bearer {bearer}',
                                    'Content-Type': 'application/json;charset=UTF-8'
                                }},
                                body: {json.dumps(upload_body)}
                            }}
                        );
                        window.__up = await r.json();
                    }} catch(e) {{
                        window.__up = {{error: e.toString()}};
                    }}
                }})();"""
                await page.add_script_tag(content=upload_js)
                await page.wait_for_timeout(5_000)

                up = await page.evaluate("window.__up")
                if not up or up.get("error"):
                    raise GenerationError(
                        f"Failed to upload reference image: {up}"
                    )
                asset_name = (up.get("media") or up).get("name")
                if not asset_name:
                    raise GenerationError(
                        f"Upload response missing 'name': {json.dumps(up)[:300]}"
                    )
                image_inputs = [
                    {
                        "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                        "name": asset_name,
                    }
                ]
                log.info("i2i.reference_uploaded", name=asset_name, path=str(ref_path))
            except GenerationError:
                raise
            except Exception as exc:
                raise GenerationError(f"Failed to process reference image: {exc}") from exc

        # Call batchGenerateImages (with retry on 401)
        _prog(5, 8, "Requesting image generation...")
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
                    "seed": int(time.time()),
                    "imageInputs": image_inputs,
                }
            ],
        }
        api_url = (
            "https://aisandbox-pa.googleapis.com/v1/projects"
            f"/{pid}/flowMedia:batchGenerateImages"
        )
        body_json = json.dumps(json.dumps(request_body))

        # Attempt the API call, with retry if 401
        raw = await _call_api_with_retry(page, api_url, bearer, body_json)
        if raw is None:
            raise ContentFilteredError(
                "API returned None — prompt may have been blocked by "
                "content filter, or bearer token expired."
            )

        # Parse fifeUrls and media UUIDs
        _prog(6, 8, "Processing API response...")
        urls = re.findall(r'"fifeUrl"\s*:\s*"([^"]+)"', raw)
        if not urls:
            raise GenerationError(
                f"No fifeUrl in API response. Raw: {raw[:500]}"
            )

        # Extract media UUIDs for upscale
        media_ids: list[str] = re.findall(
            r'"mediaId"\s*:\s*"([0-9a-fA-F-]{36})"', raw
        )
        log.info("images.found", count=len(urls), media_ids=len(media_ids))

        # Download (and optionally upscale) each image
        _prog(7, 8, f"Downloading {min(len(urls), count)} image(s)...")
        out = Path(output_dir) if output_dir else Path(tempfile_dir())
        out.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        max_dl = min(len(urls), count)

        for idx, url in enumerate(urls):
            url = url.replace("\\u0026", "&")

            if do_upscale and idx < len(media_ids):
                # Upscale via API
                try:
                    up_b64 = await _upscale_image(
                        page, bearer, pid, media_ids[idx],
                        wire_resolution, recaptcha_token,
                    )
                except Exception as exc:
                    log.warning("upscale.failed", idx=idx, error=str(exc))
                    # Fallback: download original
                    up_b64 = None

                if up_b64:
                    filepath = out / f"flow-gen-{timestamp}-{idx}-{resolution}.{_guess_ext_from_mime(up_b64)}"
                    filepath.write_bytes(base64.b64decode(up_b64))
                    saved.append(filepath)
                    log.info("image.upscaled", path=str(filepath), idx=idx, resolution=resolution)
                    _prog(idx + 1, max_dl, f"Downloading upscaled image {idx + 1}...")
                    if len(saved) >= count:
                        break
                    continue

            # Download original
            try:
                b64_data = await page.evaluate(
                    """async (u) => {
                        const r = await fetch(u);
                        const b = await r.blob();
                        return await new Promise(r => {
                            const d = new FileReader();
                            d.onload = () => r(d.result);
                            d.readAsDataURL(b);
                        });
                    }""",
                    url,
                )
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

        _prog(8, 8, "Done!")
        return GenerationResult(files=saved)

    finally:
        await release_context(ctx)


async def _call_api_with_retry(
    page: Page,
    api_url: str,
    bearer: str,
    body_json: str,
    *,
    max_retries: int = 2,
) -> str | None:
    """Call ``batchGenerateImages`` and retry on 401.

    Returns the raw response text, or ``None``.
    """
    for attempt in range(max_retries + 1):
        gen_js = f"""(async() => {{
            try {{
                const r = await fetch({json.dumps(api_url)}, {{
                    method: 'POST',
                    headers: {{
                        'Authorization': 'Bearer {bearer}',
                        'Content-Type': 'application/json;charset=UTF-8'
                    }},
                    body: {body_json}
                }});
                if (r.status === 401) {{
                    window.__gen = 'HTTP_401:' + (await r.text());
                }} else {{
                    window.__gen = await r.text();
                }}
            }} catch(e) {{
                window.__gen = 'ERR:' + e;
            }}
        }})();"""
        await page.add_script_tag(content=gen_js)

        # Dynamic polling instead of fixed timeout
        raw = await _await_window_var(
            page,
            "__gen",
            poll_ms=GEN_POLL_INTERVAL_MS,
            timeout_ms=GEN_TIMEOUT_MS,
        )

        if raw is None:
            return None

        if isinstance(raw, str) and raw.startswith("HTTP_401:"):
            if attempt < max_retries:
                log.info("auth.401_retry", attempt=attempt + 1)
                bearer = await _refresh_token(page)
                continue
            raise AuthError(
                f"HTTP 401 persisted after {max_retries} retries — session expired. "
                "Run `flow-mcp auth login` to refresh."
            )

        if isinstance(raw, str) and raw.startswith("ERR:"):
            raise GenerationError(f"API fetch failed: {raw}")

        return raw

    return None


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
    import time as _time

    sid = ";" + str(int(_time.time() * 1000))
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

    js = f"""(async() => {{
        try {{
            const r = await fetch(
                'https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage',
                {{
                    method: 'POST',
                    headers: {{
                        'Authorization': 'Bearer {bearer}',
                        'Content-Type': 'application/json;charset=UTF-8'
                    }},
                    body: {json.dumps(body)}
                }}
            );
            if (r.ok) {{
                const data = await r.json();
                window.__up_img = data.encodedImage || null;
            }} else {{
                window.__up_img = 'HTTP_' + r.status;
            }}
        }} catch(e) {{
            window.__up_img = 'ERR:' + e;
        }}
    }})();"""
    await page.add_script_tag(content=js)
    await page.wait_for_timeout(15_000)

    result = await page.evaluate("window.__up_img")
    if not result or isinstance(result, str) and (result.startswith("HTTP_") or result.startswith("ERR:")):
        log.warning("upscale.api_failed", media_id=media_id, result=str(result)[:100])
        return None

    return result


def _guess_ext_from_mime(b64_data: str) -> str:
    """Guess file extension from base64 data URL or raw base64."""
    # If it's a data URL
    if b64_data.startswith("data:"):
        mime = b64_data.split(";")[0].split(":")[1] if ":" in b64_data else ""
        return _MIME_EXT.get(mime, "jpg")
    return "jpg"


_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def tempfile_dir() -> str:
    """Cross-platform temp directory path."""
    import tempfile as _tf

    return _tf.gettempdir()


def _guess_mime(path: Path) -> str | None:
    """Guess MIME type from file extension."""
    ext = path.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    return mime_map.get(ext)
