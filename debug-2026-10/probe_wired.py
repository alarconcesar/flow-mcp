"""E2E del camino REAL del MCP: llama a ``generator.generate_images`` (código ya cableado).

Uso:
    GFLOW_PROFILE=cesaralarcon080405 python debug-2026-10/probe_wired.py

Sirve para separar "el código no funciona" de "el proceso MCP se cae":
imprime el traceback completo si algo falla.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from flow_mcp.generator import generate_images  # noqa: E402

PROMPT = (
    "Flat vector mascot: friendly female robot assistant head, "
    "navy blue and orange palette, minimal, white background, centered"
)
OUT_DIR = Path(__file__).resolve().parent


def _prog(n: int, t: int, msg: str) -> None:
    print(f"[{n}/{t}] {msg}", flush=True)


async def main() -> int:
    try:
        res = await generate_images(
            PROMPT,
            model="nano-pro",
            count=1,
            aspect="1:1",
            output_dir=str(OUT_DIR),
            _progress_cb=_prog,
        )
    except Exception:
        print("── FALLÓ ──", flush=True)
        traceback.print_exc()
        return 1

    print("OK, archivos:", flush=True)
    for f in res.files:
        p = Path(f)
        print(f"  {p}  ({p.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
