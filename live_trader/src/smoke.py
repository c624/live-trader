"""Dress rehearsal: one tiny real round trip (SOL -> USDC -> SOL) through
the exact sign/send/confirm path the trader uses. Proves execution works
before Monday and measures a real round-trip cost on the deepest pool
(a lower bound for what thin memecoin pools will cost).
"""

from __future__ import annotations

import time

from .chain import Rpc, load_keypair
from .jupiter import SOL_MINT, USDC_MINT, Jupiter
from .notify import send

LAMPORTS = 1_000_000_000
TEST_LAMPORTS = int(0.03 * LAMPORTS)
SLIPPAGE_BPS = 100


def main() -> None:
    keypair = load_keypair()
    if keypair is None:
        raise SystemExit("TRADER_PRIVATE_KEY missing")
    rpc, jup = Rpc(), Jupiter()
    pubkey = str(keypair.pubkey())

    sol_price = jup.sol_usd()
    start = rpc.sol_balance_lamports(pubkey)
    if start is None or start < TEST_LAMPORTS + 10_000_000:
        raise SystemExit(f"balance too low for test: {start}")
    print(f"start: {start / LAMPORTS:.6f} SOL (SOL=${sol_price:.2f})")

    quote = jup.quote(SOL_MINT, USDC_MINT, TEST_LAMPORTS, SLIPPAGE_BPS)
    if not quote:
        raise SystemExit("no quote SOL->USDC")
    tx = jup.swap_transaction(quote, pubkey)
    signature = rpc.sign_and_send(tx, keypair) if tx else None
    if not signature:
        raise SystemExit("buy leg: send failed")
    print(f"buy leg sent: {signature}")
    if not rpc.confirm(signature, 120):
        raise SystemExit("buy leg: not confirmed")

    usdc_raw = rpc.wait_token_balance(pubkey, USDC_MINT)
    print(f"USDC received: {usdc_raw / 1e6:.4f}")
    if usdc_raw <= 0:
        raise SystemExit("no USDC after confirmed buy")

    signature2 = None
    for attempt in range(3):
        quote2 = jup.quote(USDC_MINT, SOL_MINT, usdc_raw, SLIPPAGE_BPS)
        if not quote2:
            raise SystemExit("no quote USDC->SOL")
        tx2 = jup.swap_transaction(quote2, pubkey)
        signature2 = rpc.sign_and_send(tx2, keypair) if tx2 else None
        if signature2:
            break
        time.sleep(5)
    if not signature2:
        raise SystemExit("sell leg: send failed after retries")
    print(f"sell leg sent: {signature2}")
    if not rpc.confirm(signature2, 120):
        raise SystemExit("sell leg: not confirmed")

    time.sleep(5)
    end = rpc.sol_balance_lamports(pubkey) or 0
    cost = start - end
    cost_pct = 100.0 * cost / TEST_LAMPORTS
    message = (
        f"Smoke test PASSED: {TEST_LAMPORTS / LAMPORTS:.3f} SOL round trip, "
        f"total cost {cost / LAMPORTS:.6f} SOL ({cost_pct:.2f}% of ticket incl. fees)"
    )
    print(message)
    send(message)


if __name__ == "__main__":
    main()
