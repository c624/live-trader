"""Can a free public RPC carry the pilot, or does this need a paid account?

Quote, build and sign together take 0.2 seconds. The send is the only part of
the trading path left unmeasured, and it is the only part an RPC provider
changes -- so it decides whether an account is worth opening at all. Rather
than assume, this asks the free endpoints directly.

Four things matter, and none of them require spending SOL:

  reachable     does the endpoint answer at all from a GitHub runner
  blockhash     round trip for getLatestBlockhash, which every send needs first
  balance       can it read our own wallet, the call the loop makes each cycle
  burst         does a short burst of calls get rate limited, since the loop
                makes several calls per cycle and a 429 mid-trade is a stuck
                position rather than a slow one

sendTransaction is deliberately not exercised: landing a real transaction costs
real fees, and the pilot itself measures that honestly. What this can rule out
is an endpoint that cannot even be read from reliably.

Usage: python -m src.rpcshop [burst]
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import httpx

WALLET = "DtFUr5giXgjMEFKD89xZfg4dw6gJ8bEGc2acQ4XunpEX"

# Free, no account, no key in the URL. A configured SOLANA_RPC_URL is added
# to the comparison when present, and is never printed.
PUBLIC = {
    "solana mainnet-beta": "https://api.mainnet-beta.solana.com",
    "ankr": "https://rpc.ankr.com/solana",
    "publicnode": "https://solana-rpc.publicnode.com",
    "drpc": "https://solana.drpc.org",
    "blockworks": "https://api.blockworks.dev/solana",
}


def call(client: httpx.Client, url: str, method: str, params: list):
    started = time.time()
    try:
        r = client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                   "method": method, "params": params})
    except Exception as exc:
        return None, f"{type(exc).__name__}", time.time() - started
    elapsed = time.time() - started
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}", elapsed
    try:
        payload = r.json()
    except Exception:
        return None, "not JSON", elapsed
    if "error" in payload:
        return None, str(payload["error"])[:60], elapsed
    return payload.get("result"), "", elapsed


def measure(name: str, url: str, burst: int) -> None:
    print(f"=== {name} ===", flush=True)
    with httpx.Client(timeout=15.0) as client:
        result, err, elapsed = call(client, url, "getLatestBlockhash", [])
        if err:
            print(f"  unusable: {err} after {elapsed*1000:.0f}ms")
            return
        print(f"  blockhash  {elapsed*1000:.0f}ms")

        bal, err, elapsed = call(client, url, "getBalance", [WALLET])
        if err:
            print(f"  balance    FAILED: {err}")
        else:
            lamports = (bal or {}).get("value", 0)
            print(f"  balance    {elapsed*1000:.0f}ms  ({lamports/1e9:.4f} SOL)")

        times, refused = [], 0
        for _ in range(burst):
            _, err, elapsed = call(client, url, "getLatestBlockhash", [])
            if err:
                refused += 1
            else:
                times.append(elapsed)
        if times:
            print(f"  burst      {len(times)}/{burst} ok, "
                  f"median {statistics.median(times)*1000:.0f}ms, "
                  f"slowest {max(times)*1000:.0f}ms, refused {refused}")
        else:
            print(f"  burst      all {burst} refused")


def main() -> None:
    burst = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    endpoints = dict(PUBLIC)
    configured = os.environ.get("SOLANA_RPC_URL", "").strip()
    if configured:
        # Named, never printed: the credential lives in the path.
        endpoints["configured SOLANA_RPC_URL"] = configured

    for name, url in endpoints.items():
        try:
            measure(name, url, burst)
        except Exception as exc:
            print(f"=== {name} ===\n  probe crashed: {type(exc).__name__}: {exc}")
        print(flush=True)

    print("A usable endpoint answers a blockhash in well under a second and")
    print("survives the burst. The loop makes a handful of calls per cycle, so")
    print("refusals under a 15-call burst mean a trade can stick mid-flight.")


if __name__ == "__main__":
    main()
