"""E2E del batch de prompts: 2 prompts DISTINTOS en una sola llamada.

Uso:
    GFLOW_PROFILE=cesaralarcon080405 python debug-2026-10/probe_batch.py

Esperado: dos JPEG distintos (uno por prompt) generados en el mismo
batchexecute, no en dos llamadas separadas.
"""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from flow_mcp.generator import generate_images  # noqa: E402

PROMPTS = [
    "Flat vector mascot: friendly female robot assistant head, "
    "navy blue and orange palette, minimal, white background, centered",
    "Flat vector mascot: round orange cat mascot head, navy blue outline, "
    "minimal, white background, centered",
]
OUT_DIR = Path(__file__).resolve().parent


def _prog(n: int, t: int, msg: str) -> None:
    print(f"[{n}/{t}] {msg}", flush=True)


async def main() -> int:
    started = time.time()
    try:
        res = await generate_images(
            model="nano-pro",
            prompts=PROMPTS,
            aspect="1:1",
            output_dir=str(OUT_DIR),
            _progress_cb=_prog,
        )
    except Exception:
        print("── FALLÓ ──", flush=True)
        traceback.print_exc()
        return 1

    elapsed = time.time() - started
    print(f"OK en {elapsed:.1f}s, {len(res.files)} archivo(s):", flush=True)
    for f in res.files:
        p = Path(f)
        print(f"  {p.name}  {p.stat().st_size / 1024:.0f} KB", flush=True)
    return 0 if len(res.files) == len(PROMPTS) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
