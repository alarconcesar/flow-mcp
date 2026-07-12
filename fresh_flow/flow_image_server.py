#!/usr/bin/env python3
"""
flow_image_server.py — MCP server for Google Flow image generation.

Exposes a `generate_image` tool to Claude Code that calls the
batchGenerateImages API directly from a Playwright browser context,
bypassing the Flow Agent chat quota (~10/day).

Usage (via Claude Code MCP config):
    python flow_image_server.py

Protocol: stdio-based MCP (FastMCP).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import structlog
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Imports from gflow-cli
# ---------------------------------------------------------------------------
sys.path.insert(
    0,
    str(Path.home() / "AppData/Roaming/uv/tools/gflow-cli/lib/site-packages"),
)
from gflow_cli.api.recaptcha import TokenMinter

# ---------------------------------------------------------------------------
# Logging: structlog → stderr (never stdout, which is MCP transport)
# ---------------------------------------------------------------------------
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    cache_logger_on_first_use=True,
)
log = structlog.get_logger("flow-mcp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PROFILE_NAME = os.environ.get("GFLOW_PROFILE", "cesaralarcon080405")
_HOME = Path(os.environ.get("GFLOW_CLI_HOME", str(Path.home() / "AppData/Local/ffroliva/gflow-cli")))
_PROFILE_DIR = _HOME / f"profile_{_PROFILE_NAME}"
_OUTPUT_DIR = Path(os.environ.get("GFLOW_OUTPUT_DIR", tempfile.gettempdir()))

ASPECT_MAP = {
    "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
    "4:3": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
    "3:4": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
}
MODEL_MAP = {
    "nano2": "NARWHAL",
    "nano-pro": "GEM_PIX_2",
    "narwhal": "NARWHAL",
    "gem_pix_2": "GEM_PIX_2",
}

# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------
server = FastMCP(
    name="flow-image-server",
    instructions="""Generate images via Google Flow's batchGenerateImages API.

Uses a persistent Playwright browser session with your saved gflow-cli authentication
to call the Flow API directly — no CLI quota consumption.

Requirements:
  - Xvfb running on :99 (Linux headless) or a visible display (Windows/macOS)
  - gflow-cli profile with valid auth session
  - Playwright browsers installed
""",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_xvfb() -> None:
    """Start Xvfb on :99 if not running and we're on Linux."""
    if sys.platform != "linux":
        return
    import subprocess

    ret = subprocess.run(
        ["pgrep", "-a", "Xvfb"], capture_output=True, text=True, timeout=5
    )
    if "Xvfb" not in ret.stdout:
        subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1280x720x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("xvfb.started", display=":99")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Core generation logic (ported from generate.py)
# ---------------------------------------------------------------------------

