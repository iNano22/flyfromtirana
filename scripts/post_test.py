"""Send one test message to the Telegram channel, to check the bot setup.

Usage (from the repo root):
    python scripts/post_test.py
    python scripts/post_test.py "Custom text"

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID (from .env or the environment).
"""
import sys
from datetime import datetime
from pathlib import Path

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src.config import PROJECT_ROOT, ConfigError, require_env  # noqa: E402
from src.telegram import TelegramClient, TelegramError  # noqa: E402


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        env = require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID")
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2

    text = " ".join(sys.argv[1:]) or f"✅ Test nga FlyFromTirana bot · {datetime.now():%Y-%m-%d %H:%M}"
    client = TelegramClient(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHANNEL_ID"])
    try:
        message_id = client.send_message(text)
    except TelegramError as exc:
        print(f"❌ {exc}")
        print("Check: is the bot an admin of the channel with 'Post messages' permission?")
        return 1
    print(f"✅ Sent to {env['TELEGRAM_CHANNEL_ID']} (message_id={message_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
