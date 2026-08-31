"""How contested is a pump.fun launch, and how fast does a launch appear?

The survey that asked for four hundred signatures and got three was dropping
every failed transaction, and on this program most transactions fail. That is
not noise to filter out, it is the single most useful thing the chain has said
so far: those failures are bots racing each other for the same launches and
losing. A strategy that means to buy at second zero has to win that race.

So this measures two things properly. First, what share of transactions on
each program actually succeed, over a real sample rather than a page. Second,
how far back a thousand signatures reaches, which is the ceiling on how long
a poll can be blind before it starts missing launches.

Usage: python -m src.rpccheck
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx

RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
PROGRAMS = {
    "pump.fun bonding curve": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pump.fun AMM": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "raydium launchpad": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
}
PAGES = 3


async def page(client, key: str, program: str, before: str | None):
    params: list = [program, {"limit": 1000}]
    if before:
        params[1]["before"] = before
    response = await client.post(RPC.format(key=key), json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress", "params": params,
    })
    payload = response.json()
    if "error" in payload:
        print("  RPC ERROR:", json.dumps(payload["error"])[:200])
        return []
    return payload.get("result") or []


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")

    async with httpx.AsyncClient(timeout=90.0) as client:
        for label, program in PROGRAMS.items():
            print(f"=== {label} ===", flush=True)
            rows: list[dict] = []
            before = None
            for _ in range(PAGES):
                batch = await page(client, key, program, before)
                if not batch:
                    break
                rows.extend(batch)
                before = batch[-1]["signature"]
                if len(batch) < 1000:
                    break
            if not rows:
                print("  nothing returned\n", flush=True)
                continue

            failed = sum(1 for r in rows if r.get("err"))
            stamps = [r.get("blockTime") or 0 for r in rows if r.get("blockTime")]
            span = (max(stamps) - min(stamps)) if stamps else 0
            newest_age = time.time() - max(stamps) if stamps else 0

            print(f"  signatures: {len(rows)}")
            print(f"  failed: {failed} ({100 * failed / len(rows):.1f}%)")
            print(f"  succeeded: {len(rows) - failed} "
                  f"({100 * (len(rows) - failed) / len(rows):.1f}%)")
            print(f"  span covered: {span:.0f}s ({span / 60:.1f} min)")
            print(f"  newest is {newest_age:.0f}s old")
            if span:
                print(f"  rate: {len(rows) / span * 60:.0f} transactions/min, "
                      f"{(len(rows) - failed) / span * 60:.0f} succeeding/min")
            print(flush=True)


if __name__ == "__main__":
    asyncio.run(main())
