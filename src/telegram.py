"""Sends posts to the Telegram channel through the Bot API.

The bot must be an admin of the channel with permission to post messages.
Docs: https://core.telegram.org/bots/api#sendmessage
      https://core.telegram.org/bots/api#sendphoto
"""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path

import requests

from src.http_client import request_with_retry

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
# The text under a photo may be this long (a plain message may have 4096
# characters). Telegram counts what the reader sees, not the HTML tags.
CAPTION_LIMIT = 1024


class TelegramError(Exception):
    """Telegram refused the message or couldn't be reached."""


class TelegramRefused(TelegramError):
    """Telegram answered, and the answer was no: nothing was posted."""


class TelegramClient:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id  # "@flyfromtirana" for a public channel, or a numeric -100... id
        self.session = session or requests.Session()

    def send_post(self, text: str, photo: Path | None = None) -> int:
        """Post the text, under the photo when there is one; returns Telegram's message_id.

        The photo is a bonus, never a reason to lose a post: when the text is
        too long for a caption, or Telegram refuses the photo, the text is
        posted on its own.
        """
        if photo is None:
            return self.send_message(text)
        if visible_length(text) > CAPTION_LIMIT:
            log.warning("Post is too long to go under a photo (%d characters): sending it without %s",
                        visible_length(text), photo.name)
            return self.send_message(text)
        try:
            return self.send_photo(photo, text)
        except TelegramRefused as exc:
            # Only after a clear "no": a network error might mean the photo did
            # go through, and posting the text as well would show the deal twice.
            log.warning("%s. Sending the post without %s", exc, photo.name)
            return self.send_message(text)

    def send_message(self, text: str) -> int:
        """Post HTML-formatted text to the channel; returns Telegram's message_id."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},  # keep posts compact
        }
        return self._call("sendMessage", json=payload)["message_id"]

    def send_photo(self, photo: Path, caption: str) -> int:
        """Post a photo with HTML-formatted text under it; returns Telegram's message_id."""
        fields = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        # The file is read into memory first, so a retry sends the whole photo again.
        files = {"photo": (photo.name, photo.read_bytes(), "image/jpeg")}
        return self._call("sendPhoto", data=fields, files=files)["message_id"]

    def _call(self, method: str, **request) -> dict:
        """One Bot API call. Returns its "result", or raises TelegramError."""
        url = API_URL.format(token=self.token, method=method)
        try:
            response = request_with_retry(self.session, "POST", url, **request)
        except requests.RequestException as exc:
            # "from None" drops the original error, whose text includes the URL (and bot token).
            raise TelegramError(f"Could not reach Telegram ({type(exc).__name__})") from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = body.get("description", "no details")
            raise TelegramRefused(f"Telegram refused the message (HTTP {response.status_code}): {description}")
        return body["result"]


def visible_length(text: str) -> int:
    """How long Telegram considers HTML text to be: without the tags, an emoji counting as two."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2
