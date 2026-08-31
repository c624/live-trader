"""Detect launches from a pushed websocket stream instead of a polled index.

Polling hit a wall that faster polling cannot move: at ten seconds between
polls the median detection was 25s, and at two seconds it was still 25s. The
delay is not our cadence, it is how long Helius's signature index takes to
surface a transaction. Asking a lagging endpoint more often does not make it
fresher.

A logs subscription is pushed rather than asked, so it should arrive within a
second or two of the transaction confirming. This measures whether it does.

Two things are deliberately not assumed. The instruction names that mark a
launch are not guessed at: every "Instruction:" line seen is counted and the
histogram is printed, so the vocabulary comes from the stream rather than
from me. And the latency is measured against each transaction's own block
time, fetched afterwards in batches, so the fetch cost does not contaminate
the number being measured.

Usage: python -m src.wswatch [minutes]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import websockets

WS = "wss://mainnet.helius-rpc.com/?api-key={key}"
RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_HINTS = ("Create", "Initialize", "InitializePool", "CreatePool")
MAX_TRACKED = 400


def instruction_names(logs: list[str]) -> list[str]:
    out = []
    for line in logs:
        marker = "Instruction: "
        if marker in line:
            out.append(line.split(marker, 1)[1].strip())
    return out


async def block_times(key: str, signatures: list[str]) -> dict[str, int]:
    """Block times for the signatures we caught, fetched after the fact."""
    found: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(signatures), 100):
            chunk = signatures[start:start + 100]
            try:
                response = await client.post(
                    f"https://api.helius.xyz/v0/transactions?api-key={key}",
                    json={"transactions": chunk},
                )
                batch = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(batch, list):
                continue
            for tx in batch:
                if isinstance(tx, dict) and tx.get("signature") and tx.get("timestamp"):
                    found[tx["signature"]] = int(tx["timestamp"])
    return found


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    seen_instructions: dict[str, int] = {}
    caught: list[tuple[str, float]] = []
    messages = 0
    deadline = time.time() + minutes * 60

    print(f"subscribing to pump.fun logs for {minutes:.0f} minutes\n", flush=True)
    async with websockets.connect(WS.format(key=key), max_size=None) as socket:
        await socket.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
            "params": [{"mentions": [PUMP_FUN]}, {"commitment": "processed"}],
        }))
        print("subscribe ack:", (await socket.recv())[:120], flush=True)

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=20.0)
            except asyncio.TimeoutError:
                print("  (20s with no messages)", flush=True)
                continue
            except websockets.ConnectionClosed as exc:
                print(f"  connection closed: {exc}", flush=True)
                break
            messages += 1
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            value = ((payload.get("params") or {}).get("result") or {}).get("value") or {}
            signature, logs = value.get("signature"), value.get("logs") or []
            if not signature:
                continue
            names = instruction_names(logs)
            for name in names:
                seen_instructions[name] = seen_instructions.get(name, 0) + 1
            if len(caught) < MAX_TRACKED and any(
                any(h in n for h in CREATE_HINTS) for n in names
            ):
                caught.append((signature, time.time()))
            if messages % 2000 == 0:
                print(f"  {messages} messages, {len(caught)} creation-shaped",
                      flush=True)

    print(f"\nmessages received: {messages}")
    print("instruction histogram (top 15):")
    for name, count in sorted(seen_instructions.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {name:32s} {count}")

    if not caught:
        print("\nnothing creation-shaped seen; the histogram above is the answer "
              "about what a launch is actually called")
        return

    print(f"\nresolving block times for {len(caught)} candidates...", flush=True)
    stamps = await block_times(key, [s for s, _ in caught])
    lags = sorted(
        arrived - stamps[sig] for sig, arrived in caught
        if sig in stamps and 0 <= arrived - stamps[sig] <= 300
    )
    print("\n=== TIME FROM CONFIRMATION TO US RECEIVING IT ===")
    if not lags:
        print("no block times resolved; cannot state a latency")
        return
    def pct(p):
        return lags[min(len(lags) - 1, int(len(lags) * p / 100))]
    print(f"n={len(lags)}  fastest {lags[0]:.0f}s  p25 {pct(25):.0f}s  "
          f"median {pct(50):.0f}s  p75 {pct(75):.0f}s  slowest {lags[-1]:.0f}s")
    print(f"\npolling gave a 25s median and would not go below it.")
    print("edge is +6.9% at a 30s entry and -7.6% at 60s; add 10-20s of")
    print("quoting, signing and landing to the median above.")


if __name__ == "__main__":
    asyncio.run(main())
