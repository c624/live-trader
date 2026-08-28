"""Solana wallet + RPC plumbing: sign Jupiter transactions, send via Helius,
confirm, and read balances."""

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


class Rpc:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        key = api_key or os.environ.get("HELIUS_API_KEY", "")
        self._url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        self._client = client or httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, params: list) -> dict | None:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(3):
            try:
                response = self._client.post(self._url, json=body)
            except httpx.HTTPError:
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if response.is_success:
                payload = response.json()
                if "error" in payload:
                    print(f"rpc error {method}: {payload['error']}")
                    return None
                return payload.get("result")
            time.sleep(1.5 * (attempt + 1))
        return None

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
