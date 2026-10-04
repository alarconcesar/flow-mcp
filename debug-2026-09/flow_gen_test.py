#!/usr/bin/env python3
"""Test E2E del pipeline flow-mcp (bypass directo, sin protocolo MCP)."""
import asyncio, os, sys

os.environ['GFLOW_PROFILE'] = 'cesaralarcon080405'
sys.path.insert(0, '/home/ubuntu/.hermes/hermes-agent/venv/lib/python3.11/site-packages')
from flow_mcp.generator import generate_images

async def main():
    try:
        result = await generate_images(
            prompt="minimal flat design: white circle centered on a soft blue gradient background, no text",
            model="nano2",
            count=1,
            aspect="1:1",
            output_dir="/tmp/flow-gen-test",
        )
        print("RESULT:", result.describe() if hasattr(result, "describe") else result)
    except Exception as e:
        print("ERROR:", type(e).__name__, str(e)[:500])

if __name__ == "__main__":
    asyncio.run(main())
