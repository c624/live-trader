"""Solana wallet + RPC plumbing: sign Jupiter transactions, send them, confirm,
and read balances.

No API key is required. Free public endpoints backstop every configured one, so
the bot keeps its ability to sell even when a paid plan is exhausted mid-trade
-- the failure that would otherwise leave a position bought and unsellable."""

from __future__ import annotations

import base64
import os
import time

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction


def load_keypair() -> Keypair | None:
    """Phantom exports the private key as a base58 string; that string lives
    only in the TRADER_PRIVATE_KEY repo secret, never in code or chat."""
    raw = os.environ.get("TRADER_PRIVATE_KEY", "").strip()
    if not raw:
        return None
    return Keypair.from_base58_string(raw)


# Free, keyless endpoints, measured from a runner before being trusted here:
# both answer a blockhash in about a tenth of a second, survived a fifteen-call
# burst without a single refusal, and accepted a sendTransaction (rejecting an
# unfunded one, which is the proof the send path is open rather than throttled).
# They are the reason the bot needs no paid plan and no key at all.
FREE_ENDPOINTS = (
    ("publicnode", "https://solana-rpc.publicnode.com"),
    ("mainnet-beta", "https://api.mainnet-beta.solana.com"),
)


class Rpc:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None,
                 rpc_url: str | None = None):
        # Endpoints are tried in order, so one being exhausted or rate limited
        # is a slower call rather than a stuck position. SOLANA_RPC_URL takes a
        # complete endpoint from any provider and goes first; Helius follows if
        # a key is configured; the free endpoints always backstop both, which is
        # what keeps the bot able to sell when a paid plan runs out mid-trade.
        #
        # A configured URL carries its credential in the path. It is read from a
        # secret and never printed: failures are reported by endpoint label and
        # status code, never by request, which is why _label exists.
        self._endpoints: list[tuple[str, str]] = []
        override = (rpc_url or os.environ.get("SOLANA_RPC_URL", "")).strip()
        if override:
            self._endpoints.append(("configured", override))
        key = (api_key or os.environ.get("HELIUS_API_KEY", "")).strip()
        if key:
            self._endpoints.append(
                ("helius", f"https://mainnet.helius-rpc.com/?api-key={key}"))
        self._endpoints.extend(FREE_ENDPOINTS)
        self._client = client or httpx.Client(timeout=30.0)
        self.last_holders_error = ""

    @property
    def _url(self) -> str:
        """The endpoint currently in front. Kept for callers and tests."""
        return self._endpoints[0][1]

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, params: list) -> dict | None:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for label, url in self._endpoints:
            for attempt in range(3):
                try:
                    response = self._client.post(url, json=body)
                except httpx.HTTPError as exc:
                    print(f"rpc {label} unreachable ({type(exc).__name__})")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                if response.is_success:
                    payload = response.json()
                    if "error" in payload:
                        # A JSON-RPC error is the chain's answer, not a broken
                        # endpoint: moving to another one would only repeat it.
                        print(f"rpc error {method}: {payload['error']}")
                        return None
                    return payload.get("result")
                print(f"rpc {label} HTTP {response.status_code} on {method}")
                time.sleep(1.5 * (attempt + 1))
            if len(self._endpoints) > 1:
                print(f"rpc {label} exhausted on {method}, trying the next")
        return None

    def mint_info(self, mint: str) -> dict | None:
        """The mint account's parsed fields: authorities, supply, decimals."""
        result = self._call("getAccountInfo",
                            [mint, {"encoding": "jsonParsed"}])
        if not result:
            return None
        value = result.get("value") or {}
        data = value.get("data") or {}
        if not isinstance(data, dict):
            return None
        info = (data.get("parsed") or {}).get("info")
        return info if isinstance(info, dict) else None

    def largest_holders(self, mint: str) -> list[dict] | None:
        """Top token accounts by balance, for a concentration read.

        Returned empty for every token on the free endpoints, so the reason is
        surfaced rather than folded into "no holders": an unsupported method
        and a genuinely empty book are different facts, and only one of them
        says anything about the token.
        """
        result = self._call("getTokenLargestAccounts", [mint])
        if result is None:
            self.last_holders_error = "call_failed"
            return None
        value = result.get("value")
        if not isinstance(value, list):
            self.last_holders_error = "unexpected_shape"
            return None
        self.last_holders_error = "" if value else "empty"
        return value

    def sign_and_send(self, tx_b64: str, keypair: Keypair) -> str | None:
        """Sign Jupiter's unsigned transaction and submit. Returns signature."""
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(tx.message, [keypair])
        encoded = base64.b64encode(bytes(signed)).decode()
        # Preflight must simulate against confirmed state: at the default
        # (finalized) a sell submitted seconds after a confirmed buy fails
        # "insufficient funds" because the buy is not finalized yet.
        return self._call(
            "sendTransaction",
            [encoded, {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            }],
        )

    def confirm(self, signature: str, timeout_seconds: float = 90.0) -> bool:
        """True once the signature is confirmed and error-free."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self._call(
                "getSignatureStatuses", [[signature], {"searchTransactionHistory": False}]
            )
            statuses = (result or {}).get("value") or [None]
            status = statuses[0]
            if status:
                if status.get("err") is not None:
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            time.sleep(3.0)
        return False

    def sol_balance_lamports(self, pubkey: str) -> int | None:
        # "confirmed" commitment matches what confirm() waits for; the default
        # ("finalized") lags ~15-30s and reads stale balances after a swap.
        result = self._call("getBalance", [pubkey, {"commitment": "confirmed"}])
        try:
            return int(result["value"])
        except (TypeError, KeyError, ValueError):
            return None

    def token_balance_raw(self, owner: str, mint: str) -> int:
        """Sum of raw token units across the owner's accounts for this mint."""
        result = self._call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        total = 0
        for account in (result or {}).get("value") or []:
            try:
                info = account["account"]["data"]["parsed"]["info"]
                total += int(info["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                continue
        return total

    def wait_token_balance(self, owner: str, mint: str, timeout_seconds: float = 45.0) -> int:
        """Poll until tokens show up after a confirmed swap; 0 on timeout.

        Even at matching commitment, the node answering the balance query can
        briefly trail the one that confirmed the transaction.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            total = self.token_balance_raw(owner, mint)
            if total > 0 or time.monotonic() >= deadline:
                return total
            time.sleep(3.0)