async def _generate_images(
    prompt: str,
    model: str = "nano-pro",
    count: int = 1,
    aspect: str = "9:16",
) -> dict:
    """Generate images via Google Flow batchGenerateImages API.

    Returns a dict with either ``{"success": true, "files": [...], "paths": [...]}``
    or ``{"success": false, "error": "..." }``.
    """
    wire_model = MODEL_MAP.get(model.lower(), "NARWHAL")
    wire_aspect = ASPECT_MAP.get(aspect, "IMAGE_ASPECT_RATIO_PORTRAIT")
    timestamp = str(int(time.time() * 1000))
    saved_files: list[str] = []

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(_PROFILE_DIR),
                headless=False,
                args=[
                    "--no-sandbox",
                    "--password-store=basic",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
                viewport={"width": 1280, "height": 720},
            )
        except Exception as exc:
            log.error("browser.launch_failed", error=str(exc))
            return {"success": False, "error": f"Browser launch failed: {exc}"}

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        bearer: str | None = None

        def _on_request(req):
            nonlocal bearer
            if bearer:
                return
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer ya29"):
                bearer = auth[7:]

        page.on("request", _on_request)

        # Navigate to Flow to capture the Bearer token
        try:
            await page.goto(
                "https://labs.google/fx/tools/flow",
                wait_until="networkidle",
                timeout=60000,
            )
        except Exception as exc:
            log.warning("navigation.timeout", error=str(exc))

        await page.wait_for_timeout(8000)
        if not bearer:
            await page.wait_for_timeout(5000)
        if not bearer:
            await ctx.close()
            return {
                "success": False,
                "error": "Failed to capture Bearer token — auth session may be expired. "
                "Run `gflow auth login --browser internal` to refresh.",
            }

        log.info("auth.token_captured")

        # Mint reCAPTCHA token
        try:
            recaptcha_token = await TokenMinter(page).mint("IMAGE_GENERATION")
            log.info("recaptcha.minted")
        except Exception as exc:
            await ctx.close()
            return {
                "success": False,
                "error": f"reCAPTCHA minting failed: {exc}",
            }

        # Create project via tRPC
        project_js = f"""(async() => {{
            try {{
                const r = await fetch('https://labs.google/fx/api/trpc/project.createProject', {{
                    method: 'POST',
                    headers: {{'content-type': 'application/json'}},
                    body: JSON.stringify({{
                        "json": {{
                            "projectTitle": "g_{timestamp}",
                            "toolName": "TOOL_NAME_UNSPECIFIED"
                        }}
                    }}),
                    credentials: 'include'
                }});
                const p = JSON.parse(await r.text());
                window.__st = {{pid: p.result.data.json.result.projectId}};
            }} catch(e) {{
                window.__st = {{error: e.toString()}};
            }}
        }})();"""
        await page.add_script_tag(content=project_js)
        await page.wait_for_timeout(5000)

        st = await page.evaluate("window.__st")
        if not st or not st.get("pid"):
            await ctx.close()
            return {
                "success": False,
                "error": f"Project creation failed: {st}",
            }
        pid = st["pid"]
        log.info("project.created", pid=pid)

        # Call batchGenerateImages
        session_id = ";" + timestamp
        client_context = {
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
                    "imageInputs": [],
                }
            ],
        }
        api_url = (
            f"https://aisandbox-pa.googleapis.com/v1/projects/{pid}/flowMedia:batchGenerateImages"
        )
        body_json = json.dumps(json.dumps(request_body))

        gen_js = f"""(async() => {{
            try {{
                const r = await fetch('{api_url}', {{
                    method: 'POST',
                    headers: {{
                        'Authorization': 'Bearer {bearer}',
                        'Content-Type': 'application/json;charset=UTF-8'
                    }},
                    body: {body_json}
                }});
                window.__gen = await r.text();
            }} catch(e) {{
                window.__gen = 'ERR:' + e;
            }}
        }})();"""
        await page.add_script_tag(content=gen_js)
        await page.wait_for_timeout(25000)

        raw = await page.evaluate("window.__gen")
        if raw is None:
            await ctx.close()
            return {
                "success": False,
                "error": "API call returned None — prompt may have been blocked by "
                "content filter, or bearer token expired.",
            }
        if isinstance(raw, str) and raw.startswith("ERR:"):
            await ctx.close()
            return {
                "success": False,
                "error": f"API call failed: {raw}",
            }

        # Parse fifeUrls and download
        urls = re.findall(r'"fifeUrl"\s*:\s*"([^"]+)"', raw)
        if not urls:
            await ctx.close()
            return {
                "success": False,
                "error": f"No fifeUrl found in API response. Raw: {raw[:500]}",
            }

        log.info("images.found", count=len(urls))

        for url in urls:
            url = url.replace("\\u0026", "&")
            try:
                dl = await page.evaluate(
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

            if dl and dl.startswith("data:"):
                img_data = dl.split(",", 1)[1]
                ext = dl.split(";")[0].split("/")[1] or "webp"
                out_path = _OUTPUT_DIR / f"flow-gen-{timestamp}-{len(saved_files)}.{ext}"
                with open(out_path, "wb") as f:
                    f.write(base64.b64decode(img_data))
                saved_files.append(str(out_path.resolve()))
                log.info("image.saved", path=str(out_path))
                if len(saved_files) >= count:
                    break

        await ctx.close()

        if not saved_files:
            return {
                "success": False,
                "error": f"Downloaded 0 images. Raw response: {raw[:500]}",
            }

        return {
            "success": True,
            "files": [Path(f).name for f in saved_files],
            "paths": saved_files,
        }


# ---------------------------------------------------------------------------
# MCP Tool: generate_image
# ---------------------------------------------------------------------------

@server.tool(
    name="generate_image",
    description="Generate images via Google Flow's batchGenerateImages API. "
    "Bypasses the Flow Agent chat quota (~10/day) by calling the API directly "
    "from a browser context with your saved authentication.",
)
async def generate_image(
    prompt: str,
    model: str = "nano-pro",
    count: int = 1,
    aspect: str = "9:16",
) -> str:
    """Generate one or more images using Google Flow.

    Args:
        prompt: Text description of the image to generate.
        model: Model to use. One of: nano2, nano-pro (default), narwhal, gem_pix_2.
        count: Number of images to generate (1-4, default 1).
        aspect: Aspect ratio. One of: 9:16 (default, portrait), 16:9 (landscape),
            1:1 (square), 4:3 (landscape), 3:4 (portrait).

    Returns:
        Human-readable result string with paths to generated images.
    """
    _ensure_xvfb()

    # Validate inputs
    model = model.lower()
    if model not in MODEL_MAP:
        valid = ", ".join(MODEL_MAP.keys())
        return json.dumps({"success": False, "error": f"Invalid model '{model}'. Valid: {valid}"})

    aspect = aspect.lower()
    if aspect not in ASPECT_MAP:
        valid = ", ".join(ASPECT_MAP.keys())
        return json.dumps({"success": False, "error": f"Invalid aspect '{aspect}'. Valid: {valid}"})

    count = max(1, min(4, count))

    log.info(
        "generate_image.called",
        prompt=prompt[:80],
        model=model,
        count=count,
        aspect=aspect,
    )

    result = await _generate_images(
        prompt=prompt,
        model=model,
        count=count,
        aspect=aspect,
    )

    log.info("generate_image.complete", success=result.get("success"))

    # Format readable output
    if result.get("success"):
        paths = result.get("paths", [])
        lines = [f"✅ Generated {len(paths)} image(s):"]
        for p in paths:
            size = os.path.getsize(p)
            lines.append(f"  📁 {p} ({size / 1024:.0f} KB)")
        lines.append("\nYou can view these files in your file manager or reference them by path.")
        return "\n".join(lines)
    else:
        return f"❌ Failed: {result.get('error', 'Unknown error')}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server over stdio transport."""
    log.info("server.starting", name="flow-image-server")

    from gflow_cli.mcp.server import _configure_utf8_pipes
    _configure_utf8_pipes()

    # FastMCP handles stdio transport internally — it writes JSON-RPC to
    # the real stdout FD. We do NOT redirect sys.stdout to stderr here
    # because FastMCP.run() wraps sys.stdout.buffer directly; redirecting
    # beforehand would make it write protocol messages to stderr instead,
    # which MCP clients never read.
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
