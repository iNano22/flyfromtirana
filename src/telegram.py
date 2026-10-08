"""Sends posts to the Telegram channel through the Bot API.

The bot must be an admin of the channel with permission to post messages.
Docs: https://core.telegram.org/bots/api#sendmessage
"""
from __future__ import annotations

import requests

from src.http_client import request_with_retry

API_URL = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    """Telegram refused the message or couldn't be reached."""


class TelegramClient:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id  # "@flyfromtirana" for a public channel, or a numeric -100... id
        self.session = session or requests.Session()

    def send_message(self, text: str) -> int:
        """Post HTML-formatted text to the channel; returns Telegram's message_id."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},  # keep posts compact
        }
        url = API_URL.format(token=self.token, method="sendMessage")
        try:
            response = request_with_retry(self.session, "POST", url, json=payload)
        except requests.RequestException as exc:
            # "from None" drops the original error, whose text includes the URL (and bot token).
            raise TelegramError(f"Could not reach Telegram ({type(exc).__name__})") from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = body.get("description", "no details")
            raise TelegramError(f"Telegram refused the message (HTTP {response.status_code}): {description}")
        return body["result"]["message_id"]
