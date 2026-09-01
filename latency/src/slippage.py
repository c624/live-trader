"""What a round trip actually costs on a real, freshly launched token.

The backtest assumes a 3.2% round-trip cost against a +8.7% modelled edge, so
break-even sits at 12.6%. That 3.2% is an assumption, and it is now the single
number the whole strategy rests on -- the same position the "ten to twenty
seconds" figure was in before it turned out to be 0.17.

It does not need real money to test. Jupiter will quote a buy and the reverse
sell without executing either, so for a token that launched thirty seconds ago
we can ask: put 0.02 SOL in, how many tokens come out; put those tokens back,
how much SOL returns. What is missing on the way back is the round trip --
both price impacts plus the pool fee. Nothing is signed and nothing is sent.

There is a second question this answers, and it may matter more. A token this
young may not be routable on Jupiter at all. If a thirty-second-old launch has
no route, the strategy as built cannot execute regardless of edge or latency,
and that is worth learning before spending anything rather than after.

What this does NOT measure: the gap between a quote and a real fill. A live
fill can be worse, because the price moves between quoting and landing. So a
good number here is necessary but not sufficient, while a bad number here is
decisive -- if the quoted round trip already exceeds break-even, no execution
quality rescues it.

Usage: python -m src.slippage [minutes] [age_seconds]
"""

from __future__ import annotations

import statistics
import sys
import time

import httpx

sys.path.insert(0, "..")

QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
SOL = "So11111111111111111111111111111111111111112"
TICKET_LAMPORTS = 20_000_000     # ~$2, the pilot's ticket
SLIPPAGE_BPS = 500
BATCH = 12                       # quotes per cycle, to stay under rate limits
QUOTE_TIMEOUT = 8.0              # a stalled quote is a lost sample, not a reason to wait
GRACE_SECONDS = 120              # hard ceiling past the listening window
ASSUMED_ROUND_TRIP = 3.2         # what the backtest charges
BREAK_EVEN = 12.6                # where the +8.7% modelled edge is wiped out


# "We could not ask" is not "there is no route". The first run counted 78
# tokens as unroutable when many of those lines were HTTP 429 -- Jupiter rate
# limiting us -- which would have understated how tradeable launches are.
UNAVAILABLE = ("HTTP 429", "HTTP 5", "Timeout", "Connect", "Read", "not JSON")


def is_unavailable(err: str) -> bool:
    return any(marker in err for marker in UNAVAILABLE)


def quote(client: httpx.Client, src: str, dst: str, amount: int):
    try:
        r = client.get(QUOTE, params={"inputMint": src, "outputMint": dst,
                                      "amount": amount,
                                      "slippageBps": SLIPPAGE_BPS})
    except Exception as exc:
        return None, f"{type(exc).__name__}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        payload = r.json()
    except Exception:
        return None, "not JSON"
    if not payload.get("outAmount"):
        return None, str(payload.get("error") or payload)[:60]
    return payload, ""


def round_trip(client: httpx.Client, mint: str):
    """Cost of buying then immediately selling back, as a percentage."""
    buy, err = quote(client, SOL, mint, TICKET_LAMPORTS)
    if err:
        return None, f"no buy route: {err}", None
    tokens = int(buy["outAmount"])
    buy_impact = float(buy.get("priceImpactPct") or 0) * 100

    sell, err = quote(client, mint, SOL, tokens)
    if err:
        return None, f"buy only, no sell route: {err}", buy_impact
    back = int(sell["outAmount"])
    cost = (1 - back / TICKET_LAMPORTS) * 100
    return cost, "", buy_impact


