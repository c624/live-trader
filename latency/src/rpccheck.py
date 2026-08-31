"""Print what the RPC actually says, rather than what the code assumed.

The survey asked for four hundred signatures per program and got three, so
the failure is in the request rather than in the market. Silent empties have
cost several rounds here already; this prints the raw response, including any
error object, before anything is parsed out of it.

Usage: python -m src.rpccheck
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
PROGRAMS = {
    "pump.fun bonding curve": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pump.fun AMM": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
}


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")

    async with httpx.AsyncClient(timeout=60.0) as client:
        for label, program in PROGRAMS.items():
            for limit in (1000, 100):
                response = await client.post(RPC.format(key=key), json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [program, {"limit": limit}],
                })
                print(f"--- {label}, limit={limit} ---")
                print("  http status:", response.status_code)
                try:
                    payload = response.json()
                except ValueError:
                    print("  body was not json:", response.text[:200])
                    continue
                print("  response keys:", sorted(payload.keys()))
                if "error" in payload:
                    print("  ERROR:", json.dumps(payload["error"])[:300])
                result = payload.get("result")
                if isinstance(result, list):
                    print(f"  signatures returned: {len(result)}")
                    if result:
                        print("  newest:", json.dumps(result[0], default=str)[:220])
                else:
                    print("  result was:", str(result)[:200])
                print()


if __name__ == "__main__":
    asyncio.run(main())
