"""Make the free channel's invite links, one per place that advertises it.

Usage (from the repo root):
    python scripts/invite_links.py

For each place in invite_links (config.yaml) that has no link yet, the bot makes
one, named after the place ("instagram", "facebook", ...), and this script prints
the lines to paste into config.yaml. Places that have a link are left alone.

The links show in the Telegram app with how many people joined through each
(the channel -> Manage channel -> Invite links, "created by" the bot), and the
weekly report counts them too (src/growth.py). Because the bot made them,
Telegram tells it the whole link when someone joins: a link made by hand in the
app reaches the bot cut short, so the bot couldn't tell those apart.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID (from .env or the environment),
and the bot needs the admin right to add members to the channel.
"""
import sys
from pathlib import Path

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src.config import INVITE_SOURCES, PROJECT_ROOT, ConfigError, load_config, require_env  # noqa: E402
from src.telegram import TelegramClient, TelegramError  # noqa: E402


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
        env = require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID")
    except ConfigError as exc:
        print(f"Configuration problem: {exc}")
        return 2
    telegram = TelegramClient(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHANNEL_ID"])

    missing = [source for source in INVITE_SOURCES if source not in config.invite_links]
    if not missing:
        print("Every place already has its link in config.yaml (invite_links): nothing to make.")
        return 0
    made = {}
    for source in missing:
        try:
            made[source] = telegram.create_invite_link(source)
        except TelegramError as exc:
            print(f"Could not make the {source} link: {exc}")
            print("Is the bot an admin of the channel, with the right to add members?")
            break
    if made:
        print("Paste these under invite_links: in config.yaml:\n")
        for source, link in made.items():
            print(f'  {source}: "{link}"')
    return 0 if len(made) == len(missing) else 1


if __name__ == "__main__":
    sys.exit(main())
