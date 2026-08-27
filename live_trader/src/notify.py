"""Telegram notifications. Silent no-op when the secrets are missing, so the
bot runs identically with or without alerts wired up."""

from __future__ import annotations

import os

import httpx


def send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15.0,
        )
        return response.is_success
    except httpx.HTTPError:
        return False


def discover_chat_id() -> None:
    """One-time helper: message your bot on Telegram first, then run this to
    print the chat id to put in the TELEGRAM_CHAT_ID secret."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set")
        return
    response = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15.0)
    seen = set()
    for update in (response.json() or {}).get("result") or []:
        chat = ((update.get("message") or {}).get("chat")) or {}
        if chat.get("id") and chat["id"] not in seen:
            seen.add(chat["id"])
            print(f"chat_id={chat['id']} ({chat.get('first_name') or chat.get('title') or '?'})")
    if not seen:
        print("No messages found. Send your bot any message on Telegram, then rerun.")


if __name__ == "__main__":
    discover_chat_id()
