"""Do pumps and dumps differ at the trade level?

Every entry-time aggregate the bot could read -- buys per five minutes,
volume, price change, liquidity -- sits at the same value for the coins that
doubled and the coins that went to zero. Carter's hypothesis is that the
difference is visible one level down: inflated buying is a few wallets
dealing with themselves in bundled, same-sized trades, while organic buying
is many wallets arriving unevenly. The aggregates cannot tell those apart;
the transactions can.

This is a retrospective study on the ledger's own trades. For each hot
token (300+ buys in the five minutes before entry) with a known outcome, it
reads the transactions in the minutes before the entry from the chain and
measures how the buying was shaped. Analysis is split-sample: any cut is
chosen on the first half of the tokens by entry time and judged on the
second half, because a cut chosen on all of them will always look good on
all of them.

    python -m live_trader.src.washstudy collect state/paper out.csv [--max N]
    python -m live_trader.src.washstudy analyze out.csv

Free public RPC only: no key, no quota, no secret.
"""

from __future__ import annotations

import csv
import math
import random
import statistics
import sys
import time
from pathlib import Path

from .chain import Rpc

HOT_MIN_BUYS = 311
WINDOW_S = 300            # the five minutes before entry
MAX_PAGES = 40            # signature pages walked back from the present
SAMPLE_TX = 40            # transactions parsed per token
WALLET_CHECKS = 8         # buyers whose history is read per token
FRESH_TX = 10             # a buyer with fewer lifetime transactions is fresh
STUDY_ARMS = ("surge", "surgeliq", "liqctl", "launchctl", "wait120",
              "surgeliq1h", "liqctl1h", "surge1h")
FIELDS = ["mint", "arm", "entry_ts", "ret", "label", "m5_buys", "vol_m5_usd",
          "pages", "sig_n", "sig_fail_share", "slot_max_share", "slot_dense_share",
          "iat_cv", "parsed_n", "swap_n", "buy_n", "uniq_buyer_ratio",
          "top_buyer_share", "size_cv", "size_dup_share", "median_sol",
          "wallets_checked", "fresh_wallet_share", "note"]


# --------------------------------------------------------------- selection

def outcomes(state_dir: Path) -> list[dict]:
    """One row per hot mint: its earliest entry and the best it did within
    an hour of it. A coin that doubled for the one-hour arm and not for the
    ten-minute arm still pumped; a coin every arm rode to zero dumped."""
    trades = list(csv.DictReader(open(state_dir / "trades.csv")))
    feats = list(csv.DictReader(open(state_dir / "features.csv")))
    sells: dict[tuple, list] = {}
    for r in trades:
        if r.get("action") != "paper_sell":
            continue
        try:
            sells.setdefault((r["mint"], r["arm"]), []).append(float(r["usd_value"]) / 2 - 1)
        except (TypeError, ValueError):
            continue
    per_mint: dict[str, dict] = {}
    for f in feats:
        try:
            buys = float(f.get("m5_buys") or "")
        except ValueError:
            continue
        if buys < HOT_MIN_BUYS or f.get("arm") not in STUDY_ARMS:
            continue
        key = (f["mint"], f["arm"])
        if not sells.get(key):
            continue
        ret = sells[key].pop(0)
        ts = float(f["ts"])
        cur = per_mint.get(f["mint"])
        if cur is None:
            per_mint[f["mint"]] = {
                "mint": f["mint"], "arm": f["arm"], "entry_ts": ts, "ret": ret,
                "m5_buys": buys, "vol_m5_usd": f.get("vol_m5_usd") or "",
            }
            continue
        if ts < cur["entry_ts"]:
            cur.update({"arm": f["arm"], "entry_ts": ts, "m5_buys": buys,
                        "vol_m5_usd": f.get("vol_m5_usd") or ""})
        cur["ret"] = max(cur["ret"], ret)
    for row in per_mint.values():
        row["label"] = label(row["ret"])
    return sorted(per_mint.values(), key=lambda r: r["entry_ts"])


def label(ret: float) -> str:
    if ret >= 1.0:
        return "pump"
    if ret <= -0.9:
        return "dump"
    return "middle"


# ------------------------------------------------------------ measurement