def partition_ready(waiting, now, target_age, cap=BATCH):
    """Split the queue into what gets quoted now and what stays queued.

    Every row lands in exactly one of the two lists. An earlier version sliced
    the ready list and let the remainder fall on the floor, so tokens were
    never quoted and never counted -- a sample quietly smaller and more
    arrival-ordered than the totals claimed.
    """
    ready, still = [], []
    for row in waiting:
        (ready if now - row["first_trade_ts"] >= target_age else still).append(row)
    return ready[:cap], still + ready[cap:]


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 12
    target_age = float(sys.argv[2]) if len(sys.argv) > 2 else 30

    from live_trader.src.launchfeed import LaunchStream

    stream = LaunchStream()
    stream.start()
    print(f"listening {minutes:.0f} min; quoting each launch at ~{target_age:.0f}s old")

    deadline = time.time() + minutes * 60
    waiting: list[dict] = []
    costs: list[float] = []
    impacts: list[float] = []
    ages: list[float] = []
    no_route = 0
    unavailable = 0
    buy_only = 0
    quoted = 0

    # Without a ceiling the drain phase can outlast the listening window many
    # times over when quotes start timing out, and the run reports nothing at
    # all -- a measurement that never finishes is worth less than a partial one.
    hard_stop = deadline + GRACE_SECONDS

    with httpx.Client(timeout=QUOTE_TIMEOUT) as client:
        while time.time() < hard_stop and (time.time() < deadline or waiting):
            if time.time() < deadline:
                waiting.extend(stream.drain())

            # Quoting is rate limited, so only a batch goes per cycle; the
            # rest stay queued rather than being discarded.
            batch, waiting = partition_ready(waiting, time.time(), target_age)

            for row in batch:
                age = time.time() - row["first_trade_ts"]
                cost, err, impact = round_trip(client, row["token"])
                quoted += 1
                ages.append(age)
                if impact is not None:
                    impacts.append(impact)
                if cost is None:
                    if is_unavailable(err):
                        unavailable += 1
                    elif "buy only" in err:
                        buy_only += 1
                    else:
                        no_route += 1
                    print(f"  {row['symbol'][:12]:12} {age:5.0f}s  {err}", flush=True)
                else:
                    costs.append(cost)
                    print(f"  {row['symbol'][:12]:12} {age:5.0f}s  "
                          f"round trip {cost:6.2f}%  "
                          f"(buy impact {impact:.2f}%)", flush=True)

            if not batch:
                time.sleep(2)
            if time.time() >= deadline and not waiting:
                break

    if waiting:
        print(f"\nstopped at the {GRACE_SECONDS}s ceiling with {len(waiting)} "
              f"still queued; reporting what was priced")

    print(f"\n=== ROUND-TRIP COST AT ~{target_age:.0f}s OLD ===")
    if ages:
        ages.sort()
        # The age asked for is not necessarily the age measured: a backed-up
        # quote queue ages a token before its turn comes. Report what happened.
        print(f"age when quoted    median {statistics.median(ages):.0f}s  "
              f"(youngest {ages[0]:.0f}s, oldest {ages[-1]:.0f}s)")
    print(f"launches seen      {stream.launches}")
    print(f"quoted             {quoted}")
    print(f"no route at all    {no_route}")
    print(f"could not ask      {unavailable}   <- rate limited or errored, "
          f"not a statement about the token")
    print(f"buy but no sell    {buy_only}   <- would be an unsellable position")
    if not costs:
        print("\nNo token could be round-tripped. If that holds, the strategy")
        print("cannot execute at this age no matter what the edge is.")
        return

    costs.sort()
    median = statistics.median(costs)
    print(f"round trips priced {len(costs)}")
    print(f"  best  {costs[0]:.2f}%   median {median:.2f}%   "
          f"worst {costs[-1]:.2f}%")
    if impacts:
        print(f"  median buy-side price impact {statistics.median(impacts):.2f}%")
    print(f"\nbacktest assumes  {ASSUMED_ROUND_TRIP:.1f}%")
    print(f"break-even at     {BREAK_EVEN:.1f}%  (+8.7% modelled edge)")
    over = sum(1 for c in costs if c >= BREAK_EVEN)
    print(f"at or past break-even: {over}/{len(costs)} "
          f"({100*over/len(costs):.0f}%)")
    if median >= BREAK_EVEN:
        print("\nVERDICT: the quoted round trip alone eats the edge. Real fills")
        print("are worse than quotes, so this does not become profitable live.")
    elif median > ASSUMED_ROUND_TRIP:
        print(f"\nVERDICT: costlier than the backtest charges, but under")
        print(f"break-even. The modelled edge shrinks by about "
              f"{median - ASSUMED_ROUND_TRIP:.1f} points.")
    else:
        print("\nVERDICT: at or under the assumed cost. The backtest was not")
        print("flattering itself on this axis.")


if __name__ == "__main__":
    main()
