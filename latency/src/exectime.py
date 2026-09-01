"""How long the trading path actually takes, rather than how long I assumed.

Every decision tonight leaned on "ten to twenty seconds to quote, sign and
land", a number never measured. It decides whether any of this works: with
one-second detection, five seconds of execution leaves comfortable margin
inside the window where the edge is +6.9% to +8.7%, while twenty-five seconds
puts entry past sixty where it is -7.6% and no RPC provider rescues it.

Quoting and building the swap are Jupiter, which needs no key, so most of the
path can be measured for nothing and without an account. Signing is local and
measured here too. Only the send needs an RPC, and its cost is reported
separately so the free part of the answer is available immediately.

Usage: python -m src.exectime [samples]
"""

from __future__ import annotations

import base64
import json
import sys
import time

import httpx

QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
SWAP = "https://lite-api.jup.ag/swap/v1/swap"
SOL = "So11111111111111111111111111111111111111112"
# A liquid token: the point is the round-trip time of the path, not the pair.
TARGET = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
TICKET_LAMPORTS = 20_000_000  # about $2 at $104/SOL, the pilot's ticket
PUBKEY = "DtFUr5giXgjMEFKD89xZfg4dw6gJ8bEGc2acQ4XunpEX"


def percentiles(values: list[float]) -> str:
    if not values:
        return "no samples"
    values = sorted(values)
    def at(p):
        return values[min(len(values) - 1, int(len(values) * p / 100))]
    return (f"n={len(values)} fastest {values[0]*1000:.0f}ms "
            f"median {at(50)*1000:.0f}ms p90 {at(90)*1000:.0f}ms "
            f"slowest {values[-1]*1000:.0f}ms")


def main() -> None:
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    quote_times: list[float] = []
    build_times: list[float] = []
    sign_times: list[float] = []
    whole: list[float] = []

    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        signer = Keypair()   # a throwaway: nothing is sent, only signed
        can_sign = True
    except Exception as exc:
        print(f"signing not measured ({exc!r})")
        can_sign = False
    sign_error = ""


    with httpx.Client(timeout=30.0) as client:
        for i in range(samples):
            started = time.time()
            t0 = time.time()
            try:
                r = client.get(QUOTE, params={
                    "inputMint": SOL, "outputMint": TARGET,
                    "amount": TICKET_LAMPORTS, "slippageBps": 500,
                })
                quote = r.json()
            except Exception as exc:
                print(f"  sample {i+1}: quote failed {type(exc).__name__}")
                continue
            quote_times.append(time.time() - t0)
            if "error" in quote or not quote.get("outAmount"):
                print(f"  sample {i+1}: no route ({str(quote)[:80]})")
                continue

            t1 = time.time()
            try:
                r = client.post(SWAP, json={
                    "quoteResponse": quote, "userPublicKey": PUBKEY,
                    "wrapAndUnwrapSol": True,
                })
                swap = r.json()
            except Exception as exc:
                print(f"  sample {i+1}: build failed {type(exc).__name__}")
                continue
            build_times.append(time.time() - t1)
            encoded = swap.get("swapTransaction")
            if not encoded:
                print(f"  sample {i+1}: no transaction ({str(swap)[:80]})")
                continue

            if can_sign:
                # Sign the message bytes directly. Handing the keypair to
                # VersionedTransaction instead raises "keypair-pubkey mismatch",
                # because the fee payer is the real wallet and this signer is a
                # throwaway -- an earlier version swallowed that and silently
                # reported no samples. The ed25519 signature is the real cost
                # either way; the wallet does not make it any slower.
                t2 = time.time()
                try:
                    raw = VersionedTransaction.from_bytes(base64.b64decode(encoded))
                    signer.sign_message(bytes(raw.message))
                    sign_times.append(time.time() - t2)
                except Exception as exc:
                    sign_error = f"{type(exc).__name__}: {exc}"

            whole.append(time.time() - started)
            print(f"  sample {i+1}: {whole[-1]*1000:.0f}ms total", flush=True)

    print("\n=== HOW LONG THE TRADING PATH TAKES ===")
    print(f"quote      {percentiles(quote_times)}")
    print(f"build swap {percentiles(build_times)}")
    print(f"sign       {percentiles(sign_times)}"
          + (f"  [failed: {sign_error}]" if sign_error and not sign_times else ""))
    print(f"QUOTE+BUILD+SIGN {percentiles(whole)}")
    if whole:
        median = sorted(whole)[len(whole) // 2]
        print(f"\nadd send-and-confirm on top of {median:.1f}s.")
        print("detection is about 1s, the edge is +6.9% at a 30s entry and")
        print("-7.6% at 60s, so this is the budget the send has to fit inside.")


if __name__ == "__main__":
    main()
