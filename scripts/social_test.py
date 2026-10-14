"""Make one Instagram/Facebook post right now, with today's prices: as a draft, or for real.

Usage (from the repo root):
    python scripts/social_test.py                # the best deal of the moment, over every route
    python scripts/social_test.py --routes BGY   # only these routes (faster: a few API calls)
    python scripts/social_test.py --publish      # post it on Instagram/Facebook instead of drafting

It scans like a real run (Travelpayouts API, both directions) and takes the best
deal the free channel's rules would post. Without --publish it sends the draft
(the two cards, the caption and the Facebook link) to TELEGRAM_DRAFTS_CHAT_ID (or
TELEGRAM_ALERT_CHAT_ID). With --publish it posts on the platforms in
social.publish (config.yaml) with META_PAGE_TOKEN, and sends the report there.
Instagram fetches the pictures from the website, so --publish only works where
the site's folder is served: on the server (in Coolify: Terminal ->
`python scripts/social_test.py --publish`), not on a laptop.

Nothing else happens: the channel gets no post, the prices are fetched into a
throwaway copy of the database, and the deal is not marked as posted, so the
next real run may post it again.

Needs TRAVELPAYOUTS_TOKEN, TRAVELPAYOUTS_MARKER, TELEGRAM_BOT_TOKEN and one of the
chat ids (from .env or the environment); --publish also needs META_PAGE_TOKEN.
"""
import argparse
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src.config import PROJECT_ROOT, ConfigError, load_config, require_env  # noqa: E402
from src.deals import CHANNEL, rank_deals  # noqa: E402
from src.links import LinkBuilder  # noqa: E402
from src.main import find_deals, post_by_bot, scan_routes, select_routes  # noqa: E402
from src.meta import MetaClient  # noqa: E402
from src.photos import post_photo  # noqa: E402
from src.scanner import PriceScanner, ScannerError  # noqa: E402
from src.social import PICTURES_FOLDER, make_draft, send_draft  # noqa: E402
from src.storage import Storage  # noqa: E402
from src.telegram import TelegramClient, TelegramError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Make one social media post now, from today's prices.")
    parser.add_argument("--routes", help="only scan these destinations, e.g. BGY,VIE")
    parser.add_argument("--publish", action="store_true",
                        help="post on Instagram/Facebook (social.publish in config.yaml) instead of sending a draft")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
        env = require_env("TRAVELPAYOUTS_TOKEN", "TRAVELPAYOUTS_MARKER", "TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_DRAFTS_CHAT_ID", "").strip() or os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
        if not chat_id:
            raise ConfigError("Set TELEGRAM_DRAFTS_CHAT_ID (or TELEGRAM_ALERT_CHAT_ID): the chat the draft goes to")
        meta = None
        if args.publish:
            if not config.social.publish:
                raise ConfigError("social.publish in config.yaml is empty: nowhere to post")
            if not config.website.url:
                raise ConfigError("website.url in config.yaml is empty: Instagram needs the pictures on the site")
            meta = MetaClient(require_env("META_PAGE_TOKEN")["META_PAGE_TOKEN"])
        only = {code.strip().upper() for code in args.routes.split(",")} if args.routes else None
        routes = select_routes(config.routes, only)
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2

    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    scanner = PriceScanner(env["TRAVELPAYOUTS_TOKEN"], currency=config.currency, market=config.market,
                           lookahead_days=config.lookahead_days, request_delay_seconds=config.request_delay_seconds)
    # The booking link for Facebook, with the social SubID (as main.publish_social builds it).
    links = LinkBuilder(replace(config.links, sub_id=config.social.sub_id), env["TRAVELPAYOUTS_MARKER"],
                        origin=config.origin, currency=config.currency)
    with tempfile.TemporaryDirectory(prefix="flyfromtirana-social-test-") as folder:
        # A throwaway copy of the database: its price history gives the "usual" prices,
        # today's prices are saved into the copy only, and the real file stays as it is.
        db_copy = Path(folder) / "prices.db"
        shutil.copy2(config.db_path, db_copy)
        with Storage(db_copy) as storage:
            print(f"Scanning {len(routes)} route(s) from {config.origin}...")
            try:
                outbound, inbound = scan_routes(scanner, storage, config, routes, today, now)
            except ScannerError as exc:
                print(f"❌ {exc}")
                return 1
            deals = find_deals(routes, outbound, inbound, storage, config.origin, config.rules, now, CHANNEL)
            best = rank_deals(deals, 1)
            if not best:
                print("No route is a deal by the free channel's rules right now: nothing to draft.")
                return 1
            deal = best[0]
            draft = make_draft(deal, config, links, Path(folder), photo=post_photo(deal.route.iata))
        telegram = TelegramClient(env["TELEGRAM_BOT_TOKEN"], chat_id)
        try:
            if meta is None:
                print(f"Sending the draft for {deal.summary()} to chat {chat_id}...")
                send_draft(telegram, draft)
                print("✅ Sent: the two cards as files, the caption, and the Facebook link.")
            else:
                print(f"Posting {deal.summary()} on {', '.join(config.social.publish)}...")
                post_by_bot(draft, meta, config.social.publish, config.website.output_dir / PICTURES_FOLDER,
                            config.website.url, telegram, now)
                print(f"✅ Done: the report (and a draft for anything that failed) went to chat {chat_id}.")
        except TelegramError as exc:
            print(f"❌ {exc}")
            print("Check: did you press Start in the bot's chat (or make it an admin of the channel)?")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
