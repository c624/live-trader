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

Sending is checked too, and it has to be, because a public endpoint that reads
fine may still throttle or refuse sendTransaction -- and that failure mode is
a position we bought and cannot sell. It is checked without spending anything:
the transaction is signed by a freshly generated keypair holding zero SOL, so
it cannot pay a fee and therefore cannot land. What comes back separates the
two cases. "insufficient funds" or "account not found" means the endpoint
processed the send and the path is open; a 403, 410 or "unsupported method"
means sending is disabled there and the endpoint is useless to us no matter
how fast it reads.

Our own wallet is never involved in that check.

Usage: python -m src.rpcshop [burst]
"""

from __future__ import annotations

import base64
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


# Errors that prove the endpoint accepted and processed a send. The transaction
# is unfunded, so a healthy endpoint rejects it for exactly these reasons.
PROCESSED = ("insufficient funds", "account not found", "accountnotfound",
             "attempt to debit", "blockhash not found", "insufficientfunds")
# Errors that prove sending is closed to us regardless of read speed.
REFUSED = ("unsupported", "not supported", "disabled", "forbidden",
           "unauthorized", "rate limit", "too many requests", "method not found")


def can_send(client: httpx.Client, url: str) -> tuple[str, str, float]:
    """Is sendTransaction open here? Costs nothing: the signer has no SOL."""
    started = time.time()
    try:
        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import MessageV0
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction
    except Exception as exc:
        return "unknown", f"solders unavailable: {exc}", time.time() - started

    blockhash, err, _ = call(client, url, "getLatestBlockhash", [])
    if err:
        return "unknown", f"no blockhash: {err}", time.time() - started

    broke = Keypair()          # generated here, holds nothing, cannot pay a fee
    ix = transfer(TransferParams(from_pubkey=broke.pubkey(),
                                 to_pubkey=broke.pubkey(), lamports=1))
    msg = MessageV0.try_compile(
        broke.pubkey(), [ix], [],
        Hash.from_string(blockhash["value"]["blockhash"]))
    tx = VersionedTransaction(msg, [broke])
    encoded = base64.b64encode(bytes(tx)).decode()

    started = time.time()
    result, err, elapsed = call(client, url, "sendTransaction",
                                [encoded, {"encoding": "base64"}])
    if result:
        # Should be unreachable: an unfunded transaction cannot land.
        return "OPEN", f"accepted (unexpected) {str(result)[:40]}", elapsed
    low = err.lower()
    if any(m in low for m in PROCESSED):
        return "OPEN", "processed the send, rejected the unfunded tx", elapsed
    if any(m in low for m in REFUSED):
        return "CLOSED", err[:70], elapsed
    return "unclear", err[:70], elapsed


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

        sendable, detail, elapsed = can_send(client, url)
        print(f"  send       {sendable}  {detail} ({elapsed*1000:.0f}ms)")

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

    print("A usable endpoint answers a blockhash in well under a second,")
    print("survives the burst, and shows send OPEN. Reads alone are not enough:")
    print("an endpoint that reads fine but refuses sends leaves us holding a")
    print("position we cannot exit, which is worse than not entering at all.")


if __name__ == "__main__":
    main()
