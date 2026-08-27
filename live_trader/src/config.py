"""Config loading for the live trader. One JSON file holds every knob."""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as fh:
        cfg = json.load(fh)
    # DRY_RUN=1 in the environment beats the file, so a dispatch run can
    # never accidentally trade while we are still testing plumbing.
    if os.environ.get("DRY_RUN"):
        cfg["trading_enabled"] = False
    return cfg


def kill_switch(repo_root: Path | None = None) -> str | None:
    """KILL stops new buys; KILL_ALL stops sells too. Files, so Carter can
    flip them from the GitHub mobile app without touching code."""
    root = repo_root or Path(__file__).resolve().parents[2]
    if (root / "KILL_ALL").exists():
        return "KILL_ALL"
    if (root / "KILL").exists():
        return "KILL"
    return None
