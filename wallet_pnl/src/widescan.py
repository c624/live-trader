"""The wider wallet scan: every coin that doubled in the record, who bought
it before it did, and whether any of those wallets is worth following.

The August study nominated wallets from 64 winners and found sprayers with
an edge under the cost of copying them. The paper record has since grown to
9,600 distinct coins with a measured outcome, 485 of which doubled from the
bot's own quoted entry, so the same question is asked again over eight
times the winners, with a control the first pass did not have: the coins
that lost 80% or more, read the same way. A wallet that is early in winners
and not in dumps is selecting; a wallet early in both is buying everything.

Every read is a free public RPC call. No key, no trades, no signing.

Pre-registered bar, fixed before the first run. A wallet passes when, on
its own most recent thousand transactions graded by the honest ledger with
open bags written to zero:
  * it bought at least 20 distinct tokens in that window,
  * at least 10% of them are winners from our record (hit rate),
  * its median hold on closed positions is five minutes or longer, and
  * its honest return on SOL deployed is +10% or better.
If no wallet passes, the copy-trade line is closed on the widest set of
winners the project can measure.

Steps (each resumable, each shardable for parallel jobs):
  tokens   state/paper out/tokens.json [--dumps N] [--gecko]
  collect  out/tokens.json out/buyers-K.json --shard K --shards N [--max M]
  grade    out/ out/grades-K.csv --shard K --shards N [--max M]
  report   out/
"""

from __future__ import annotations

import csv
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from live_trader.src.chain import Rpc                      # noqa: E402
from live_trader.src.gecko import Gecko, NETWORK           # noqa: E402
from wallet_pnl.src.ledger import Swap, build_report, split_by_time  # noqa: E402
from wallet_pnl.src.rpcparse import buys_in, swaps_for     # noqa: E402

TICKET_USD = 2.0          # every paper buy in the record
WIN_RET = 1.0             # doubled from our quoted entry
DUMP_RET = -0.8           # lost 80% or more
DUMP_SAMPLE = 300         # dumps read as the control
WINDOW_S = 1800           # how long before the run a buy counts as early
MAX_PAGES = 40            # signature pages walked back before giving up
SAMPLE_TX = 80            # transactions parsed per token, spread over the window
PAUSE = 0.12              # between calls on a free endpoint
MIN_WINNERS = 3           # early in this many distinct winners to be graded
GRADE_MAX = 40            # nominees graded, ranked by winners then selection
MAX_SIGNATURES = 1000     # a wallet's recent history, one page
GECKO_MIN_GAIN = 200.0    # a current runner is up this much on the day
GECKO_MIN_RESERVE = 20000.0

BAR = {"min_tokens": 20, "min_hit_pct": 10.0, "min_hold_min": 5.0, "min_harsh_pct": 10.0}

GRADE_FIELDS = ["wallet", "wins", "dumps", "selection", "tokens", "hit_pct",
                "win_rate_pct", "realized_pct", "harsh_pct", "hold_min",
                "per_day", "median_buy_sol", "observed_days",
                "harsh_first_half", "harsh_second_half", "swaps", "passes"]


# ----------------------------------------------------------------- tokens
def record_tokens(state_dir: Path, dumps: int = DUMP_SAMPLE, seed: int = 7) -> list[dict]:
    """Every coin in the paper record with its earliest entry and best
    return across arms, labelled win or dump; dumps sampled to size."""
    trades = list(csv.DictReader(open(Path(state_dir) / "trades.csv")))
    entry: dict[str, int] = {}
    best: dict[str, float] = {}
    for r in trades:
        mint = r.get("mint") or ""
        if not mint:
            continue
        if r.get("action") == "paper_buy":
            try:
                ts = int(float(r["ts"]))
            except (TypeError, ValueError):
                continue
            entry[mint] = min(entry.get(mint, ts), ts)
        elif r.get("action") == "paper_sell":
            try:
                ret = float(r["usd_value"]) / TICKET_USD - 1.0
            except (TypeError, ValueError):
                continue
            best[mint] = max(best.get(mint, -1.0), ret)
    wins, losers = [], []
    for mint, ret in best.items():
        if mint not in entry:
            continue
        row = {"mint": mint, "entry_ts": entry[mint], "ret": round(ret, 4), "source": "record"}
        if ret >= WIN_RET:
            row["label"] = "win"
            wins.append(row)
        elif ret <= DUMP_RET:
            row["label"] = "dump"
            losers.append(row)
    random.Random(seed).shuffle(losers)
    out = wins + losers[:dumps]
    out.sort(key=lambda r: r["entry_ts"])
    return out