def signatures_before(rpc: Rpc, mint: str, entry_ts: float,
                      window_s: int = WINDOW_S, max_pages: int = MAX_PAGES) -> tuple[list, int]:
    """Signatures touching the mint inside [entry - window, entry], walked
    back from the present one page at a time. Returns (rows, pages)."""
    rows: list = []
    before = None
    pages = 0
    while pages < max_pages:
        params: list = [mint, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        page = rpc._call("getSignaturesForAddress", params)
        pages += 1
        if not isinstance(page, list) or not page:
            break
        for r in page:
            bt = r.get("blockTime")
            if isinstance(bt, (int, float)) and entry_ts - window_s <= bt <= entry_ts:
                rows.append(r)
        oldest = page[-1].get("blockTime")
        before = page[-1].get("signature")
        if isinstance(oldest, (int, float)) and oldest < entry_ts - window_s:
            break
        if len(page) < 1000:
            break
    return rows, pages


def signature_shape(rows: list) -> dict:
    """What the signature list alone says: how many, how many failed, how
    tightly they cluster into slots, how uneven their arrival is."""
    n = len(rows)
    out = {"sig_n": n, "sig_fail_share": "", "slot_max_share": "", "slot_dense_share": "", "iat_cv": ""}
    if n == 0:
        return out
    out["sig_fail_share"] = round(sum(1 for r in rows if r.get("err") is not None) / n, 4)
    slots: dict[int, int] = {}
    for r in rows:
        s = r.get("slot")
        if isinstance(s, int):
            slots[s] = slots.get(s, 0) + 1
    if slots:
        out["slot_max_share"] = round(max(slots.values()) / n, 4)
        out["slot_dense_share"] = round(sum(c for c in slots.values() if c >= 4) / n, 4)
        ordered = sorted(slots)
        if len(ordered) >= 3:
            gaps = [b - a for a, b in zip(ordered, ordered[1:])]
            mean = statistics.fmean(gaps)
            if mean > 0:
                out["iat_cv"] = round(statistics.pstdev(gaps) / mean, 4)
    return out


def parse_swap(tx: dict, mint: str) -> dict | None:
    """The trader, direction and SOL paid of one transaction, or None if it
    did not move this mint for its fee payer."""
    try:
        meta = tx["meta"]
        keys = tx["transaction"]["message"]["accountKeys"]
        payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    except (KeyError, IndexError, TypeError):
        return None
    if meta.get("err") is not None:
        return None
    pre = {b.get("owner"): float(b["uiTokenAmount"]["uiAmount"] or 0)
           for b in meta.get("preTokenBalances") or [] if b.get("mint") == mint}
    post = {b.get("owner"): float(b["uiTokenAmount"]["uiAmount"] or 0)
            for b in meta.get("postTokenBalances") or [] if b.get("mint") == mint}
    delta = post.get(payer, 0.0) - pre.get(payer, 0.0)
    if delta == 0:
        return None
    try:
        lamports = meta["preBalances"][0] - meta["postBalances"][0] - meta.get("fee", 0)
    except (KeyError, IndexError, TypeError):
        lamports = 0
    return {"wallet": payer, "side": "buy" if delta > 0 else "sell",
            "tokens": abs(delta), "sol": round(abs(lamports) / 1e9, 6)}


def trade_shape(swaps: list[dict]) -> dict:
    """How the buying was distributed across wallets and sizes."""
    buys = [s for s in swaps if s["side"] == "buy"]
    out = {"swap_n": len(swaps), "buy_n": len(buys), "uniq_buyer_ratio": "",
           "top_buyer_share": "", "size_cv": "", "size_dup_share": "", "median_sol": ""}
    if not buys:
        return out
    wallets = [b["wallet"] for b in buys]
    counts: dict[str, int] = {}
    for w in wallets:
        counts[w] = counts.get(w, 0) + 1
    out["uniq_buyer_ratio"] = round(len(counts) / len(buys), 4)
    out["top_buyer_share"] = round(max(counts.values()) / len(buys), 4)
    sizes = [b["sol"] for b in buys if b["sol"] > 0]
    if len(sizes) >= 2:
        mean = statistics.fmean(sizes)
        out["size_cv"] = round(statistics.pstdev(sizes) / mean, 4) if mean > 0 else ""
        rounded = [round(s, 3) for s in sizes]
        seen: dict[float, int] = {}
        for s in rounded:
            seen[s] = seen.get(s, 0) + 1
        out["size_dup_share"] = round(sum(c for c in seen.values() if c >= 2) / len(rounded), 4)
    if sizes:
        out["median_sol"] = round(statistics.median(sizes), 6)
    return out


def measure(rpc: Rpc, row: dict, sample_tx: int = SAMPLE_TX,
            wallet_checks: int = WALLET_CHECKS, pause: float = 0.15,
            rng: random.Random | None = None) -> dict:
    rng = rng or random.Random(int(row["entry_ts"]))
    out = {k: row.get(k, "") for k in FIELDS}
    rows, pages = signatures_before(rpc, row["mint"], row["entry_ts"])
    out["pages"] = pages
    out.update(signature_shape(rows))
    if not rows:
        out["note"] = "no signatures in window" if pages < MAX_PAGES else "window beyond page cap"
        return out
    ok = [r for r in rows if r.get("err") is None]
    picked = rng.sample(ok, min(sample_tx, len(ok)))
    swaps = []
    for r in picked:
        tx = rpc._call("getTransaction", [r["signature"], {"encoding": "jsonParsed",
                                                            "maxSupportedTransactionVersion": 0}])
        time.sleep(pause)
        if isinstance(tx, dict):
            s = parse_swap(tx, row["mint"])
            if s:
                swaps.append(s)
    out["parsed_n"] = len(picked)
    out.update(trade_shape(swaps))
    buyers = []
    for s in swaps:
        if s["side"] == "buy" and s["wallet"] not in buyers:
            buyers.append(s["wallet"])
    checked = fresh = 0
    for w in buyers[:wallet_checks]:
        hist = rpc._call("getSignaturesForAddress", [w, {"limit": FRESH_TX}])
        time.sleep(pause)
        if isinstance(hist, list):
            checked += 1
            fresh += 1 if len(hist) < FRESH_TX else 0
    out["wallets_checked"] = checked
    out["fresh_wallet_share"] = round(fresh / checked, 4) if checked else ""
    return out


def collect(state_dir: Path, out_path: Path, max_tokens: int, rpc: Rpc | None = None) -> int:
    rpc = rpc or Rpc(api_key="", rpc_url="")
    rows = outcomes(state_dir)
    # Every pump and dump first (the question is about them), middles after.
    ordered = [r for r in rows if r["label"] != "middle"] + [r for r in rows if r["label"] == "middle"]
    ordered = ordered[:max_tokens]
    done = set()
    if out_path.exists():
        done = {r["mint"] for r in csv.DictReader(open(out_path))}
    new = out_path.exists() is False or out_path.stat().st_size == 0
    with open(out_path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        n = 0
        for r in ordered:
            if r["mint"] in done:
                continue
            t0 = time.time()
            try:
                m = measure(rpc, r)
            except Exception as exc:  # one bad token must not end the study
                m = {k: r.get(k, "") for k in FIELDS}
                m["note"] = f"error {type(exc).__name__}"
            w.writerow(m)
            fh.flush()
            n += 1
            print(f"{n:>4} {r['label']:<6} {r['mint'][:8]} pages={m.get('pages')} sig={m.get('sig_n')} "
                  f"buys={m.get('buy_n')} uniq={m.get('uniq_buyer_ratio')} fresh={m.get('fresh_wallet_share')} "
                  f"{time.time() - t0:.0f}s", flush=True)
    return n


# ---------------------------------------------------------------- analysis

METRICS = ["sig_n", "sig_fail_share", "slot_max_share", "slot_dense_share", "iat_cv",
           "uniq_buyer_ratio", "top_buyer_share", "size_cv", "size_dup_share",
           "median_sol", "fresh_wallet_share"]


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def best_cut(rows: list[dict], metric: str, min_keep: int = 20) -> tuple | None:
    """The threshold on one metric that most separates pumps from dumps,
    chosen on these rows only. Returns (direction, value, pump_rate, dump_rate)."""
    vals = [(_f(r[metric]), r["label"]) for r in rows if _f(r[metric]) is not None]
    if len(vals) < 2 * min_keep:
        return None
    vals.sort(key=lambda t: t[0])
    best = None
    for direction, seq in (("ge", vals[::-1]), ("le", vals)):
        kept = pumps = dumps = 0
        for v, lab in seq:
            kept += 1
            pumps += lab == "pump"
            dumps += lab == "dump"
            if kept < min_keep:
                continue
            score = pumps / kept - dumps / kept
            if best is None or score > best[0]:
                best = (score, direction, v, pumps / kept, dumps / kept)
    return best[1:] if best else None


def apply_cut(rows: list[dict], metric: str, direction: str, value: float) -> list[dict]:
    keep = []
    for r in rows:
        v = _f(r[metric])
        if v is None:
            continue
        if (direction == "ge" and v >= value) or (direction == "le" and v <= value):
            keep.append(r)
    return keep


def summarize(rows: list[dict]) -> str:
    n = len(rows)
    if n == 0:
        return "n=0"
    rets = [_f(r["ret"]) for r in rows if _f(r["ret"]) is not None]
    mean = statistics.fmean(rets) if rets else float("nan")
    ci = ""
    if len(rets) > 1:
        se = statistics.stdev(rets) / math.sqrt(len(rets))
        ci = f" [{(mean - 2 * se) * 100:+.0f},{(mean + 2 * se) * 100:+.0f}]"
    pumps = sum(1 for r in rows if r["label"] == "pump") / n
    dumps = sum(1 for r in rows if r["label"] == "dump") / n
    return f"n={n:<4} pump {pumps:5.1%} dump {dumps:5.1%} mean {mean * 100:+6.1f}%{ci}"


def analyze(path: Path, min_keep: int = 40) -> str:
    rows = [r for r in csv.DictReader(open(path)) if r.get("sig_n") not in ("", "0", None)]
    rows.sort(key=lambda r: float(r["entry_ts"]))
    counts = {lab: sum(1 for r in rows if r["label"] == lab) for lab in ("pump", "dump", "middle")}
    lines = [f"tokens measured: {len(rows)}  ({counts['pump']} pumps, {counts['dump']} dumps, "
             f"{counts['middle']} middle)", ""]
    lines.append(f"{'metric':<20}{'pumps med':>12}{'dumps med':>12}{'n':>6}")
    for m in METRICS:
        p = [_f(r[m]) for r in rows if r["label"] == "pump" and _f(r[m]) is not None]
        d = [_f(r[m]) for r in rows if r["label"] == "dump" and _f(r[m]) is not None]
        if p and d:
            lines.append(f"{m:<20}{statistics.median(p):>12.3g}{statistics.median(d):>12.3g}{len(p) + len(d):>6}")
    half = len(rows) // 2
    first, second = rows[:half], rows[half:]
    # The halves must hold the same mix of outcomes, or a cut is judged
    # against a different population than it was chosen on.
    mix = lambda rs: " ".join(f"{lab} {sum(1 for r in rs if r['label'] == lab)}" for lab in ("pump", "dump", "middle"))
    lines += ["", f"split-sample: cut chosen on first {len(first)} tokens ({mix(first)}), "
                  f"judged on last {len(second)} ({mix(second)})",
              f"  second half, no cut:  {summarize(second)}",
              "  a cut is judged by the second half it keeps against the second half it drops:"]
    for m in METRICS:
        cut = best_cut(first, m, min_keep=min_keep)
        if not cut:
            continue
        direction, value, pr, dr = cut
        kept = apply_cut(second, m, direction, value)
        dropped = [r for r in second if r not in kept and _f(r[m]) is not None]
        lines.append(f"  {m:<18} {direction} {value:<8.3g} first: pump {pr:5.1%} dump {dr:5.1%}")
        lines.append(f"      kept:    {summarize(kept)}")
        lines.append(f"      dropped: {summarize(dropped)}")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "collect":
        max_tokens = 400
        if "--max" in sys.argv:
            max_tokens = int(sys.argv[sys.argv.index("--max") + 1])
        n = collect(Path(sys.argv[2]), Path(sys.argv[3]), max_tokens)
        print(f"measured {n} tokens")
    elif cmd == "analyze":
        print(analyze(Path(sys.argv[2])))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
