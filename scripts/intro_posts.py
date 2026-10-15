"""The nine posts that open the Instagram and Facebook accounts: draw them, send them for a look, post them.

Usage (from the repo root):
    python scripts/intro_posts.py                    # draw the cards into ig_preview/intro/
    python scripts/intro_posts.py --draft            # send all nine to your chat: the cards as files, then the captions
    python scripts/intro_posts.py --publish          # post 1 to 9 on Instagram and Facebook (on the server)
    python scripts/intro_posts.py --publish --only 3 # just one of them
    python scripts/intro_posts.py --publish --from 4 # carry on from number 4 (after a stop)

The texts are in content/intro_posts.yaml and the pictures are drawn by
src/intro_cards.py. They are posted in order, with a pause
between posts (--pause seconds, 30 by default), so number 9 ends up top-left of
the Instagram grid. Each post is reported to your chat like the daily ones; a
post that fails comes back as a draft. Nothing is remembered in the database:
these are not deals.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_DRAFTS_CHAT_ID (or TELEGRAM_ALERT_CHAT_ID);
--publish also needs META_PAGE_TOKEN and runs on the server, because Instagram
fetches the pictures from the website.
"""
import argparse
import html
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src.cards import save_jpeg  # noqa: E402
from src.intro_cards import draw_intro  # noqa: E402
from src.config import PROJECT_ROOT, Config, ConfigError, load_config, require_env  # noqa: E402
from src.main import post_by_bot  # noqa: E402
from src.meta import MetaClient, MetaError  # noqa: E402
from src.social import PICTURES_FOLDER, Post, failed, handle, site_address  # noqa: E402
from src.telegram import TelegramClient, TelegramError  # noqa: E402

POSTS_FILE = PROJECT_ROOT / "content" / "intro_posts.yaml"
PREVIEW_DIR = PROJECT_ROOT / "ig_preview" / "intro"


def load_posts(path: Path = POSTS_FILE) -> list[dict]:
    posts = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not 2 <= len(posts) <= 10:
        raise ConfigError(f"{path.name} must hold between 2 and 10 posts (Telegram sends up to 10 files at once)")
    return posts


def draw(post: dict, number: int, total: int, config: Config, out_dir: Path) -> Path:
    """The card of one post, as a JPEG in out_dir (src/intro_cards.py draws it)."""
    card = draw_intro(post, config, address=site_address(config), handle=handle(config))
    return save_jpeg(card, out_dir / f"intro_{number}.jpg")


def captions(post: dict, config: Config) -> tuple[str, str]:
    """The post's text for Instagram (no links: "linku në bio") and for Facebook (links)."""
    channel = config.channel_handle                       # "@flyfromtirana"
    telegram_url = f"https://t.me/{channel.lstrip('@')}"
    text = str(post["caption"]).strip()
    tags = f"\n\n{config.social.hashtags}" if config.social.hashtags else ""
    instagram = text.format(TELEGRAM=channel, WEBSITE=f"{site_address(config)} (linku në bio)",
                            PREMIUM=f"linku është në çdo ofertë të kanalit {channel}")
    facebook = text.format(TELEGRAM=telegram_url, WEBSITE=config.website.url,
                           PREMIUM=config.premium.join_link or telegram_url)
    return instagram + tags, facebook + tags


def main() -> int:
    parser = argparse.ArgumentParser(description="The nine introduction posts: draw, send for a look, or post.")
    parser.add_argument("--draft", action="store_true", help="send the cards and captions to your chat")
    parser.add_argument("--publish", action="store_true", help="post them on Instagram and Facebook, in order")
    parser.add_argument("--only", type=int, metavar="N", help="with --publish: only post number N")
    parser.add_argument("--from", dest="start", type=int, default=1, metavar="N",
                        help="with --publish: start at post number N")
    parser.add_argument("--pause", type=int, default=30, help="seconds between posts (default 30)")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
        posts = load_posts()
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2
    total = len(posts)

    if not args.draft and not args.publish:
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        for number, post in enumerate(posts, start=1):
            print(f"✅ {draw(post, number, total, config, PREVIEW_DIR)}")
        return 0

    try:
        env = require_env("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_DRAFTS_CHAT_ID", "").strip() or os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
        if not chat_id:
            raise ConfigError("Set TELEGRAM_DRAFTS_CHAT_ID (or TELEGRAM_ALERT_CHAT_ID): the chat to report to")
        meta = None
        if args.publish:
            if not config.social.publish or not config.website.url:
                raise ConfigError("--publish needs social.publish and website.url in config.yaml")
            meta = MetaClient(require_env("META_PAGE_TOKEN")["META_PAGE_TOKEN"])
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2
    telegram = TelegramClient(env["TELEGRAM_BOT_TOKEN"], chat_id)

    with tempfile.TemporaryDirectory(prefix="flyfromtirana-intro-") as folder:
        cards = [draw(post, number, total, config, Path(folder)) for number, post in enumerate(posts, start=1)]
        try:
            if args.draft:
                telegram.send_files(cards, f"📸 <b>{total} postimet hyrëse</b>, me radhën e postimit (1 → {total}). "
                                           "Tekstet vijnë më poshtë, një mesazh për postim.")
                for number, post in enumerate(posts, start=1):
                    instagram, _ = captions(post, config)
                    telegram.send_message(f"<b>{number}/{total}</b>\n\n{html.escape(instagram, quote=False)}")
                print(f"✅ Sent {total} cards and {total} captions to chat {chat_id}.")
                return 0

            numbers = [args.only] if args.only else list(range(args.start, total + 1))
            problems = 0
            for number in numbers:
                post = posts[number - 1]
                instagram, facebook = captions(post, config)
                ready = Post(kind="intro", title=f"Hyrje {number}/{total}: {post.get('title', '')}", pictures=[cards[number - 1]],
                             story=None, caption=instagram, facebook_text=facebook, link=config.website.url)
                print(f"Posting {ready.title} on {', '.join(config.social.publish)}...")
                outcomes = post_by_bot(ready, meta, config.social.publish, config.website.output_dir / PICTURES_FOLDER,
                                       config.website.url, telegram, datetime.now(timezone.utc))
                for what, outcome in outcomes:
                    print(f"{'❌' if isinstance(outcome, MetaError) else '✅'} {what}: {outcome or 'done'}")
                if failed(outcomes):
                    problems += 1
                    print(f"Stopping at {number}: fix the problem, then --publish --from {number}.")
                    break
                if number != numbers[-1]:
                    time.sleep(args.pause)
            return 1 if problems else 0
        except TelegramError as exc:
            print(f"❌ {exc}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
