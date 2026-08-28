"""Compare the live bot's ledgers against the paper lab: the Monday-night
verdict tool. Reads the live repo's signals.csv/trades.csv and the paper
ledger, prints what the detection overlap looks like and, once real trades
exist, how live results track paper expectations.

Usage: python -m src.compare_live LIVE_STATE_DIR [PAPER_LEDGER]
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as fh:
        return list(csv.DictReader(fh))


def signal_summary(signals: list[dict]) -> str:
    lines = ["## Live detection\n"]
    decisions = Counter(s["decision"] for s in signals)
    lines.append(f"Signals logged: {len(signals)} ({dict(decisions)})")
    ages = [float(s["age_hours"]) for s in signals if s.get("age_hours")]
    if ages:
        ages.sort()
        lines.append(
            f"Pool age at detection: median {60 * ages[len(ages) // 2]:.0f} min, "
            f"90th pct {60 * ages[int(len(ages) * 0.9)]:.0f} min"
        )
        lines.append(
            "The paper lab detected on a 2h tick; the live loop detects in "
            "minutes, so live entries are systematically earlier than the "
            "entries the edge was measured on. Watch whether that helps or hurts."
        )
    return "\n".join(lines)


def trade_summary(trades: list[dict]) -> str:
    lines = ["\n## Live trades vs paper expectation\n"]
    sells = [t for t in trades if t["action"] == "sell"]
    writeoffs = [t for t in trades if t["action"] == "writeoff"]
    buys = [t for t in trades if t["action"] == "buy"]
    failed = [t for t in trades if t["action"] == "buy_failed"]
    if not buys and not sells:
        lines.append("No live trades yet (dry-run only). This section fills in Monday.")
        return "\n".join(lines)
    lines.append(
        f"Buys: {len(buys)}, buy failures: {len(failed)}, sells: {len(sells)}, "
        f"rug write-offs: {len(writeoffs)}"
    )
    pnls = []
    for t in sells:
        note = t.get("note") or ""
        if "pnl_pct=" in note:
            pnls.append(float(note.split("pnl_pct=")[1]))
    closed = len(pnls) + len(writeoffs)
    if closed:
        total = sum(pnls) + -100.0 * len(writeoffs)
        lines.append(
            f"Closed positions: {closed}, mean net return {total / closed:+.1f}% "
            "(write-offs counted at -100%)."
        )
        by_reason = Counter(t.get("reason") or "?" for t in sells)
        lines.append(f"Sell reasons: {dict(by_reason)}")
        lines.append(
            "Compare that mean against the paper report's cell for the same "
            "group and strategy over the same days: the difference IS the "
            "paper-to-live gap (slippage, fees, failed exits)."
        )
    return "\n".join(lines)


def main() -> None:
    live_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "live_state")
    signals = read_csv(live_dir / "signals.csv")
    trades = read_csv(live_dir / "trades.csv")
    print(signal_summary(signals))
    print(trade_summary(trades))


if __name__ == "__main__":
    main()
