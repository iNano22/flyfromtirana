"""Sends posts to the Telegram channel through the Bot API.

The bot must be an admin of the channel with permission to post messages.
Docs: https://core.telegram.org/bots/api#sendmessage
      https://core.telegram.org/bots/api#sendphoto

The same client also sends the social media drafts to the owner's own chat
(src/social.py): any chat the bot may write to works as chat_id.
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import re
from pathlib import Path

import requests

from src.http_client import request_with_retry

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
# The text under a photo may be this long (a plain message may have 4096
# characters). Telegram counts what the reader sees, not the HTML tags.
CAPTION_LIMIT = 1024


# Buttons under a message: rows of (label, what it does). What it does is an address
# (https://... opens it) or a short code the bot gets back when the button is pressed.
Buttons = list[list[tuple[str, str]]]
# The kinds of updates the bot reads (get_updates): messages, button taps, channel joins.
UPDATE_KINDS = ("message", "callback_query", "chat_member")


def keyboard(buttons: Buttons) -> dict:
    """Buttons as Telegram wants them (an inline keyboard)."""
    return {"inline_keyboard": [[{"text": label, "url": action} if action.startswith("https://")
                                 else {"text": label, "callback_data": action} for label, action in row]
                                for row in buttons]}


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

    def send_message(self, text: str, buttons: Buttons | None = None) -> int:
        """Post HTML-formatted text to the chat, with buttons under it if given; returns Telegram's message_id."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},  # keep posts compact
        }
        if buttons:
            payload["reply_markup"] = keyboard(buttons)
        return self._call("sendMessage", json=payload)["message_id"]

    def edit_message(self, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        """Replace a message's text (and buttons): the bot's menus change in place when a button is pressed."""
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}, "reply_markup": keyboard(buttons or [])}
        try:
            self._call("editMessageText", json=payload)
        except TelegramRefused as exc:
            if "message is not modified" not in str(exc):   # the same button pressed twice: nothing to change
                raise

    def send_photo(self, photo: Path, caption: str) -> int:
        """Post a photo with HTML-formatted text under it; returns Telegram's message_id."""
        fields = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
        # The file is read into memory first, so a retry sends the whole photo again.
        files = {"photo": (photo.name, photo.read_bytes(), "image/jpeg")}
        return self._call("sendPhoto", data=fields, files=files)["message_id"]

    def send_files(self, paths: list[Path], caption: str) -> list[int]:
        """Send pictures (or a video) as files, in one group, with HTML text under the group.

        A photo sent as a file keeps its size and quality (Telegram shrinks a
        "photo" to 1280px), so the receiver can post it elsewhere as it is.
        Returns the message_id of each file (Telegram makes one message per file).
        Docs: https://core.telegram.org/bots/api#sendmediagroup
        """
        # Each item names its file as attach://<field>; the fields hold the bytes.
        media = []
        files = {}
        for number, path in enumerate(paths):
            field = f"file{number}"
            item = {"type": "document", "media": f"attach://{field}"}
            if number == 0:
                item.update(caption=caption, parse_mode="HTML")   # shown once, under the whole group
            media.append(item)
            kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"   # image/jpeg, video/mp4
            files[field] = (path.name, path.read_bytes(), kind)
        fields = {"chat_id": self.chat_id, "media": json.dumps(media)}
        return [message["message_id"] for message in self._call("sendMediaGroup", data=fields, files=files)]

    # --- the channels' numbers (src/growth.py) ---------------------------------------

    def member_count(self) -> int:
        """How many members the chat has, the bot and the admins included."""
        return self._call("getChatMemberCount", json={"chat_id": self.chat_id})

    def create_invite_link(self, name: str) -> str:
        """A new invite link for the chat, called `name` in the app's list of links.

        The bot needs the admin right to add members. Docs: https://core.telegram.org/bots/api#createchatinvitelink
        """
        return self._call("createChatInviteLink", json={"chat_id": self.chat_id, "name": name})["invite_link"]

    def member_status(self, user_id: int) -> str:
        """Whether a person is in the chat: "member", "administrator", "creator", "left", "kicked", ...

        Telegram answers this for chats where the bot is an admin (the channels)."""
        member = self._call("getChatMember", json={"chat_id": self.chat_id, "user_id": user_id})
        if member.get("status") == "restricted":
            return "member" if member.get("is_member") else "left"
        return member.get("status", "left")

    # --- the bot's own chat (src/bot.py) -----------------------------------------------

    def get_updates(self, offset: int | None = None, *, wait: int = 0, kinds: tuple[str, ...] = UPDATE_KINDS,
                    limit: int = 100) -> list[dict]:
        """What happened since the last call: messages to the bot, button taps, people joining the
        channels. Waits up to `wait` seconds for something to happen (long polling).

        Telegram keeps an update for 24 hours, until a call with an offset above its update_id
        confirms it. It also remembers `kinds` and drops any other kind from then on, so every
        call names all the kinds the bot needs. Docs: https://core.telegram.org/bots/api#getupdates
        """
        payload = {"timeout": wait, "limit": limit, "allowed_updates": list(kinds)}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", json=payload, timeout=wait + 15)

    def answer_button(self, callback_id: str, text: str | None = None) -> None:
        """Tell Telegram a button tap was handled (it stops the button's spinner), with a short note."""
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", json=payload)

    def set_commands(self, commands: list[tuple[str, str]], description: str) -> None:
        """The bot's menu of commands, and the text people see before they press Start."""
        self._call("setMyCommands", json={"commands": [{"command": name, "description": about}
                                                       for name, about in commands]})
        self._call("setMyDescription", json={"description": description})

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
