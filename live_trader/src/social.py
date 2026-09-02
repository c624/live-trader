"""Attention on X before it reaches the order book, on a fixed read budget.

Momentum and paid boosts both failed as signals, and both are attention that
has already arrived: buys are in the book, the boost is paid for. What was
never measured is conversation -- a token being mentioned by more people,
faster, before the price has finished moving. That is the last fast input.

The X API is pay-per-use in 2026 at roughly half a cent per post read, so a
hundred dollars is about twenty thousand reads and this cannot be a stream.
It is a sampled search: every so often, one query over the tokens already in
the buffer, capped at a hundred results, and a hard reads budget that stops
the client cold when it is spent. Running out of budget is reported, never
silently absorbed -- a token with no mention count because the budget was
gone must not read as a token nobody talks about.

Nothing is trusted. Every count is recorded on every entry and checked
against outcomes with a control from the same population, the same way
traction and boosts were, and both of those failed that check.
"""

from __future__ import annotations

import os
import re
import time

import httpx

SEARCH = "https://api.x.com/2/tweets/search/recent"
DEFAULT_BUDGET = 19_000        # post reads, under a $100 cap at $0.005 each
MAX_RESULTS = 100              # the endpoint's ceiling per request
QUERY_CHARS = 480              # under the endpoint's query length ceiling
CA = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
SOCIAL_FIELDS = ("mentions_15m", "mentions_1h", "authors_1h", "reach_1h")
UNKNOWN = {k: None for k in SOCIAL_FIELDS}


class Social:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None,
                 budget: int = DEFAULT_BUDGET, reads: int = 0):
        self.token = (token or os.environ.get("X_BEARER_TOKEN", "")).strip()
        self._client = client or httpx.Client(timeout=15.0)
        self.budget = budget
        # Reads carry over from the ledger. Each loop is a fresh process, and
        # a budget that reset every twenty-five minutes would be no budget.
        self.reads = int(reads or 0)
        self.calls = 0
        self.failures = 0
        self.exhausted = self.reads + MAX_RESULTS > self.budget
        # mint -> list of (ts, author_id, followers)
        self._mentions: dict[str, list[tuple[float, str, int]]] = {}
        # mint -> when it was last included in a search. A token that was
        # never searched has no mention count at all, as opposed to zero.
        self.polled: dict[str, float] = {}
        self._seen_posts: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ reading
    def _search(self, query: str, since_id: str | None = None) -> dict | None:
        if self.exhausted or not self.enabled:
            return None
        if self.reads + MAX_RESULTS > self.budget:
            # Stop before the request that would overrun, and say so once.
            self.exhausted = True
            print(f"social: read budget exhausted at {self.reads}", flush=True)
            return None
        params = {
            "query": query, "max_results": MAX_RESULTS,
            "tweet.fields": "created_at,author_id,public_metrics",
            "expansions": "author_id",
            "user.fields": "public_metrics",
        }
        if since_id:
            params["since_id"] = since_id
        self.calls += 1
        try:
            r = self._client.get(SEARCH, params=params,
                                 headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError:
            self.failures += 1
            return None
        if r.status_code != 200:
            self.failures += 1
            return None
        try:
            payload = r.json()
        except ValueError:
            self.failures += 1
            return None
        self.reads += len(payload.get("data") or [])
        return payload

    def choose(self, mints: list[str], now: float) -> list[str]:
        """Which of these tokens fit in one query: least recently searched
        first, then the order given, so a long buffer is covered in turns
        rather than the same ten tokens being read every time."""
        wanted = list(dict.fromkeys(m for m in mints if m))
        wanted.sort(key=lambda m: self.polled.get(m, 0.0))
        query, used = "", []
        for mint in wanted:
            piece = mint if not query else f" OR {mint}"
            if len(query) + len(piece) > QUERY_CHARS:
                break
            query += piece
            used.append(mint)
        return used

    def poll(self, mints: list[str], now: float | None = None) -> int:
        """One sampled search over these tokens. Returns mentions ingested."""
        now = time.time() if now is None else now
        if not mints or self.exhausted or not self.enabled:
            return 0
        # Contract addresses are unambiguous in a way ticker symbols are not:
        # "$DOG" matches a dozen tokens, a mint matches one.
        used = self.choose(mints, now)
        if not used:
            return 0
        payload = self._search(" OR ".join(used))
        if payload is None:
            return 0
        for mint in used:
            self.polled[mint] = now
        followers = {}
        for user in (payload.get("includes") or {}).get("users") or []:
            followers[user.get("id")] = int(
                (user.get("public_metrics") or {}).get("followers_count") or 0)
        count = 0
        for post in payload.get("data") or []:
            pid = str(post.get("id") or "")
            # The same post comes back on every search until it ages out of
            # the window; counted each time it would read as a new mention.
            if pid and pid in self._seen_posts:
                continue
            if pid:
                self._seen_posts.add(pid)
            text = post.get("text") or ""
            author = post.get("author_id") or ""
            for mint in set(CA.findall(text)):
                if mint in used:
                    self._mentions.setdefault(mint, []).append(
                        (now, author, followers.get(author, 0)))
                    count += 1
        self._prune(now)
        return count

    def _prune(self, now: float, keep_s: float = 7200.0) -> None:
        for mint in list(self._mentions):
            rows = [r for r in self._mentions[mint] if now - r[0] <= keep_s]
            if rows:
                self._mentions[mint] = rows
            else:
                del self._mentions[mint]
        for mint in list(self.polled):
            if now - self.polled[mint] > keep_s * 4:
                del self.polled[mint]
        if len(self._seen_posts) > 50_000:
            self._seen_posts.clear()

    # ----------------------------------------------------------- features
    def features(self, mint: str, now: float | None = None) -> dict:
        """Mention velocity for one token, or explicit unknowns.

        When the budget is spent, the client is disabled, or this token was
        never included in a search, the counts are None, not zero. Zero means
        nobody mentioned it; None means we could not look, and only one of
        those is a fact about the token.
        """
        if not self.enabled or self.exhausted or mint not in self.polled:
            return dict(UNKNOWN)
        now = time.time() if now is None else now
        rows = self._mentions.get(mint, [])
        m15 = [r for r in rows if now - r[0] <= 900]
        m1h = [r for r in rows if now - r[0] <= 3600]
        return {
            "mentions_15m": len(m15),
            "mentions_1h": len(m1h),
            "authors_1h": len({r[1] for r in m1h}),
            "reach_1h": sum(r[2] for r in m1h),
        }

    def summary(self) -> str:
        state = "exhausted" if self.exhausted else "ok"
        return (f"social: {self.calls} searches, {self.reads}/{self.budget} reads, "
                f"{self.failures} failures, {len(self.polled)} tokens searched, "
                f"{state}")
