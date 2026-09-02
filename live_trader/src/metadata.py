"""What a launch said about itself, read from its metadata.

Every pump.fun token is created with a metadata URI, usually a JSON file
on IPFS carrying the name, the image, and optionally a Telegram, X and
website link. The largest measured effect in the public literature on which
launches survive is that link: tokens launched with a Telegram present
graduate off the bonding curve about nine times as often as those without
(hazard ratio 5.4 in a multivariate model over 830,000 launches). Graduation
is not the same as a trade making money, so the flag is recorded and tested
against a control rather than believed.

The read is cheap but not free: one HTTP fetch per token, against gateways
that rate-limit. So the fetch is lazy -- only tokens an arm could act on are
read, oldest first so a token is known before it ages out of its window --
and capped per call. A token whose metadata could not be read has None in
every link field, not zero: "we could not look" must never pass as "no link".
"""

from __future__ import annotations

import re
import time

import httpx

META_FIELDS = ("has_telegram", "has_twitter", "has_website")
UNKNOWN = {k: None for k in META_FIELDS}
# Fetches per call. A check takes minutes once every arm has read the chain,
# so a launch gets about one check inside its entry window: whatever is not
# read on that check is never read. Twenty covered a fifth of the eligible
# rows; sixty at a third of a second each costs twenty seconds a check.
MAX_FETCH_PER_CALL = 60
TIMEOUT_S = 6.0
# Launch metadata is pinned on IPFS and the URI usually names the public
# ipfs.io gateway, which is slow and rate-limited: three of the first four
# reads failed there. The same content is served by the gateway the launch
# platform itself uses, so that is tried first and the original last.
GATEWAYS = ("https://pump.mypinata.cloud/ipfs/{cid}",)
IPFS_PATH = re.compile(r"/ipfs/([A-Za-z0-9]+)")
# A failed fetch is retried once after this long; gateways hiccup.
RETRY_AFTER_S = 45.0
LINK_KEYS = {
    "has_telegram": ("telegram",),
    "has_twitter": ("twitter", "x"),
    "has_website": ("website", "web"),
}


def candidates(uri: str) -> list[str]:
    """Where to read this URI from, fastest first, the URI itself last."""
    found = IPFS_PATH.search(uri or "")
    if not found:
        return [uri] if uri else []
    cid = found.group(1)
    urls = [g.format(cid=cid) for g in GATEWAYS]
    if uri not in urls:
        urls.append(uri)
    return urls


def links_from(meta: dict | None) -> dict:
    """Presence flags for each social link in a metadata document."""
    if not isinstance(meta, dict):
        return dict(UNKNOWN)
    out = {}
    for field, keys in LINK_KEYS.items():
        present = False
        for key in keys:
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                present = True
                break
        out[field] = 1 if present else 0
    return out


class Metadata:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=TIMEOUT_S,
                                              follow_redirects=True)
        # mint -> link flags (a read that succeeded)
        self._known: dict[str, dict] = {}
        # mint -> when the last failed fetch happened
        self._failed_at: dict[str, float] = {}
        self.calls = 0
        self.failures = 0

    def close(self) -> None:
        self._client.close()

    def fetch(self, uri: str) -> dict | None:
        """The metadata document, from the first gateway that serves it.
        One fetch is counted per token however many gateways were tried;
        a failure means none of them answered."""
        self.calls += 1
        for url in candidates(uri):
            payload = self._get(url)
            if payload is not None:
                return payload
        self.failures += 1
        return None

    def _get(self, url: str) -> dict | None:
        try:
            r = self._client.get(url)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            payload = r.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def features(self, mint: str) -> dict:
        return dict(self._known.get(mint) or UNKNOWN)

    def annotate(self, rows: list[dict], now: float | None = None,
                 limit: int = MAX_FETCH_PER_CALL) -> int:
        """Put link flags on every row; read metadata for up to `limit`
        rows that carry a URI and are not yet known. Returns fetches made.

        Rows are read oldest first: the oldest is the one nearest the end
        of its entry window, so it is the one whose answer is needed next.
        """
        now = time.time() if now is None else now
        pending = [r for r in rows
                   if r.get("token") and r.get("uri")
                   and r["token"] not in self._known
                   and now - self._failed_at.get(r["token"], -1e12) >= RETRY_AFTER_S]
        pending.sort(key=lambda r: float(r.get("first_trade_ts") or now))
        fetched = 0
        for row in pending[:limit]:
            mint = row["token"]
            meta = self.fetch(row["uri"])
            fetched += 1
            if meta is None:
                self._failed_at[mint] = now
                continue
            self._known[mint] = links_from(meta)
            self._failed_at.pop(mint, None)
        for row in rows:
            if row.get("token"):
                row.update(self.features(row["token"]))
        return fetched

    def summary(self) -> str:
        return (f"metadata: {self.calls} fetches, {self.failures} failures, "
                f"{len(self._known)} tokens known")