def run_start(candles: list, min_gain_pct: float = GECKO_MIN_GAIN) -> int | None:
    """When a runner's run began, from hourly candles (any order): the first
    candle of the last day whose high reached the day's gain over its open."""
    rows = sorted((c for c in candles if len(c) >= 5), key=lambda c: c[0])
    if not rows:
        return None
    rows = rows[-24:]
    base = float(rows[0][1])
    if base <= 0:
        return None
    target = base * (1 + min_gain_pct / 100.0)
    for c in rows:
        if float(c[2]) >= target:
            return int(c[0])
    peak = max(rows, key=lambda c: float(c[2]))
    return int(peak[0])


def gecko_runners(gecko: Gecko | None = None, pages: int = 5) -> list[dict]:
    """Coins up 200%+ on the day right now, from the trending and busiest
    pools, with the hour their run began. A supplement to the record: the
    free feeds have no history, so this only ever sees today's winners."""
    g = gecko or Gecko()
    seen: set[str] = set()
    out: list[dict] = []
    for kind, params in (("trending_pools", {}), ("pools", {"sort": "h24_volume_usd_desc"})):
        for page in range(1, pages + 1):
            payload = g._get(f"/networks/{NETWORK}/{kind}", {"page": page, **params})
            if not payload:
                break
            for pool in payload.get("data") or []:
                attrs = pool.get("attributes") or {}
                change = _f((attrs.get("price_change_percentage") or {}).get("h24"))
                reserve = _f(attrs.get("reserve_in_usd"))
                if change is None or change < GECKO_MIN_GAIN or (reserve or 0) < GECKO_MIN_RESERVE:
                    continue
                rel = pool.get("relationships") or {}
                base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                mint = base.split("_", 1)[-1] if base else ""
                address = attrs.get("address") or ""
                if not mint or not address or mint in seen:
                    continue
                seen.add(mint)
                ohlcv = g._get(f"/networks/{NETWORK}/pools/{address}/ohlcv/hour", {"limit": 48})
                candles = (((ohlcv or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                start = run_start(candles)
                if start is None:
                    continue
                out.append({"mint": mint, "entry_ts": start, "ret": round(change / 100.0, 4),
                            "source": "gecko", "label": "win"})
    return out


# ---------------------------------------------------------------- collect
def window_signatures(rpc: Rpc, mint: str, end_ts: int, window_s: int = WINDOW_S,
                      max_pages: int = MAX_PAGES) -> tuple[list, int, bool]:
    """Signatures on the mint inside [end - window, end], walked back from
    the present. The flag says whether the walk reached the window's start;
    a token too busy for the page budget is reported, never sampled from
    the middle of its later life."""
    rows: list = []
    before = None
    pages = 0
    reached = False
    while pages < max_pages:
        params: list = [mint, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        page = rpc._call("getSignaturesForAddress", params)
        pages += 1
        time.sleep(PAUSE)
        if not isinstance(page, list) or not page:
            reached = True
            break
        for r in page:
            bt = r.get("blockTime")
            if isinstance(bt, (int, float)) and end_ts - window_s <= bt <= end_ts and r.get("err") is None:
                rows.append(r)
        oldest = page[-1].get("blockTime")
        before = page[-1].get("signature")
        if isinstance(oldest, (int, float)) and oldest < end_ts - window_s:
            reached = True
            break
        if len(page) < 1000:
            reached = True
            break
    return rows, pages, reached


def spaced(rows: list, n: int) -> list:
    """n rows spread evenly over the window, oldest first."""
    rows = sorted(rows, key=lambda r: r.get("blockTime") or 0)
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def early_buyers(rpc: Rpc, token: dict) -> dict:
    sigs, pages, reached = window_signatures(rpc, token["mint"], int(token["entry_ts"]))
    out = {"label": token["label"], "source": token["source"], "entry_ts": token["entry_ts"],
           "ret": token.get("ret"), "pages": pages, "reached": reached,
           "in_window": len(sigs), "sampled": 0, "buyers": []}
    if not reached:
        return out
    for row in spaced(sigs, SAMPLE_TX):
        tx = rpc._call("getTransaction",
                       [row["signature"], {"encoding": "jsonParsed",
                                           "maxSupportedTransactionVersion": 0}])
        time.sleep(PAUSE)
        if not tx:
            continue
        out["sampled"] += 1
        for buy in buys_in(tx):
            if buy["mint"] == token["mint"]:
                out["buyers"].append({"wallet": buy["wallet"], "sol": round(buy["sol"], 6),
                                      "before_s": int(token["entry_ts"]) - int(buy["ts"] or token["entry_ts"])})
    return out


def collect(tokens_path: Path, out_path: Path, shard: int = 0, shards: int = 1,
            max_tokens: int | None = None, rpc: Rpc | None = None) -> dict:
    tokens = json.loads(Path(tokens_path).read_text())
    mine = [t for i, t in enumerate(tokens) if i % shards == shard]
    done: dict = {}
    if Path(out_path).exists():
        done = json.loads(Path(out_path).read_text())
    todo = [t for t in mine if t["mint"] not in done]
    if max_tokens is not None:
        todo = todo[:max_tokens]
    print(f"shard {shard}/{shards}: {len(mine)} tokens, {len(done)} done, {len(todo)} to read", flush=True)
    rpc = rpc or Rpc(api_key="", rpc_url="")
    for i, token in enumerate(todo, 1):
        try:
            result = early_buyers(rpc, token)
        except Exception as exc:  # a broken read is a skipped token, not a dead run
            print(f"[{i}/{len(todo)}] {token['mint'][:10]} error {type(exc).__name__}: {exc}", flush=True)
            continue
        done[token["mint"]] = result
        Path(out_path).write_text(json.dumps(done))
        print(f"[{i}/{len(todo)}] {token['mint'][:10]} {token['label']:4} "
              f"{result['pages']:2d} pages {'ok ' if result['reached'] else 'SKIP'} "
              f"{result['in_window']:5d} in window, {len(result['buyers'])} buys", flush=True)
    return done


# -------------------------------------------------------------- nominate
def load_buyers(out_dir: Path) -> dict:
    merged: dict = {}
    for p in sorted(Path(out_dir).glob("buyers-*.json")):
        merged.update(json.loads(p.read_text()))
    return merged


def nominate(buyers: dict, min_winners: int = MIN_WINNERS) -> list[dict]:
    """Wallets early in enough distinct winners, with their dump count as
    the control, ranked by winners then by how selective they were."""
    wins: dict[str, set] = {}
    dumps: dict[str, set] = {}
    sol: dict[str, float] = {}
    for mint, row in buyers.items():
        for b in row.get("buyers") or []:
            w = b["wallet"]
            (wins if row["label"] == "win" else dumps).setdefault(w, set()).add(mint)
            sol[w] = sol.get(w, 0.0) + float(b.get("sol") or 0)
    out = []
    for w, mints in wins.items():
        if len(mints) < min_winners:
            continue
        d = len(dumps.get(w, ()))
        out.append({"wallet": w, "wins": len(mints), "dumps": d,
                    "selection": round(len(mints) / (len(mints) + d), 3),
                    "sol": round(sol[w], 3)})
    out.sort(key=lambda r: (-r["wins"], -r["selection"], -r["sol"]))
    return out


def chance_selection(buyers: dict) -> float | None:
    """What a wallet that buys everything would score: winners read over
    all tokens read. A wallet has to beat this to be selecting at all."""
    read = [r for r in buyers.values() if r.get("reached")]
    if not read:
        return None
    return round(sum(1 for r in read if r["label"] == "win") / len(read), 3)


# ------------------------------------------------------------------ grade
def wallet_swaps(rpc: Rpc, wallet: str) -> list[Swap]:
    rows = rpc._call("getSignaturesForAddress", [wallet, {"limit": MAX_SIGNATURES}])
    if not isinstance(rows, list):
        return []
    swaps: list[Swap] = []
    for row in rows:
        sig = row.get("signature")
        if not sig or row.get("err"):
            continue
        tx = rpc._call("getTransaction",
                       [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        time.sleep(PAUSE)
        if not tx:
            continue
        for leg in swaps_for(tx, wallet):
            if leg["ts"]:
                swaps.append(Swap(ts=leg["ts"], signature=leg["signature"], mint=leg["mint"],
                                  token_amount=leg["token_amount"], sol_amount=leg["sol_amount"]))
    return swaps


def passes(row: dict, bar: dict = BAR) -> bool:
    return (row["tokens"] >= bar["min_tokens"] and row["hit_pct"] >= bar["min_hit_pct"]
            and row["hold_min"] >= bar["min_hold_min"] and row["harsh_pct"] >= bar["min_harsh_pct"])


def grade_wallet(rpc: Rpc, nominee: dict, swaps: list[Swap] | None = None) -> dict:
    swaps = wallet_swaps(rpc, nominee["wallet"]) if swaps is None else swaps
    report = build_report(nominee["wallet"], swaps)
    tokens = len(report.scored)
    row = {"wallet": nominee["wallet"], "wins": nominee["wins"], "dumps": nominee["dumps"],
           "selection": nominee["selection"], "tokens": tokens,
           "hit_pct": round(100 * nominee["wins"] / tokens, 1) if tokens else 0.0,
           "win_rate_pct": round(report.win_rate_pct, 1),
           "realized_pct": round(report.realized_pct, 1),
           "harsh_pct": round(report.harsh_pct, 1),
           "hold_min": round(report.median_hold_minutes, 1),
           "per_day": round(tokens / max(report.observed_days, 0.01), 1),
           "median_buy_sol": round(report.median_buy_sol, 4),
           "observed_days": round(report.observed_days, 2),
           "harsh_first_half": "", "harsh_second_half": "", "swaps": len(swaps)}
    if swaps and report.last_ts > report.first_ts:
        first, second = split_by_time(swaps, (report.first_ts + report.last_ts) // 2)
        for key, part in (("harsh_first_half", first), ("harsh_second_half", second)):
            r = build_report(nominee["wallet"], part)
            row[key] = round(r.harsh_pct, 1) if r.sol_deployed else ""
    row["passes"] = "yes" if passes(row) else ""
    return row


def grade(out_dir: Path, out_csv: Path, shard: int = 0, shards: int = 1,
          max_wallets: int = GRADE_MAX, rpc: Rpc | None = None) -> list[dict]:
    nominees = nominate(load_buyers(out_dir))[:max_wallets]
    mine = [n for i, n in enumerate(nominees) if i % shards == shard]
    done: dict[str, dict] = {}
    if Path(out_csv).exists():
        done = {r["wallet"]: r for r in csv.DictReader(open(out_csv))}
    print(f"shard {shard}/{shards}: {len(nominees)} nominees, {len(mine)} mine, {len(done)} graded", flush=True)
    rpc = rpc or Rpc(api_key="", rpc_url="")
    for n in mine:
        if n["wallet"] in done:
            continue
        try:
            row = grade_wallet(rpc, n)
        except Exception as exc:
            print(f"{n['wallet'][:10]} error {type(exc).__name__}: {exc}", flush=True)
            continue
        done[n["wallet"]] = row
        with open(out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=GRADE_FIELDS)
            w.writeheader()
            w.writerows(done.values())
        print(f"{row['wallet'][:10]} wins {row['wins']} dumps {row['dumps']} tokens {row['tokens']} "
              f"hit {row['hit_pct']}% harsh {row['harsh_pct']:+}% hold {row['hold_min']}m "
              f"{'PASS' if row['passes'] else ''}", flush=True)
    return list(done.values())


# ----------------------------------------------------------------- report
def report(out_dir: Path) -> str:
    buyers = load_buyers(out_dir)
    lines = []
    read = [r for r in buyers.values() if r.get("reached")]
    skipped = [r for r in buyers.values() if not r.get("reached")]
    wins = [r for r in read if r["label"] == "win"]
    dumps = [r for r in read if r["label"] == "dump"]
    lines.append(f"tokens read: {len(read)} ({len(wins)} winners, {len(dumps)} dumps), "
                 f"{len(skipped)} skipped for exceeding {MAX_PAGES} signature pages")
    by_src: dict[str, int] = {}
    for r in read:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    lines.append(f"sources: {by_src}")
    n_buys = sum(len(r["buyers"]) for r in read)
    wallets = {b["wallet"] for r in read for b in r["buyers"]}
    lines.append(f"early buys parsed: {n_buys} by {len(wallets)} wallets")
    nominees = nominate(buyers)
    chance = chance_selection(buyers)
    lines.append(f"wallets early in {MIN_WINNERS}+ distinct winners: {len(nominees)}; "
                 f"selection of a wallet that buys everything: {chance}")
    grades: list[dict] = []
    for p in sorted(Path(out_dir).glob("grades-*.csv")):
        grades.extend(csv.DictReader(open(p)))
    for g in grades:
        for k in ("wins", "dumps", "tokens", "swaps"):
            g[k] = int(float(g[k]))
        for k in ("selection", "hit_pct", "win_rate_pct", "realized_pct", "harsh_pct",
                  "hold_min", "per_day", "median_buy_sol", "observed_days"):
            g[k] = float(g[k])
    grades.sort(key=lambda g: (-g["wins"], -g["selection"]))
    lines.append("")
    lines.append(f"graded: {len(grades)} of {len(nominees)} nominees")
    lines.append(f"bar: tokens >= {BAR['min_tokens']}, hit >= {BAR['min_hit_pct']}%, "
                 f"hold >= {BAR['min_hold_min']} min, honest >= +{BAR['min_harsh_pct']}%")
    lines.append("")
    lines.append(f"{'wallet':10} {'wins':>4} {'dumps':>5} {'sel':>5} {'tokens':>6} {'hit%':>5} "
                 f"{'win%':>5} {'real%':>7} {'honest%':>8} {'hold_m':>6} {'/day':>6} "
                 f"{'buy_sol':>7} {'days':>5} {'h1%':>7} {'h2%':>7} pass")
    for g in grades:
        lines.append(f"{g['wallet'][:10]:10} {g['wins']:4d} {g['dumps']:5d} {g['selection']:5.2f} "
                     f"{g['tokens']:6d} {g['hit_pct']:5.1f} {g['win_rate_pct']:5.1f} "
                     f"{g['realized_pct']:+7.1f} {g['harsh_pct']:+8.1f} {g['hold_min']:6.1f} "
                     f"{g['per_day']:6.1f} {g['median_buy_sol']:7.3f} {g['observed_days']:5.2f} "
                     f"{_pct(g['harsh_first_half']):>7} {_pct(g['harsh_second_half']):>7} "
                     f"{g['passes'] or '-'}")
    passed = [g for g in grades if g["passes"]]
    lines.append("")
    if grades and not passed:
        lines.append("VERDICT: no graded wallet passes the pre-registered bar")
    elif passed:
        lines.append(f"VERDICT: {len(passed)} wallet(s) pass the bar: "
                     + ", ".join(g["wallet"] for g in passed))
    else:
        lines.append("VERDICT: nothing graded yet")
    return "\n".join(lines)


def _pct(v) -> str:
    try:
        return f"{float(v):+.1f}"
    except (TypeError, ValueError):
        return "-"


def _f(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------------- cli
def _opt(args: list[str], name: str, default=None):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _positional(args: list[str]) -> list[str]:
    """Arguments that are neither an option nor an option's value."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("--"):
            skip = "=" not in a and a not in ("--gecko",)
        else:
            out.append(a)
    return out


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit(__doc__)
    cmd, rest = args[0], args[1:]
    pos = _positional(rest)
    if cmd == "tokens":
        state_dir, out = Path(pos[0]), Path(pos[1])
        tokens = record_tokens(state_dir, int(_opt(rest, "--dumps", DUMP_SAMPLE)))
        wins = sum(1 for t in tokens if t["label"] == "win")
        print(f"record: {wins} winners, {len(tokens) - wins} dumps")
        if "--gecko" in rest:
            known = {t["mint"] for t in tokens}
            extra = [t for t in gecko_runners() if t["mint"] not in known]
            print(f"gecko: {len(extra)} current runners added")
            tokens.extend(extra)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(tokens, indent=0))
        print(f"wrote {out} ({len(tokens)} tokens)")
    elif cmd == "collect":
        maxt = _opt(rest, "--max")
        collect(Path(pos[0]), Path(pos[1]), int(_opt(rest, "--shard", 0)),
                int(_opt(rest, "--shards", 1)), int(maxt) if maxt else None)
    elif cmd == "grade":
        grade(Path(pos[0]), Path(pos[1]), int(_opt(rest, "--shard", 0)),
              int(_opt(rest, "--shards", 1)), int(_opt(rest, "--max", GRADE_MAX)))
    elif cmd == "report":
        print(report(Path(pos[0])))
    else:
        raise SystemExit(f"unknown command {cmd}\n{__doc__}")


if __name__ == "__main__":
    main()
