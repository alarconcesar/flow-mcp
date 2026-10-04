"""Sonda de la migración batchexecute: sesión, tokens `at` y proyecto.

Uso:  GFLOW_PROFILE=<perfil> python debug-2026-10/probe_page.py

No genera nada (no quema captcha): solo navega a flow.google.com, lee
WIZ_global_data y lista el media del proyecto actual.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/home/ubuntu/projects/flow-mcp/src")

from playwright.async_api import async_playwright  # noqa: E402

from flow_mcp import flow_batch as fb  # noqa: E402
from flow_mcp.browser import create_browser_context, ensure_xvfb  # noqa: E402
from flow_mcp.profile import resolve_profile  # noqa: E402


def _mask(value: str | None) -> str:
    if not value:
        return "—"
    return f"{value[:10]}…({len(value)})"


async def main() -> None:
    profile = resolve_profile()
    print(f"perfil: {profile}")

    async with async_playwright() as pw:
        # headless=True: igual que browser_pool (el Xvfb del host exige -auth propio).
        ctx = await create_browser_context(pw, profile, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        info = await fb.wait_for_flow_ready(page)
        print(f"host: {info.get('host')}  path: {info.get('path')}")
        print(f"at:   {_mask(info.get('at'))}")
        print(f"sid:  {_mask(info.get('sid'))}")
        print(f"bl:   {_mask(info.get('bl'))}")

        pid = await fb.current_project_id(page)
        print(f"proyecto en URL/estado: {pid or '—'}")
        if not pid:
            print("creando proyecto por batchexecute…")
            pid = await fb.create_project(page, "flow-mcp")
            fb.store_project_id(pid)
            print(f"proyecto creado y guardado: {pid}")

        # RPC sin captcha: valida transporte + envelope + parser de punta a punta.
        _, text = await fb.run_rpc(
            page, fb.RPC_PROJECT_MEDIA, fb.project_media_request(pid), max_chars=400_000
        )
        print(f"respuesta: {len(text)} chars, empieza con {text[:12]!r}")
        print(f"envelopes: {[(r.rpcid, r.ok) for r in fb.parse_envelope(text)]}")
        payload = fb.first_payload(text, fb.RPC_PROJECT_MEDIA)
        images = fb.read_images(payload)
        print(f"media en el proyecto: {len(images)}")
        for img in images[:3]:
            print(f"  - {img.media_id}  {img.url[:70]}…")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
