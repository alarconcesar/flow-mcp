#!/usr/bin/env python3
"""Test decisivo: ¿qué auth acepta aisandbox-pa y de dónde sale el token?"""
import asyncio, re, sys
from playwright.async_api import async_playwright

PROFILE = "/home/ubuntu/.local/share/gflow-cli/profile_cesaralarcon080405"
YA29_RE = re.compile(r"ya29\.[A-Za-z0-9_\-\.]{10,}")
AT_RE = re.compile(r"(?:^|&)at=([^&]+)")

async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, headless=True,
            args=["--no-sandbox", "--password-store=basic",
                  "--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        found = {}
        asi_events = []

        def on_req(req):
            try:
                u = req.url
                a = req.headers.get("authorization", "")
                if "ya29" in a:
                    found.setdefault("header_ya29", a[:70])
                if req.method == "POST":
                    d = req.post_data or ""
                    m = YA29_RE.search(d)
                    if m and "body_ya29" not in found:
                        found["body_ya29"] = m.group(0)[:70]
                    m2 = AT_RE.search(d)
                    if m2 and "at_param" not in found:
                        found["at_param"] = m2.group(1)[:70]
                if "aisandbox" in u or "pa.googleapis" in u:
                    asi_events.append((req.method, u[:130], a[:40]))
            except Exception:
                pass

        page.on("request", on_req)
        await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(9)
        # abrir un proyecto existente para disparar más tráfico autenticado
        try:
            await page.goto("https://flow.google.com/project/a9591705-48d4-43d1-b464-d2693647844c",
                            wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("proj nav:", str(e)[:100])
        await asyncio.sleep(12)

        print("== TOKENS encontrados ==")
        for k, v in found.items():
            print(f"  {k}: {v}")
        print(f"== requests a aisandbox: {len(asi_events)} ==")
        for m, u, a in asi_events[:12]:
            print(f"  {m} {u} AUTH={a!r}")

        # prueba 1: creditBalance sin bearer
        r1 = await page.evaluate("""async () => {
            try { const r = await fetch('https://aisandbox-pa.googleapis.com/v1/flow/creditBalance', {credentials:'include'});
                  return {s: r.status, b: (await r.text()).slice(0,300)}; }
            catch(e){ return {s:0, b:'ERR '+e}; } }""")
        print("== creditBalance SIN bearer:", r1)

        # prueba 2: con el token capturado
        tok = None
        for k in ("header_ya29", "body_ya29", "at_param"):
            if k in found:
                tok = found[k].replace("Bearer ", "")
                break
        if tok:
            r2 = await page.evaluate("""async ([t]) => {
                try { const r = await fetch('https://aisandbox-pa.googleapis.com/v1/flow/creditBalance',
                    {credentials:'include', headers: {'authorization': 'Bearer '+t}});
                  return {s: r.status, b: (await r.text()).slice(0,300)}; }
                catch(e){ return {s:0, b:'ERR '+e}; } }""", [tok])
            print("== creditBalance CON bearer:", r2)
        else:
            print("== No se encontró token alguno ==")

        await ctx.close()

asyncio.run(main())
