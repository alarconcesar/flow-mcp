"""Sonda: ¿dónde vive el site key de reCAPTCHA en la app nueva de Flow?

Uso: GFLOW_PROFILE=<perfil> python debug-2026-10/probe_recaptcha.py

Revisa la portada y una página de proyecto, e imprime scripts de recaptcha,
presencia de grecaptcha y candidatos de site key en WIZ_global_data.
"""

from __future__ import annotations

import asyncio
import re
import sys

sys.path.insert(0, "/home/ubuntu/projects/flow-mcp/src")

from playwright.async_api import async_playwright  # noqa: E402

from flow_mcp import flow_batch as fb  # noqa: E402
from flow_mcp.browser import create_browser_context  # noqa: E402
from flow_mcp.profile import resolve_profile  # noqa: E402

_PROBE_JS = """
() => {
    const out = { url: location.href, scripts: [], grecaptcha: typeof grecaptcha, enterprise: null, keys: [] };
    document.querySelectorAll('script[src]').forEach((s) => {
        const src = s.getAttribute('src') || '';
        if (/recaptcha|enterprise/i.test(src)) out.scripts.push(src);
    });
    try { out.enterprise = typeof grecaptcha !== 'undefined' ? !!grecaptcha.enterprise : null; } catch (e) { out.enterprise = 'err'; }
    const seen = new Set();
    const walk = (node, depth) => {
        if (depth > 4 || node === null || node === undefined) return;
        if (typeof node === 'string') {
            const m = node.match(/6[Ll][A-Za-z0-9_-]{20,}/g);
            if (m) m.forEach((k) => { if (!seen.has(k)) { seen.add(k); out.keys.push(k); } });
            return;
        }
        if (typeof node === 'object') {
            let n = 0;
            for (const k of Object.keys(node)) { if (n++ > 300) break; walk(node[k], depth + 1); }
        }
    };
    walk(globalThis.WIZ_global_data || {}, 0);
    out.wizKeys = Object.keys(globalThis.WIZ_global_data || {});
    return out;
}
"""


async def report(page, label: str) -> str | None:
    print(f"\n── {label} ──")
    info = await page.evaluate(_PROBE_JS)
    print(f"url: {info['url']}")
    print(f"grecaptcha: {info['grecaptcha']} (enterprise={info['enterprise']})")
    print(f"scripts recaptcha: {info['scripts'] or '—'}")
    print(f"site keys halladas: {info['keys'] or '—'}")
    print(f"claves de WIZ_global_data: {len(info['wizKeys'])}")
    return info["keys"][0] if info["keys"] else None


async def main() -> None:
    profile = resolve_profile()
    async with async_playwright() as pw:
        ctx = await create_browser_context(pw, profile, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        info = await fb.wait_for_flow_ready(page)
        await report(page, "portada flow.google.com")

        pid = await fb.current_project_id(page)
        if pid:
            print(f"\nnavegando al proyecto {pid}…")
            await page.goto(f"https://flow.google.com/project/{pid}",
                            wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(12_000)
            await report(page, "página de proyecto")

        # Buscar el key en los bundles JS de la app
        print("\n── bundles JS ──")
        found = await page.evaluate("""
        async () => {
            const srcs = Array.from(document.querySelectorAll('script[src]'))
                .map((s) => s.src).filter((s) => s.includes('flow.google.com/'));
            const hits = [];
            for (const src of srcs.slice(0, 12)) {
                try {
                    const t = await (await fetch(src)).text();
                    const m = t.match(/6[Ll][A-Za-z0-9_-]{30,}/g);
                    if (m) hits.push([src.split('/').pop(), Array.from(new Set(m)).slice(0, 3)]);
                } catch (e) { /* ignore */ }
            }
            return { scanned: srcs.length, hits };
        }
        """)
        print(f"bundles escaneados: {found['scanned']}")
        for name, keys in (found.get("hits") or [])[:8]:
            print(f"  {name}: {keys}")

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
