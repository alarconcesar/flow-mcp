#!/usr/bin/env python3
"""Diagnóstico: endpoints y auth reales de la nueva app flow.google.com."""
import asyncio
from playwright.async_api import async_playwright

PROFILE = "/home/ubuntu/.local/share/gflow-cli/profile_cesaralarcon080405"

async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, headless=True,
            args=["--no-sandbox", "--password-store=basic",
                  "--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        events = []

        def on_req(req):
            try:
                u = req.url
                if any(k in u for k in ("trpc", "/api/", "batch", "Generate", "generate",
                                        "media", "Create", "create")):
                    auth = req.headers.get("authorization", "")
                    events.append((req.method, u[:200], auth[:70]))
            except Exception:
                pass

        page.on("request", on_req)
        print("== Navegando a flow.google.com ==", flush=True)
        try:
            await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"(navegación: {e})", flush=True)
        await asyncio.sleep(14)
        print(f"== Requests relevantes: {len(events)} ==", flush=True)
        for m, u, a in events[:70]:
            print(f"{m} {u} | AUTH={a!r}", flush=True)

        print("\n== Sondeos directos ==", flush=True)
        for u in ["https://labs.google/fx/api/auth/session",
                  "https://flow.google.com/api/auth/session"]:
            try:
                r = await page.goto(u, wait_until="domcontentloaded", timeout=20000)
                b = await page.evaluate("document.body ? document.body.innerText : ''")
                print(f"{u} -> {r.status if r else '?'} | {b[:400]!r}", flush=True)
            except Exception as e:
                print(f"{u} -> ERR {type(e).__name__}: {str(e)[:150]}", flush=True)

        await ctx.close()

asyncio.run(main())
