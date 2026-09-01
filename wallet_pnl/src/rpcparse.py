"""Read who bought what, from raw transactions on a keyless RPC.

The earlier wallet work went through Helius's parsed-transaction API, which
is no longer available and was never necessary: a transaction's own metadata
carries the balances before and after, and the difference is the trade. This
parses that directly, so wallet study runs on the same free endpoints as
everything else.

The rule for calling something a buy is deliberately strict. A wallet bought
a token in a transaction when its balance of that token went up and its SOL
went down in the same transaction. Requiring both sides rules out airdrops,
transfers in, and the token accounts that a routing program opens and closes
in passing -- an earlier version of this work counted those and produced
median trade sizes that were really account rent, which made a spray bot look
like a trader.
"""

from __future__ import annotations

LAMPORTS = 1_000_000_000


def _owner_sol_deltas(meta: dict, keys: list[str]) -> dict[str, int]:
    """Lamports gained or lost per account, from the transaction's own record."""
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    out: dict[str, int] = {}
    for i, key in enumerate(keys):
        if i < len(pre) and i < len(post):
            out[key] = post[i] - pre[i]
    return out


def _owner_token_deltas(meta: dict) -> dict[tuple[str, str], float]:
    """(owner, mint) -> change in token amount."""
    def index(rows):
        out = {}
        for row in rows or []:
            owner, mint = row.get("owner"), row.get("mint")
            amount = (row.get("uiTokenAmount") or {}).get("uiAmount")
            if owner and mint and amount is not None:
                out[(owner, mint)] = out.get((owner, mint), 0.0) + float(amount)
        return out

    before = index(meta.get("preTokenBalances"))
    after = index(meta.get("postTokenBalances"))
    deltas = {}
    for key in set(before) | set(after):
        change = after.get(key, 0.0) - before.get(key, 0.0)
        if change:
            deltas[key] = change
    return deltas


def account_keys(tx: dict) -> list[str]:
    """Every account the transaction touched, including loaded lookups."""
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = []
    for entry in message.get("accountKeys") or []:
        keys.append(entry.get("pubkey") if isinstance(entry, dict) else entry)
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    for group in ("writable", "readonly"):
        keys.extend(loaded.get(group) or [])
    return [k for k in keys if k]


def buys_in(tx: dict) -> list[dict]:
    """Every (wallet, mint) that gained tokens and spent SOL here."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        # A failed transaction moved nothing. Counting it would credit a
        # wallet with a position it never held.
        return []
    keys = account_keys(tx)
    sol = _owner_sol_deltas(meta, keys)
    out = []
    for (owner, mint), change in _owner_token_deltas(meta).items():
        if change <= 0:
            continue
        spent = -sol.get(owner, 0)
        if spent <= 0:
            continue
        out.append({
            "wallet": owner,
            "mint": mint,
            "tokens": change,
            "sol": spent / LAMPORTS,
            "ts": tx.get("blockTime"),
            "signature": (tx.get("transaction") or {}).get("signatures", [None])[0],
        })
    return out


def swaps_for(tx: dict, wallet: str) -> list[dict]:
    """Both directions of a wallet's trading against SOL in one transaction.

    A buy is tokens in with SOL out; a sale is tokens out with SOL in. Signs
    follow the ledger's convention: token_amount positive when acquired,
    sol_amount negative when spent. Legs where only one side moves are left
    out, which is what keeps airdrops, transfers and routing accounts from
    being priced as trades.
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    keys = account_keys(tx)
    sol_change = _owner_sol_deltas(meta, keys).get(wallet, 0) / LAMPORTS
    if sol_change == 0:
        return []

    out = []
    for (owner, mint), change in _owner_token_deltas(meta).items():
        if owner != wallet or change == 0:
            continue
        if (change > 0 and sol_change >= 0) or (change < 0 and sol_change <= 0):
            continue
        out.append({
            "ts": tx.get("blockTime"),
            "signature": (tx.get("transaction") or {}).get("signatures", [None])[0],
            "mint": mint,
            "token_amount": change,
            "sol_amount": sol_change,
        })
    return out
