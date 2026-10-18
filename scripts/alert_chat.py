"""Find the chat for the failure alerts, and send a test alert there.

Usage (from the repo root):
    python scripts/alert_chat.py          # lists the chats that wrote to the bot lately
    python scripts/alert_chat.py --test   # sends a test alert to TELEGRAM_ALERT_CHAT_ID

To get the alerts in your own Telegram: open the bot (the one that posts to the
channel), press Start, then run this script. It shows your chat's number: put it
as TELEGRAM_ALERT_CHAT_ID in .env for local runs, and on the server in the deploy
tool (Coolify: Environment Variables). A private channel works too: add the bot as
an admin and use the channel's -100... number.

Only while the server is stopped: the server's bot (src/bot.py) reads every message
to the bot as it arrives, and Telegram gives each one to whoever asks first.

Needs TELEGRAM_BOT_TOKEN (from .env or the environment).
"""
import json
import sys
from pathlib import Path

import requests

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src.config import PROJECT_ROOT, ConfigError, require_env  # noqa: E402
from src.scheduler import ALERT_CHAT_ENV  # noqa: E402
from src.telegram import API_URL, TelegramClient, TelegramError  # noqa: E402


def list_chats(token: str) -> int:
    """Print every chat that wrote to the bot in the last day (what Telegram still keeps)."""
    # Telegram remembers which kinds of updates a bot last asked for, and drops the others.
    # Once something asked only for channel posts, so private messages were never delivered:
    # asking for them by name here puts them back.
    wanted = json.dumps(["message", "my_chat_member", "channel_post"])
    try:
        response = requests.get(API_URL.format(token=token, method="getUpdates"),
                                params={"allowed_updates": wanted}, timeout=20)
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"❌ Could not reach Telegram ({type(exc).__name__})")   # not the error itself: it holds the token
        return 1
    if not body.get("ok"):
        print(f"❌ Telegram said: {body.get('description', 'no details')}")
        return 1
    chats = {}
    for update in body["result"]:
        message = update.get("message") or update.get("my_chat_member") or update.get("channel_post") or {}
        chat = message.get("chat")
        if chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            chats[chat["id"]] = f"{name} ({chat['type']})"
    if not chats:
        print("No chats yet. Open the bot in Telegram, send it any message (\"hi\"), and run this again.")
        print("A message sent before the first run of this script may not count: send a new one.")
        return 1
    for chat_id, label in chats.items():
        print(f"{chat_id}   {label}")
    print(f"\nPut the number of your own chat as {ALERT_CHAT_ENV}.")
    return 0


def send_test(token: str) -> int:
    try:
        chat = require_env(ALERT_CHAT_ENV)[ALERT_CHAT_ENV]
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2
    try:
        TelegramClient(token, chat).send_message("✅ <b>FlyFromTirana</b>: a test alert. Failed scans will be reported here.")
    except TelegramError as exc:
        print(f"❌ {exc}")
        print("Check: did you press Start in the bot? For a channel: is the bot an admin there?")
        return 1
    print(f"✅ Sent to {chat}")
    return 0


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        token = require_env("TELEGRAM_BOT_TOKEN")["TELEGRAM_BOT_TOKEN"]
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2
    return send_test(token) if "--test" in sys.argv[1:] else list_chats(token)


if __name__ == "__main__":
    sys.exit(main())
