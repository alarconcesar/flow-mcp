"""E2E del lote largo: 6 prompts → 2 rondas de 4 y 2 → 6 imágenes.

Uso:
    GFLOW_PROFILE=cesaralarcon080405 python debug-2026-10/probe_lote.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from flow_mcp.server import generate_batch  # noqa: E402

PROMPTS = [
    "Flat vector icon: coffee cup, single flat color, white background, centered",
    "Flat vector icon: headphones, single flat color, white background, centered",
    "Flat vector icon: vinyl record, single flat color, white background, centered",
    "Flat vector icon: microphone, single flat color, white background, centered",
    "Flat vector icon: guitar pick, single flat color, white background, centered",
    "Flat vector icon: speaker, single flat color, white background, centered",
]
OUT_DIR = Path(__file__).resolve().parent / "lote6"


def _prog(n: int, t: int, msg: str) -> None:
    print(f"[{n}/{t}] {msg}", flush=True)


async def main() -> int:
    started = time.time()
    import os

    os.environ["GFLOW_OUTPUT_DIR"] = str(OUT_DIR)
    raw = await generate_batch(prompts=PROMPTS, aspect="1:1", ctx=None)
    out = json.loads(raw)
    elapsed = time.time() - started

    print(f"\n{elapsed:.1f}s · success={out['success']} · "
          f"{out['generated']}/{out['requested']} imágenes en {out['rounds']} rondas")
    for img in out.get("images", []):
        p = Path(img["path"])
        print(f"  #{img['index']} {p.name} {img['kb']} KB · {p.exists()}"
              f" · {img['prompt'][:40]}")
    for err in out.get("errors", []):
        print("  ERROR:", err)
    return 0 if out["success"] and out["generated"] == len(PROMPTS) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
