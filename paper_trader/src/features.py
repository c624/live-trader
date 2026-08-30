"""Entry-time features, computed only from data that existed before the buy.

Every function here takes candles and an entry timestamp and looks strictly
backwards. Nothing may peek at a bar stamped at or after entry: a feature
that does is a filter that cannot be run live, and it will look brilliant in
the study and lose money on Monday.
"""

from __future__ import annotations

HOUR = 3600


def _before(rows: list[tuple], entry_ts: int) -> list[tuple]:
    return [r for r in rows if r[0] < entry_ts]


def entry_features(
    rows: list[tuple], entry_ts: int, reserve_usd: float | None
) -> dict[str, float | None]:
    """Features for one candidate buy.

    rows are (unix_seconds, open, high, low, close, volume_usd) oldest-first.
    Missing features come back as None rather than zero: 'no history' and
    'flat history' are different things and must not be averaged together.
    """
    pre = _before(rows, entry_ts)
    out: dict[str, float | None] = {
        "reserve_usd": reserve_usd,
        "pre_candles": float(len(pre)),
        "history_minutes": 0.0,
        "vol_60m_usd": None,
        "ret_60m_pct": None,
        "from_high_60m_pct": None,
        "turnover_60m": None,
    }
    if not pre:
        return out

    out["history_minutes"] = round((entry_ts - pre[0][0]) / 60.0, 1)

    window = [r for r in pre if r[0] >= entry_ts - HOUR]
    if not window:
        return out

    volume = sum(r[5] for r in window if len(r) > 5)
    out["vol_60m_usd"] = round(volume, 2)

    entry_price = pre[-1][4]
    first_price = window[0][1] or window[0][4]
    if entry_price and first_price:
        out["ret_60m_pct"] = round((entry_price / first_price - 1) * 100, 2)

    high = max(r[2] for r in window)
    if entry_price and high:
        # Zero means buying the high of the hour; -60 means well off it.
        out["from_high_60m_pct"] = round((entry_price / high - 1) * 100, 2)

    if reserve_usd:
        out["turnover_60m"] = round(volume / reserve_usd, 4)

    return out


FEATURE_NAMES = [
    "reserve_usd",
    "pre_candles",
    "history_minutes",
    "vol_60m_usd",
    "ret_60m_pct",
    "from_high_60m_pct",
    "turnover_60m",
]
