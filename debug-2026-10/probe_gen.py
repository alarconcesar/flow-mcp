"""E2E de generación por batchexecute: captcha → ogiZ0b → descarga.

Uso:  GFLOW_PROFILE=<perfil> python debug-2026-10/probe_gen.py "prompt" [aspecto] [modelo]

Quema un captcha y una generación real de la cuenta.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys

sys.path.insert(0, "/home/ubuntu/projects/flow-mcp/src")

from playwright.async_api import async_playwright  # noqa: E402

from flow_mcp import flow_batch as fb  # noqa: E402
from flow_mcp.browser import create_browser_context  # noqa: E402
from flow_mcp.js_templates import DOWNLOAD_IMAGE_JS  # noqa: E402
from flow_mcp.profile import resolve_profile  # noqa: E402
from flow_mcp.recaptcha import TokenMinter  # noqa: E402

OUT_DIR = "/home/ubuntu/projects/flow-mcp/debug-2026-10"
DEFAULT_PROMPT = "A single red circle centered on a plain white background, minimal flat design."


async def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
    aspect = sys.argv[2] if len(sys.argv) > 2 else "1:1"
    model = sys.argv[3] if len(sys.argv) > 3 else "nano-pro"

    profile = resolve_profile()
    print(f"perfil: {profile}")

    async with async_playwright() as pw:
        ctx = await create_browser_context(pw, profile, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await fb.wait_for_flow_ready(page)
        pid = await fb.ensure_project(page)
        print(f"proyecto: {pid}")
        state = await fb.open_project(page, pid)
        print(f"proyecto abierto — tokens={bool(state.get('at'))} grecaptcha={state.get('grecaptcha')}")

        minter = TokenMinter(page)
        site_key = await minter.site_key()
        print(f"site key: {site_key[:18]}…({len(site_key)})")
        token = await minter.mint(fb.CAPTCHA_IMAGE)
        print(f"captcha:  {token[:18]}…({len(token)})")

        freq = fb.image_request(prompt, pid, count=1, aspect=aspect, model=model)
        freq = fb.apply_captcha(freq, token)
        _, text = await fb.run_rpc(page, fb.RPC_GEN_IMAGE, freq, max_chars=2_000_000)
        print(f"respuesta: {len(text)} chars — {text[:70]!r}")
        print(f"envelopes: {[(r.rpcid, r.ok) for r in fb.parse_envelope(text)]}")

        payload = fb.first_payload(text, fb.RPC_GEN_IMAGE)
        images = fb.read_images(payload)
        print(f"imágenes: {len(images)}")
        for img in images:
            print(f"  - {img.media_id}  {img.url[:80]}…")

        for idx, img in enumerate(images[:1]):
            data = await page.evaluate(DOWNLOAD_IMAGE_JS, img.url)
            if not data or not str(data).startswith("data:"):
                print(f"download FALLÓ: {str(data)[:120]}")
                continue
            raw = data.split(",", 1)[1]
            ext = data.split(";")[0].split("/")[1] or "webp"
            path = os.path.join(OUT_DIR, f"test-gen-{idx}.{ext}")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(raw))
            print(f"GUARDADO: {path} ({os.path.getsize(path)} bytes)")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
