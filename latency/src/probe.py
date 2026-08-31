"""Find out what a launch actually looks like on chain.

The first probe asked for a hundred signatures, got eleven parsed
transactions back, and found no CREATE_POOL among them - while the race it
was checking had reported forty-four "pools" that turned out to be the
accounts of about two transactions. So nothing about the chain path is known
yet, and this widens the search rather than assuming a label.

It walks several hundred recent signatures from each candidate program,
parses them in batches of a hundred (the parse endpoint's limit, which the
first probe was silently exceeding), and reports what types actually occur.
Any type that looks like a creation gets its shape dumped so the extractor
can be written against it.

Usage: python -m src.probe [signatures_per_program]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx

RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
PARSE = "https://api.helius.xyz/v0/transactions?api-key={key}"
WSOL = "So11111111111111111111111111111111111111112"

# Launches may live on any of these; the histogram says which.
PROGRAMS = {
    "pump.fun bonding curve": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pump.fun AMM": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "raydium launchpad": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
}
CREATION_HINTS = ("CREATE", "INITIALIZE", "POOL", "MINT")


async def signatures(client, key: str, program: str, wanted: int) -> list[str]:
    out: list[str] = []
    before = None
    while len(out) < wanted:
        params: list = [program, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        payload = await client.post(RPC.format(key=key), json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress", "params": params,
        })
        rows = (payload.json() or {}).get("result") or []
        if not rows:
            break
        out.extend(r["signature"] for r in rows if not r.get("err"))
        before = rows[-1]["signature"]
        if len(rows) < 1000:
            break
    return out[:wanted]


async def parse_all(client, key: str, sigs: list[str]) -> list[dict]:
    parsed: list[dict] = []
    # The parse endpoint takes a hundred at a time; asking for more silently
    # returns a short answer, which is how the first probe saw eleven.
    for start in range(0, len(sigs), 100):
        payload = await client.post(
            PARSE.format(key=key), json={"transactions": sigs[start:start + 100]}
        )
        try:
            batch = payload.json()
        except ValueError:
            continue
        if isinstance(batch, list):
            parsed.extend(t for t in batch if isinstance(t, dict))
    return parsed


def dump(tx: dict) -> None:
    age = time.time() - (tx.get("timestamp") or 0)
    print(f"  --- {tx.get('type')} | source={tx.get('source')} | {age/60:.0f} min old ---")
    print("   ", (tx.get("description") or "")[:150])
    transfers = tx.get("tokenTransfers") or []
    mints = sorted({t.get("mint") for t in transfers if t.get("mint") != WSOL})
    print(f"    tokenTransfers={len(transfers)} non-WSOL mints={mints[:3]}")
    for t in transfers[:4]:
        print(f"      mint={str(t.get('mint'))[:12]} amt={t.get('tokenAmount')} "
              f"to={str(t.get('toUserAccount'))[:10]}")
    print(f"    accountData entries={len(tx.get('accountData') or [])}")
    events = tx.get("events") or {}
    if events:
        print("    events:", json.dumps(events, default=str)[:300])
    print()


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")
    wanted = int(sys.argv[1]) if len(sys.argv) > 1 else 400

    async with httpx.AsyncClient(timeout=60.0) as client:
        for label, program in PROGRAMS.items():
            print(f"=== {label} ({program[:10]}...) ===", flush=True)
            sigs = await signatures(client, key, program, wanted)
            if not sigs:
                print("  no signatures returned\n", flush=True)
                continue
            parsed = await parse_all(client, key, sigs)
            print(f"  {len(sigs)} signatures -> {len(parsed)} parsed")
            if parsed:
                span = time.time() - min(t.get("timestamp") or 0 for t in parsed)
                print(f"  covering the last {span/60:.0f} minutes")
            kinds: dict[str, int] = {}
            for tx in parsed:
                kinds[tx.get("type") or "?"] = kinds.get(tx.get("type") or "?", 0) + 1
            print(f"  types: {dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}")

            interesting = [
                tx for tx in parsed
                if any(h in (tx.get("type") or "") for h in CREATION_HINTS)
            ]
            print(f"  creation-shaped: {len(interesting)}")
            for tx in interesting[:2]:
                dump(tx)
            if not interesting and parsed:
                print("  no creation-shaped type; sample of what is there:")
                dump(parsed[0])
            print(flush=True)


if __name__ == "__main__":
    asyncio.run(main())
