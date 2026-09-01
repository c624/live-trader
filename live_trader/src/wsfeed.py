"""A launch feed the trading loop can drain, fed by a websocket in the background.

Discovery was the whole problem. GeckoTerminal listed a pool 160 seconds after
its first trade, polling the signature index managed 25 and would not go
lower at any cadence, and a logs subscription delivers in 1. The edge is
+8.70% entering at a pool's first bar and -7.62% by sixty seconds, so this is
the difference between a strategy and a slow loss.

The loop that buys is synchronous and stays that way. This runs the socket on
its own thread and hands over completed rows through a lock, so nothing in the
trading path has to become async to benefit.

Launches announce themselves as a BondingCurveV3 instruction. That name was
read off the live stream's instruction histogram, not guessed: an earlier
attempt filtered for CREATE_POOL, which this program never emits, and found
nothing while concluding the market was quiet.
"""

from __future__ import annotations

import json
import threading
import time

import httpx

WS_URL = "wss://mainnet.helius-rpc.com/?api-key={key}"
PARSE_URL = "https://api.helius.xyz/v0/transactions?api-key={key}"
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LAUNCH_INSTRUCTIONS = ("BondingCurveV3",)
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


def instruction_names(logs: list[str]) -> list[str]:
    marker = "Instruction: "
    return [line.split(marker, 1)[1].strip() for line in logs if marker in line]


def looks_like_launch(logs: list[str]) -> bool:
    return any(name in LAUNCH_INSTRUCTIONS for name in instruction_names(logs))


def mint_from_parsed(tx: dict) -> str | None:
    """The token being launched: the non-quote mint the transaction moved.

    Picking the largest movement rather than the first keeps a routing hop or
    a fee transfer from being mistaken for the launch itself.
    """
    totals: dict[str, float] = {}
    for transfer in tx.get("tokenTransfers") or []:
        mint = transfer.get("mint")
        if not mint or mint == WSOL or mint in STABLES:
            continue
        try:
            amount = abs(float(transfer.get("tokenAmount") or 0))
        except (TypeError, ValueError):
            continue
        totals[mint] = totals.get(mint, 0.0) + amount
    if not totals:
        return None
    return max(totals, key=lambda m: totals[m])


class LaunchFeed:
    """Subscribes in the background; drain() returns launches seen since last call."""

    def __init__(
        self,
        api_key: str,
        max_pending: int = 200,
        resolve_per_second: float = 2.0,
        enough_pending: int = 8,
    ):
        self._key = api_key
        self._max_pending = max_pending
        # Resolving costs an API call and the same key quotes, signs, sends
        # and - the part that matters - exits. Spending it on discovery until
        # the provider throttles would leave open positions unsellable, which
        # is the one failure worth engineering against.
        self._min_gap = 1.0 / resolve_per_second if resolve_per_second else 0.0
        self._enough_pending = enough_pending
        self._last_resolve = 0.0
        self._backoff_until = 0.0
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.messages = 0
        self.launches = 0
        self.errors = 0
        self.throttled = 0
        self.skipped_busy = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def drain(self) -> list[dict]:
        with self._lock:
            rows, self._pending = self._pending, []
        return rows

    def _offer(self, row: dict) -> None:
        with self._lock:
            # A backlog means the loop is busy; the freshest launches are the
            # only ones still inside the window worth entering, so old ones go.
            if len(self._pending) >= self._max_pending:
                self._pending = self._pending[-(self._max_pending // 2):]
            self._pending.append(row)

    def _should_resolve(self, now_ts: float) -> bool:
        """Whether this transaction is worth an API call right now.

        BondingCurveV3 marks bonding-curve activity, not creation: it fires
        around 350 times a minute while roughly nine of those are genuinely
        new mints. Parsing all of them spent the key's budget discovering
        tokens already known and earned an HTTP 429 on the account the
        trading path depends on.
        """
        if now_ts < self._backoff_until:
            self.throttled += 1
            return False
        with self._lock:
            pending = len(self._pending)
        # The loop buys at most a couple of launches per pass. A queue deeper
        # than that is discovery nobody will act on before it goes stale.
        if pending >= self._enough_pending:
            self.skipped_busy += 1
            return False
        if now_ts - self._last_resolve < self._min_gap:
            self.skipped_busy += 1
            return False
        return True

    def _resolve(self, client: httpx.Client, signature: str) -> dict | None:
        self._last_resolve = time.time()
        try:
            response = client.post(
                PARSE_URL.format(key=self._key),
                json={"transactions": [signature]},
                timeout=10.0,
            )
            if response.status_code == 429:
                # Back off hard: the same credits are what sell a position.
                self._backoff_until = time.time() + 30
                self.throttled += 1
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            self.errors += 1
            return None
        if not isinstance(payload, list) or not payload:
            return None
        tx = payload[0]
        mint = mint_from_parsed(tx) if isinstance(tx, dict) else None
        if not mint or mint in self._seen:
            return None
        self._seen.add(mint)
        return {
            "token": mint,
            "symbol": "?",
            "pool": "",
            "signature": signature,
            "first_trade_ts": tx.get("timestamp"),
            "detected_ts": time.time(),
        }

    def _run(self) -> None:
        # Imported here so the trading loop can be imported without the
        # websocket dependency present, which keeps the offline tests offline.
        import websockets.sync.client as ws

        client = httpx.Client()
        while not self._stop.is_set():
            try:
                with ws.connect(WS_URL.format(key=self._key), max_size=None) as socket:
                    socket.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [PUMP_FUN]},
                                   {"commitment": "processed"}],
                    }))
                    while not self._stop.is_set():
                        raw = socket.recv(timeout=30)
                        self.messages += 1
                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            continue
                        value = ((payload.get("params") or {}).get("result")
                                 or {}).get("value") or {}
                        signature = value.get("signature")
                        if not signature or not looks_like_launch(value.get("logs") or []):
                            continue
                        if not self._should_resolve(time.time()):
                            continue
                        row = self._resolve(client, signature)
                        if row:
                            self.launches += 1
                            self._offer(row)
            except Exception as exc:  # a dropped socket must not stop trading
                self.errors += 1
                print(f"launch feed reconnecting after: {type(exc).__name__}: {exc}")
                time.sleep(2)
        client.close()
