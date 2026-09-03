"""New launches from a feed that carries only launches.

The previous design subscribed to every log on the pump.fun program to find
the handful that were new tokens: roughly 38,000 messages a minute to extract
about thirty launches, and it exhausted a month of RPC credits in eight
minutes. Using a general-purpose firehose to watch for a specific event was
the mistake, not the plan tier - the same design would drain any free tier in
about an hour.

A launch stream sends one message per launch. The volume difference is about
a thousandfold, and it carries the mint directly, so no follow-up call is
needed to learn what was created. That removes the parse call per launch as
well, which is what earned the first HTTP 429.

The endpoint is configurable because the point is the shape, not the vendor:
anything that emits a JSON object per new token works, and if one goes away
another can be pointed at without touching the trading path.
"""

from __future__ import annotations

import json
import os
import threading
import time

DEFAULT_URL = "wss://pumpportal.fun/api/data"
SUBSCRIBE = {"method": "subscribeNewToken"}
MINT_KEYS = ("mint", "mintAddress", "token", "tokenAddress", "ca")
TIME_KEYS = ("timestamp", "created_timestamp", "createdAt", "blockTime")


def extract_mint(payload: dict) -> str | None:
    """The new token's address, whatever the feed happens to call it."""
    for key in MINT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and 32 <= len(value) <= 44:
            return value
    return None


def extract_created_ts(payload: dict, now: float) -> float:
    """When the token was created, in seconds. Milliseconds are common."""
    for key in TIME_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            seconds = value / 1000.0 if value > 1e11 else float(value)
            # A stamp from the future or the distant past is not a launch we
            # can reason about; treating it as "now" would let a stale row
            # pass an age filter it should fail.
            if 0 <= now - seconds <= 3600:
                return seconds
    return now


def extract_dev_buy_sol(payload: dict) -> float | None:
    """How much SOL the creator put in at launch, or None if the feed did
    not say.

    Public data on 830,000 launches puts the creator's own buy among the
    strongest predictors of a token leaving the bonding curve. The feed
    reports it directly as the SOL spent on the creating transaction; when
    only the curve's balance is given, the amount above the 30 SOL virtual
    reserve every curve starts with is the creator's money.
    """
    for key in ("solAmount", "sol_amount", "initialBuySol"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return round(float(value), 4)
    curve = payload.get("vSolInBondingCurve")
    if isinstance(curve, (int, float)) and curve >= 30:
        return round(float(curve) - 30.0, 4)
    return None


def extract_uri(payload: dict) -> str:
    for key in ("uri", "metadataUri", "metadata_uri"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


class LaunchStream:
    """Background subscriber exposing drain(); one message per launch."""

    def __init__(self, url: str | None = None, max_pending: int = 100):
        self.url = url or os.environ.get("LAUNCH_FEED_URL", DEFAULT_URL)
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._failures = 0
        self.messages = 0
        self.launches = 0
        self.errors = 0
        # Launches shed when the queue overflows. Reported at the end of a
        # run because a discovery path quietly throwing work away looks
        # exactly like a quiet market.
        self.dropped = 0
        # How many launches carried a metadata URI and a creator buy. A run
        # in which the metadata arms saw nothing at all is either a feed that
        # stopped sending these or a reader that stopped looking, and the
        # counts say which.
        self.with_uri = 0
        self.with_dev_buy = 0

    @property
    def healthy(self) -> bool:
        return self.messages > 0

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def drain(self) -> list[dict]:
        with self._lock:
            rows, self._pending = self._pending, []
        return rows

    def handle(self, raw: str, now: float | None = None) -> dict | None:
        """Parse one message into a row, or None if it is not a new launch."""
        now = time.time() if now is None else now
        self.messages += 1
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        mint = extract_mint(payload)
        if not mint or mint in self._seen:
            return None
        self._seen.add(mint)
        self.launches += 1
        row = {
            "token": mint,
            "symbol": payload.get("symbol") or payload.get("name") or "?",
            "pool": payload.get("pool") or payload.get("bondingCurveKey") or "",
            "first_trade_ts": extract_created_ts(payload, now),
            "detected_ts": now,
            # What the launch said about itself, for the arms that filter on
            # it. Missing is None, never zero.
            "dev_buy_sol": extract_dev_buy_sol(payload),
            "uri": extract_uri(payload),
        }
        self.with_uri += 1 if row["uri"] else 0
        self.with_dev_buy += 1 if row["dev_buy_sol"] is not None else 0
        with self._lock:
            if len(self._pending) >= self._max_pending:
                # Keep the newest: a queued launch goes stale in seconds.
                keep = self._max_pending // 2
                self.dropped += len(self._pending) - keep
                self._pending = self._pending[-keep:]
            self._pending.append(row)
        return row

    def _run(self) -> None:
        import websockets.sync.client as ws

        while not self._stop.is_set():
            try:
                with ws.connect(self.url, max_size=None) as socket:
                    socket.send(json.dumps(SUBSCRIBE))
                    self._failures = 0
                    while not self._stop.is_set():
                        self.handle(socket.recv(timeout=60))
            except Exception as exc:
                self.errors += 1
                self._failures += 1
                delay = min(60.0, 2.0 * (2 ** min(self._failures - 1, 5)))
                if self._failures <= 3 or self._failures % 10 == 0:
                    print(f"launch stream reconnect #{self._failures} in "
                          f"{delay:.0f}s after: {type(exc).__name__}: {exc}")
                time.sleep(delay)
