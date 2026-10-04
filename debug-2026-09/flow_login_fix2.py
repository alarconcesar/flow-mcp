#!/usr/bin/env python3
"""Login asistido v2 para flow-mcp.
- Abre Chromium en :98, NAVEGA SOLO a la mejor página de login disponible.
- Oculta el diálogo 'Restore pages?' (--hide-crash-restore-bubble).
- Detecta la sesión de Flow (sin CORS) y guarda .gflow_account al lograrlo.
"""
import asyncio, json, re, sys, time
from pathlib import Path
from playwright.async_api import async_playwright

PROFILE = Path("/home/ubuntu/.local/share/gflow-cli/profile_cesaralarcon080405")
SESSION_API = "https://labs.google/fx/api/auth/session"
TIMEOUT = 900  # 15 min para que César complete el login
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

async def check_session(ctx):
    try:
        p = await ctx.new_page()
        try:
            r = await p.goto(SESSION_API, wait_until="domcontentloaded", timeout=25000)
            body = await p.evaluate("document.body ? document.body.innerText : ''")
            status = r.status if r else 0
        finally:
            await p.close()
        if status == 200 and body and body.strip() not in ("{}", ""):
            m = EMAIL_RE.search(body)
            return (m.group(0) if m else "detected"), body
        return None, body
    except Exception as e:
        return None, f"ERR {type(e).__name__}: {e}"

async def has_login_input(page):
    try:
        return bool(await page.evaluate(
            "!!document.querySelector('input[type=email], input[name=identifier]')"))
    except Exception:
        return False

async def steer_to_login(page):
    """Navega sola hasta una pantalla con campo de login visible."""
    for url in ["https://accounts.google.com/", "https://mail.google.com/"]:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)
            u = page.url
            has = await has_login_input(page)
            print(f"[steer] {url} -> {u[:110]} | input_login={has}", flush=True)
            if has:
                return True
        except Exception as e:
            print(f"[steer] {url} FALLO: {e}", flush=True)
    # fallback: home de Flow + click en un botón que dispare el login
    try:
        await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)
        for sel in ["text=/sign in/i", "text=/get started/i", "text=/new project/i"]:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    print(f"[steer] click -> {sel}", flush=True)
                    await loc.click(timeout=6000)
                    await asyncio.sleep(5)
                    u = page.url
                    has = await has_login_input(page)
                    print(f"[steer] tras click -> {u[:110]} | input_login={has}", flush=True)
                    if has:
                        return True
            except Exception:
                continue
    except Exception as e:
        print(f"[steer] fallback FALLO: {e}", flush=True)
    return False

async def main():
    async with async_playwright() as pw:
        try:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE), headless=False,
                args=["--no-sandbox", "--password-store=basic",
                      "--disable-blink-features=AutomationControlled",
                      "--hide-crash-restore-bubble"],
                viewport={"width": 1280, "height": 800},
            )
        except Exception as e:
            print(f"ERROR lanzando Chrome: {e}", flush=True)
            sys.exit(2)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        ok = await steer_to_login(page)
        print("== MIRA EL VNC: pantalla de login lista ==" if ok
              else "== AVISO: no encontré pantalla de login; revisa el VNC ==", flush=True)
        print("== Inicia sesión con cesaralarcon080405@gmail.com — se detecta solo ==", flush=True)

        deadline = time.monotonic() + TIMEOUT
        n = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(8)
            n += 1
            email, body = await check_session(ctx)
            if email:
                (PROFILE / ".gflow_account").write_text(email, encoding="utf-8")
                print(f"\n  ✅ SESIÓN DETECTADA: {email}", flush=True)
                await ctx.close()
                print("== LISTO: sesión guardada, Chrome cerrado. ==", flush=True)
                sys.exit(0)
            print(f"  ⏳ Esperando login... ({n*8}s)", flush=True)
        print("❌ Timeout (15 min).", flush=True)
        await ctx.close()
        sys.exit(1)

asyncio.run(main())
