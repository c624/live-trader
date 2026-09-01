"""Ledger for the live trader: positions JSON plus append-only CSV logs.

Everything lives in STATE_DIR, which the workflow maps to the live-state
branch. The CSVs are the record Monday's paper-vs-live verdict reads, so
rows are appended and never edited.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

TRADE_FIELDS = [
    "ts", "action", "mint", "symbol", "pool", "sol_lamports", "token_raw",
    "usd_value", "gecko_price_usd", "reason", "signature", "note", "arm",
]
SIGNAL_FIELDS = [
    "ts", "mint", "symbol", "pool", "age_hours", "reserve_usd",
    "gecko_price_usd", "decision", "reason",
]


def state_dir() -> Path:
    path = Path(os.environ.get("STATE_DIR", "state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def use_paper_state() -> Path:
    """Point this process's ledger at a paper subdirectory.

    Paper and live must never share a ledger: simulated fills would feed the
    loss cap and daily spend cap that govern real money, and paper positions
    left in live state would be treated as real holdings if the bot were
    armed afterwards. Redirecting the directory makes the mixture impossible
    instead of relying on every reader to check a flag.
    """
    path = state_dir() / "paper"
    path.mkdir(parents=True, exist_ok=True)
    os.environ["STATE_DIR"] = str(path)
    return path


def load_state() -> dict:
    path = state_dir() / "live_state.json"
    if path.exists():
        with open(path) as fh:
            return json.load(fh)
    return {"positions": [], "seen": {}, "daily_spend": {}}


def save_state(state: dict) -> None:
    path = state_dir() / "live_state.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    tmp.replace(path)


def _append(name: str, fields: list[str], row: dict) -> None:
    path = state_dir() / name
    exists = path.exists()
    if exists:
        # A changed schema must not be appended under the old header: the
        # columns would silently shift and every later reader would
        # misattribute values. Keep the old file, start a new one.
        with open(path) as fh:
            header = (fh.readline().strip().split(",") if fh else [])
        if header and header != fields:
            path.rename(path.with_name(f"{path.stem}_v1{path.suffix}"))
            exists = False
    with open(path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def log_trade(row: dict) -> None:
    _append("trades.csv", TRADE_FIELDS, row)


def log_signal(row: dict) -> None:
    _append("signals.csv", SIGNAL_FIELDS, row)
