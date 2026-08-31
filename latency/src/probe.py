"""Dump the shape of a real pump.fun launch so the extractor is written
against what the chain actually returns rather than against a guess.

Three parsers in this project have now been written from assumptions about a
payload's shape and all three were wrong in a way that only surfaced after a
full run. The discovery path decides which token gets bought, so it gets
looked at first.

Usage: python -m src.probe [how_many]
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
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL = "So11111111111111111111111111111111111111112"


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")
    wanted = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    async with httpx.AsyncClient(timeout=45.0) as client:
        sigs = (await client.post(RPC.format(key=key), json={
            "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
            "params": [PUMP_FUN_PROGRAM, {"limit": 100}],
        })).json().get("result") or []
        recent = [s["signature"] for s in sigs if not s.get("err")][:100]
        parsed = (await client.post(PARSE.format(key=key),
                                    json={"transactions": recent})).json()

    if not isinstance(parsed, list):
        print("unexpected parse response:", str(parsed)[:300])
        return

    kinds: dict[str, int] = {}
    for tx in parsed:
        if isinstance(tx, dict):
            kinds[tx.get("type") or "?"] = kinds.get(tx.get("type") or "?", 0) + 1
    print(f"transaction types in the last {len(parsed)}: {kinds}\n")

    shown = 0
    for tx in parsed:
        if not isinstance(tx, dict) or tx.get("type") != "CREATE_POOL":
            continue
        shown += 1
        age = time.time() - (tx.get("timestamp") or 0)
        print(f"--- CREATE_POOL {shown}, {age:.0f}s old ---")
        print("  description:", (tx.get("description") or "")[:160])
        print("  top-level keys:", sorted(tx.keys()))
        transfers = tx.get("tokenTransfers") or []
        print(f"  tokenTransfers ({len(transfers)}):")
        for t in transfers[:6]:
            print(f"    mint={t.get('mint')} amount={t.get('tokenAmount')} "
                  f"to={str(t.get('toUserAccount'))[:8]}")
        print("  non-WSOL mints:",
              sorted({t.get("mint") for t in transfers if t.get("mint") != WSOL}))
        accounts = [a.get("account") for a in (tx.get("accountData") or [])]
        print(f"  accountData entries: {len(accounts)}; first 6: {accounts[:6]}")
        events = tx.get("events") or {}
        print("  events keys:", sorted(events.keys()))
        if events:
            print("  events sample:", json.dumps(events, default=str)[:400])
        print()
        if shown >= wanted:
            break
    if not shown:
        print("no CREATE_POOL in this batch; pump.fun may be quiet or the "
              "type label differs - the type histogram above is the answer")


if __name__ == "__main__":
    asyncio.run(main())
