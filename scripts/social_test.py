"""Make one Instagram/Facebook post right now: as a draft, or for real.

Usage (from the repo root):
    python scripts/social_test.py                # the best deal of the moment (a scan), over every route
    python scripts/social_test.py --routes BGY   # only these routes (faster: a few API calls)
    python scripts/social_test.py --weekly       # the week's top deals, from the database (no scan)
    python scripts/social_test.py --guide        # the next city guide (no scan)
    python scripts/social_test.py --publish      # any of the above posted on Instagram/Facebook instead of drafted

It scans like a real run (Travelpayouts API, both directions) and takes the best
deal the free channel's rules would post. Without --publish it sends the draft
(the two cards, the caption and the Facebook link) to TELEGRAM_DRAFTS_CHAT_ID (or
TELEGRAM_ALERT_CHAT_ID). With --publish it posts on the platforms in
social.publish (config.yaml) with META_PAGE_TOKEN, and sends the report there.
Instagram fetches the pictures from the website, so --publish only works where
the site's folder is served: on the server (in Coolify: Terminal ->
`python scripts/social_test.py --publish`), not on a laptop.

The channel gets no post, and the prices are fetched into a throwaway copy of the
database. What was posted (or drafted) is remembered like a real post, so the bot
doesn't repeat it: the deal stays off the social posts for the cooldown, the week's
top is done for today, and the city guide moves on to the next city. To mark the
next city as done without posting it (you posted it by hand): --guide --skip.

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
from src.deals import CHANNEL, price_band, rank_deals  # noqa: E402
from src.links import LinkBuilder  # noqa: E402
from src.main import choose_guide, city_price, find_deals, post_by_bot, scan_routes, select_routes  # noqa: E402
from src.meta import MetaClient, MetaError  # noqa: E402
from src.photos import post_photo  # noqa: E402
from src.scanner import PriceScanner, Quote, ScannerError  # noqa: E402
from src.social import (GUIDE_CHANNEL, PICTURES_FOLDER, SOCIAL_CHANNEL, WEEKLY_CHANNEL, WEEKLY_MIN_DEALS,  # noqa: E402
                        deal_post, failed, guide_post, send_draft, weekly_post)
from src.website import build_offers, page_url  # noqa: E402
from src.storage import Storage  # noqa: E402
from src.telegram import TelegramClient, TelegramError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Make one social media post now.")
    parser.add_argument("--routes", help="only scan these destinations, e.g. BGY,VIE")
    parser.add_argument("--publish", action="store_true",
                        help="post on Instagram/Facebook (social.publish in config.yaml) instead of sending a draft")
    parser.add_argument("--weekly", action="store_true",
                        help="the week's top deals (a carousel) from the database, no scan")
    parser.add_argument("--guide", action="store_true", help="the next city guide, no scan")
    parser.add_argument("--skip", action="store_true",
                        help="with --guide: mark the next city as posted without posting it")
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
    local_now = now.astimezone(ZoneInfo(config.timezone))
    # The booking link for Facebook, with the social SubID (as main.publish_social builds it).
    links = LinkBuilder(replace(config.links, sub_id=config.social.sub_id), env["TRAVELPAYOUTS_MARKER"],
                        origin=config.origin, currency=config.currency)
    with tempfile.TemporaryDirectory(prefix="flyfromtirana-social-test-") as folder:
        # A throwaway copy of the database: its price history gives the "usual" prices,
        # today's prices are saved into the copy only, and the real file stays as it is.
        db_copy = Path(folder) / "prices.db"
        shutil.copy2(config.db_path, db_copy)
        remember: list[tuple[str, list[Quote]]] = []   # what to record in the real database afterwards
        with Storage(db_copy) as storage:
            if args.weekly:
                top = [o for o in build_offers(config, storage, now) if o.is_deal][: config.social.weekly_count]
                if len(top) < WEEKLY_MIN_DEALS:
                    print(f"Only {len(top)} deal(s) on the website: nothing for a top-of-the-week post.")
                    return 1
                photos = {o.route.iata: post_photo(o.route.iata) for o in top}
                post = weekly_post(top, config, Path(folder), photos=photos, now=local_now)
                remember.append((WEEKLY_CHANNEL, [o.best for o in top]))
            elif args.guide:
                choice = choose_guide(config, storage)
                page = page_url(config, config.language, choice[0].slug) if choice else None
                if not choice or not page:
                    print("No city guide to post (needs a guide in the posts' language and website.url).")
                    return 1
                guide, iata = choice
                price = city_price(build_offers(config, storage, now), guide.city)
                marker = Quote(origin=config.origin, destination=iata, depart_date=local_now.date(), price=price or 0,
                               airline="", transfers=0, duration_min=None, link=None, fetched_at=now)
                remember.append((GUIDE_CHANNEL, [marker]))
                if args.skip:
                    record(config, remember, now)
                    following = choose_guide(config, Storage(config.db_path))
                    print(f"✅ {guide.display_name} is marked as posted; the next guide is "
                          f"{following[0].display_name if following else 'none'}.")
                    return 0
                post = guide_post(guide, iata, price, config, Path(folder), photo=post_photo(iata), page_url=page)
            else:
                scanner = PriceScanner(env["TRAVELPAYOUTS_TOKEN"], currency=config.currency, market=config.market,
                                       lookahead_days=config.lookahead_days,
                                       request_delay_seconds=config.request_delay_seconds)
                print(f"Scanning {len(routes)} route(s) from {config.origin}...")
                try:
                    outbound, inbound = scan_routes(scanner, storage, config, routes, local_now.date(), now)
                except ScannerError as exc:
                    print(f"❌ {exc}")
                    return 1
                deals = find_deals(routes, outbound, inbound, storage, config.origin, config.rules, now, CHANNEL)
                best = rank_deals(deals, 1)
                if not best:
                    print("No route is a deal by the free channel's rules right now: nothing to post.")
                    return 1
                post = deal_post(best[0], config, links, Path(folder), photo=post_photo(best[0].route.iata))
                remember.append((SOCIAL_CHANNEL, best[0].quotes))
        telegram = TelegramClient(env["TELEGRAM_BOT_TOKEN"], chat_id)
        try:
            if meta is None:
                print(f"Sending the draft of \"{post.title}\" to chat {chat_id}...")
                send_draft(telegram, post)
                print("✅ Sent: the pictures as files, the caption, and the Facebook link.")
            else:
                print(f"Posting \"{post.title}\" on {', '.join(config.social.publish)}...")
                outcomes = post_by_bot(post, meta, config.social.publish,
                                       config.website.output_dir / PICTURES_FOLDER, config.website.url, telegram, now)
                for what, outcome in outcomes:
                    if isinstance(outcome, MetaError):
                        print(f"❌ {what}: {outcome}")
                    else:
                        print(f"✅ {what}: {outcome or 'done'}")
                print(f"The report went to chat {chat_id}" +
                      (", with a draft for what failed." if failed(outcomes) else "."))
                if failed(outcomes):
                    return 1
        except TelegramError as exc:
            print(f"❌ {exc}")
            print("Check: did you press Start in the bot's chat (or make it an admin of the channel)?")
            return 1
        record(config, remember, now)
    return 0


def record(config, remember: list[tuple[str, list[Quote]]], now: datetime) -> None:
    """Remember the post in the real database, as a run would, so the bot doesn't repeat it."""
    with Storage(config.db_path) as real:
        for channel, quotes in remember:
            for quote in quotes:
                real.record_post(channel, quote, price_band(quote.price, config.rules.price_band_eur), now)


if __name__ == "__main__":
    sys.exit(main())
