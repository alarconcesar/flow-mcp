#!/usr/bin/env python3
"""Diagnóstico: ¿hay sesión de Google activa en el perfil gflow?"""
import asyncio, sys
from playwright.async_api import async_playwright

PROFILE = "/home/ubuntu/.local/share/gflow-cli/profile_cesaralarcon080405"

async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, headless=True,
            args=["--no-sandbox", "--password-store=basic"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            # 1) API de sesión de Flow — navegación top-level (sin CORS)
            resp = await page.goto("https://labs.google/fx/api/auth/session",
                                   wait_until="domcontentloaded", timeout=30000)
            print("STATUS:", resp.status if resp else None)
            print("URL_FINAL:", page.url[:160])
            body = await page.evaluate("document.body ? document.body.innerText : ''")
            print("BODY[:600]:", repr(body[:600]))
            # ¿JSON con token?
            has_token = "access_token" in body or "ya29" in body
            print("TIENE_TOKEN:", has_token)
            # 2) Página de Flow directamente
            resp2 = await page.goto("https://labs.google/fx/tools/flow",
                                    wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            print("FLOW_STATUS:", resp2.status if resp2 else None)
            print("FLOW_URL_FINAL:", page.url[:200])
            title = await page.title()
            print("FLOW_TITLE:", title[:120])
            await ctx.close()
        except Exception as e:
            print("ERROR:", type(e).__name__, str(e)[:300])
            try:
                await ctx.close()
            except Exception:
                pass

asyncio.run(main())
