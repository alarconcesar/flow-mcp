#!/usr/bin/env python3
"""
generate.py — Generate image via Google Flow batchGenerateImages API.
Bypasses Flow Agent (chat) quota entirely.
"""
import asyncio, argparse, json, os, sys, time, base64
sys.path.insert(0, os.path.expanduser("~/.local/share/uv/tools/gflow-cli/lib/python3.11/site-packages"))
from playwright.async_api import async_playwright
from gflow_cli.api.recaptcha import TokenMinter

PROFILE_DIR = os.path.expanduser("~/.local/share/gflow-cli/profile_cesaralarcon080405")

ASPECT_MAP = {"9:16":"IMAGE_ASPECT_RATIO_PORTRAIT","16:9":"IMAGE_ASPECT_RATIO_LANDSCAPE",
              "1:1":"IMAGE_ASPECT_RATIO_SQUARE","4:3":"IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
              "3:4":"IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"}
MODEL_MAP = {"nano2":"NARWHAL","nano-pro":"GEM_PIX_2","narwhal":"NARWHAL","gem_pix_2":"GEM_PIX_2"}

async def generate(prompt: str, model: str = "nano2", count: int = 1,
                   aspect: str = "9:16", out_dir: str = "/tmp") -> list[str]:
    wire_model = MODEL_MAP.get(model.lower(), "NARWHAL")
    wire_aspect = ASPECT_MAP.get(aspect, "IMAGE_ASPECT_RATIO_PORTRAIT")
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, headless=False,
            args=["--no-sandbox","--password-store=basic","--disable-gpu",
                  "--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"],
            viewport={"width":1280,"height":720})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        bearer = None
        def on_req(req):
            nonlocal bearer
            if not bearer:
                a = req.headers.get("authorization","")
                if a.startswith("Bearer ya29"): bearer = a[7:]
        page.on("request", on_req)
        await page.goto("https://labs.google/fx/tools/flow", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(8000)
        if not bearer:
            await page.wait_for_timeout(5000)
        if not bearer:
            raise RuntimeError("Failed to capture Bearer token")
        recaptcha_token = await TokenMinter(page).mint("IMAGE_GENERATION")
        t = str(int(time.time() * 1000))
        await page.add_script_tag(content=f"""(async()=>{{try{{const r=await fetch('https://labs.google/fx/api/trpc/project.createProject',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{"json":{{"projectTitle":"g_{t}","toolName":"TOOL_NAME_UNSPECIFIED"}}}}),credentials:'include'}});const p=JSON.parse(await r.text());window.__st={{pid:p.result.data.json.result.projectId}};}}catch(e){{window.__st={{error:e.toString()}};}}}})();""")
        await page.wait_for_timeout(5000)
        st = await page.evaluate("window.__st")
        pid = st.get("pid") if st else None
        if not pid:
            raise RuntimeError(f"Project creation failed: {st}")
        sid = ";" + t
        cctx = {"tool":"PINHOLE","projectId":pid,"sessionId":sid,
                "recaptchaContext":{"token":recaptcha_token,"applicationType":"RECAPTCHA_APPLICATION_TYPE_WEB"}}
        body = {"clientContext":cctx,"mediaGenerationContext":{"batchId":f"g_{t}"},"useNewMedia":True,
                "requests":[{"clientContext":cctx,"imageModelName":wire_model,"imageAspectRatio":wire_aspect,
                "structuredPrompt":{"parts":[{"text":prompt}]},"seed":int(time.time()),"imageInputs":[]}]}
        api_url = f"https://aisandbox-pa.googleapis.com/v1/projects/{pid}/flowMedia:batchGenerateImages"
        body_json = json.dumps(json.dumps(body))
        await page.add_script_tag(content=f"""(async()=>{{try{{const r=await fetch('{api_url}',{{method:'POST',headers:{{'Authorization':'Bearer {bearer}','Content-Type':'application/json;charset=UTF-8'}},body:{body_json}}});window.__gen=await r.text();}}catch(e){{window.__gen='ERR:'+e;}}}})();""")
        await page.wait_for_timeout(25000)
        raw = await page.evaluate("window.__gen")
        if raw is None:
            raise RuntimeError("API call returned None — prompt may have been silently blocked by content filter, or bearer token expired.")
        if isinstance(raw, str) and raw.startswith("ERR:"):
            raise RuntimeError(f"API call failed: {raw}")
        import re
        saved = []
        urls = re.findall(r'"fifeUrl"\s*:\s*"([^"]+)"', raw)
        for url in urls:
            url = url.replace('\\u0026','&')
            dl = await page.evaluate("""async(u)=>{const r=await fetch(u);const b=await r.blob();return await new Promise(r=>{const d=new FileReader();d.onload=()=>r(d.result);d.readAsDataURL(b);});}""", url)
            if dl and dl.startswith("data:"):
                img = dl.split(",",1)[1]
                ext = dl.split(";")[0].split("/")[1] or "webp"
                fp = os.path.join(out_dir, f"flow-gen-{t}.{ext}")
                with open(fp,"wb") as f: f.write(base64.b64decode(img))
                saved.append(fp)
                if len(saved) >= count: break
        if not saved:
            raise RuntimeError(f"No images: {raw[:500]}")
        await ctx.close()
        return saved

def main():
    p = argparse.ArgumentParser(description="Generate via Flow")
    p.add_argument("prompt"); p.add_argument("--model","-m",default="nano-pro",choices=["nano2","nano-pro","narwhal","gem_pix_2"])
    p.add_argument("--count","-n",type=int,default=1,choices=[1,2,3,4])
    p.add_argument("--aspect","-a",default="9:16",choices=["9:16","16:9","1:1","4:3","3:4"])
    p.add_argument("--out","-o",default="/tmp")
    a = p.parse_args()
    for f in asyncio.run(generate(a.prompt,a.model,a.count,a.aspect,a.out)):
        print(f"MEDIA:{f}  ({os.path.getsize(f)} bytes)")

if __name__ == "__main__":
    main()
