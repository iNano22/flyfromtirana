"""FlyFromTirana entry point: scan prices -> store -> detect deals -> post -> social drafts.

Run from the repo root:
    python -m src.main --dry-run                     # print posts instead of sending them
    python -m src.main --dry-run --routes BGY,VIE -v # a couple of routes, debug logging
    python -m src.main                               # the real thing (what src/scheduler.py runs)
    python -m src.main --premium-only                # the scheduler's quick scan between full runs:
                                                     # posts to premium only, saves no prices

Exit codes: 0 = OK, 1 = the run failed (API down, a post failed, ...),
2 = configuration problem (missing env var, bad config.yaml).
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from src.config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    Config,
    ConfigError,
    DealRules,
    Route,
    Secrets,
    load_config,
    load_secrets,
)
from src.deals import (CHANNEL, PREMIUM_CHANNEL, Deal, early_access_cutoff, find_route_deal, price_band,
                       rank_deals)
from src.formatter import format_post
from src.guides import Guide
from src.links import LinkBuilder
from src.meta import MetaClient, MetaError
from src.photos import post_photo
from src.scanner import PriceScanner, Quote, RouteFetchError, ScannerError
from src.social import (GUIDE_CHANNEL, PICTURES_FOLDER, SOCIAL_CHANNEL, WEEKLY_CHANNEL, WEEKLY_MIN_DEALS, Post,
                        deal_post, failed, guide_post, post_on_platforms, prune_pictures, put_pictures_online,
                        report, send_draft, weekly_post)
from src.storage import Storage
from src.telegram import TelegramClient, TelegramError
from src.website import build_offers, page_url, site_guides

log = logging.getLogger("flyfromtirana")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # urllib3's debug lines include request URLs, and the Telegram URL contains the bot token.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(PROJECT_ROOT / ".env")  # does nothing if there's no .env (e.g. on the server)
    try:
        config = load_config(args.config)
        if args.ignore_quiet_hours:
            config = replace(config, quiet_hours=None)
        secrets = load_secrets(dry_run=args.dry_run, premium=config.premium.enabled)
        only_routes = {code.strip().upper() for code in args.routes.split(",")} if args.routes else None
        return run(config, secrets, dry_run=args.dry_run, only_routes=only_routes,
                   premium_only=args.premium_only)
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return 2
    except ScannerError as exc:
        log.error("%s", exc)
        return 1
    except Exception:
        log.exception("Unexpected error")
        return 1


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan cheap flights from Tirana and post deals to Telegram.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print posts instead of sending them (nothing is marked as posted)")
    parser.add_argument("--routes", help="only scan these destinations, e.g. BGY,VIE")
    parser.add_argument("--premium-only", action="store_true",
                        help="quick scan: post new deals to the premium channel only (no free-channel "
                             "posts, prices are not saved)")
    parser.add_argument("--ignore-quiet-hours", action="store_true",
                        help="post even during quiet hours (e.g. a manual test at night)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def run(
    config: Config,
    secrets: Secrets,
    *,
    dry_run: bool,
    only_routes: set[str] | None = None,
    premium_only: bool = False,
    session: requests.Session | None = None,
    now: datetime | None = None,
    output: Callable[[str], None] = print,
) -> int:
    """One scan. `session`, `now` and `output` are only passed in by tests.

    premium_only: the quick scan the scheduler runs between the full ones, so
    premium gets a deal within minutes. It only posts to the premium channel
    and saves no prices: the price history (and so the "usual" price) keeps
    growing at the pace of the full runs, and the database stays small.
    """
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(config.timezone))
    session = session or requests.Session()
    premium = config.premium
    if premium_only and not premium.enabled:
        log.info("Premium is off in config.yaml: a premium-only scan has nothing to do")
        return 0
    if premium_only and not dry_run and in_quiet_hours(local_now.time(), config.quiet_hours):
        # Nothing would be posted or saved, so don't spend the API calls.
        log.info("Quiet hours (%s local): skipping the premium scan", local_now.strftime("%H:%M"))
        return 0

    routes = select_routes(config.routes, only_routes)
    links = LinkBuilder(config.links, secrets.travelpayouts_marker, origin=config.origin, currency=config.currency)
    links.validate()
    if not secrets.travelpayouts_marker:
        log.warning("TRAVELPAYOUTS_MARKER is not set: links in this dry run carry no affiliate marker")

    scanner = PriceScanner(
        secrets.travelpayouts_token,
        currency=config.currency,
        market=config.market,
        lookahead_days=config.lookahead_days,
        request_delay_seconds=config.request_delay_seconds,
        session=session,
    )
    def telegram_for(chat_id: str) -> TelegramClient | None:
        return None if dry_run else TelegramClient(secrets.telegram_bot_token, chat_id, session)

    log.info("Scanning %d route(s) from %s%s%s", len(routes), config.origin,
             " (premium only)" if premium_only else "", " (dry run)" if dry_run else "")
    with Storage(config.db_path) as storage:
        # 1) Fetch and store prices
        outbound, inbound = scan_routes(scanner, storage, config, routes, local_now.date(), now,
                                        save=not premium_only)
        if not outbound:
            log.error("Every route failed to fetch, nothing to do")
            return 1

        # 2) Find deals and post the best ones: premium first (instantly), then free
        common = dict(config=config, links=links, storage=storage, dry_run=dry_run,
                      now=now, local_now=local_now, output=output)
        exit_code = 0
        if premium.enabled:
            deals = find_deals(routes, outbound, inbound, storage, config.origin, premium.rules, now,
                               PREMIUM_CHANNEL)
            best = rank_deals(deals, premium.max_posts_per_run)
            log.info("[premium] %d route(s) with new deals, posting the best %d", len(deals), len(best))
            exit_code |= publish(best, channel=PREMIUM_CHANNEL, template=f"{config.language}_premium",
                                 telegram=telegram_for(secrets.telegram_premium_channel_id), **common)
        if premium_only:
            return exit_code

        # The free channel only gets deals premium has had for free_delay_hours.
        early_access = {}
        if premium.enabled:
            early_access = dict(early_access_from=PREMIUM_CHANNEL,
                                early_access_until=early_access_cutoff(now, premium.free_delay_hours))
        deals = find_deals(routes, outbound, inbound, storage, config.origin, config.rules, now, CHANNEL,
                           **early_access)
        best = rank_deals(deals, config.max_posts_per_run)
        log.info("[free] %d route(s) with new deals, posting the best %d", len(deals), len(best))
        exit_code |= publish(best, channel=CHANNEL, template=config.language,
                             telegram=telegram_for(secrets.telegram_channel_id),
                             premium_link=premium.join_link if premium.enabled else None, **common)

        # 3) The Instagram/Facebook posts of the free channel's deals: posted by the bot, or
        #    sent to the owner as drafts
        exit_code |= publish_social(best, telegram=telegram_for(secrets.telegram_drafts_chat_id),
                                    drafts_chat=bool(secrets.telegram_drafts_chat_id),
                                    meta=meta_client(config, secrets, session, dry_run=dry_run),
                                    marker=secrets.travelpayouts_marker, **common)

        # 4) Housekeeping
        pruned = storage.prune_prices(now - timedelta(days=config.history_retention_days))
        if pruned:
            log.info("Deleted %d price rows older than %d days", pruned, config.history_retention_days)
    return exit_code


def select_routes(routes: list[Route], only: set[str] | None) -> list[Route]:
    if not only:
        return routes
    unknown = only - {r.iata for r in routes}
    if unknown:
        raise ConfigError(f"--routes has codes that aren't in config.yaml: {', '.join(sorted(unknown))}")
    return [r for r in routes if r.iata in only]


def scan_routes(scanner: PriceScanner, storage: Storage, config: Config, routes: list[Route],
                today: date, now: datetime, *,
                save: bool = True) -> tuple[dict[str, list[Quote]], dict[str, list[Quote]]]:
    """Fetch and save prices for every route, both directions.

    Returns (outbound, inbound), keyed by destination code. Routes that failed
    are left out; a route with no prices maps to an empty list.
    save=False (the quick premium scan) leaves the price history as it is.
    """
    outbound: dict[str, list[Quote]] = {}
    inbound: dict[str, list[Quote]] = {}
    for route in routes:
        try:
            outbound[route.iata] = scanner.fetch_route(config.origin, route.iata, today, now)
        except RouteFetchError as exc:
            log.error("Skipping %s: %s", route.iata, exc)
            continue
        inbound[route.iata] = []
        if config.scan_return_flights:
            try:
                inbound[route.iata] = scanner.fetch_route(route.iata, config.origin, today, now)
            except RouteFetchError as exc:
                log.warning("No return prices for %s: %s", route.iata, exc)
        if save:
            storage.save_quotes(outbound[route.iata] + inbound[route.iata])
    return outbound, inbound


def find_deals(routes: list[Route], outbound: dict[str, list[Quote]], inbound: dict[str, list[Quote]],
               storage: Storage, origin: str, rules: DealRules, now: datetime, channel: str,
               **early_access) -> list[Deal]:
    """One Deal per route that has something new to post on `channel`."""
    deals = []
    for route in routes:
        if route.iata not in outbound:
            continue
        deal = find_route_deal(route, origin, outbound[route.iata], inbound.get(route.iata, []),
                               storage, rules, now, channel, **early_access)
        if deal:
            deals.append(deal)
    return deals


def publish(deals: list[Deal], *, channel: str, template: str, config: Config, links: LinkBuilder,
            storage: Storage, telegram: TelegramClient | None, dry_run: bool, now: datetime,
            local_now: datetime, output: Callable[[str], None], premium_link: str | None = None) -> int:
    """Send (or, in a dry run, print) each deal to one channel. Returns 1 if any post failed, else 0."""
    if not deals:
        return 0
    quiet = in_quiet_hours(local_now.time(), config.quiet_hours)
    if quiet and not dry_run:
        log.info("Quiet hours (%s local): not posting. Deals are checked again next run.",
                 local_now.strftime("%H:%M"))
        return 0
    if quiet:
        log.info("Note: it's quiet hours, so a real run would not post right now")

    failures = 0
    for deal in deals:
        # The destination's photo, if the route has one (assets/telegram/<iata>.jpg).
        photo = post_photo(deal.route.iata) if config.post_photos else None
        text = format_post(deal, links, language=config.language, channel_handle=config.channel_handle,
                           airline_names=config.airlines, template=template, premium_link=premium_link,
                           website_url=config.website.url, photo=photo)
        if dry_run:
            with_photo = f" · photo {photo.path.name}" if photo else ""
            output(f"\n----- DRY RUN [{channel}] · {deal.summary()}{with_photo} -----\n{text}\n")
            continue
        try:
            message_id = telegram.send_post(text, photo.path if photo else None)
        except TelegramError as exc:
            log.error("[%s] Could not post %s: %s", channel, deal.summary(), exc)
            failures += 1
            continue
        # Remember every date shown in the post, so none of them is reposted too soon.
        for quote in deal.quotes:
            storage.record_post(channel, quote, price_band(quote.price, config.rules.price_band_eur), now)
        log.info("[%s] Posted %s (message %s)", channel, deal.summary(), message_id)
    return 1 if failures else 0


def meta_client(config: Config, secrets: Secrets, session: requests.Session, *, dry_run: bool) -> MetaClient | None:
    """The Meta client for posting by the bot, or None when the posts go out as drafts instead."""
    social = config.social
    if dry_run or not social.enabled or not social.publish:
        return None
    if not secrets.meta_page_token:
        log.info("META_PAGE_TOKEN is not set: Instagram/Facebook posts are sent to the owner as drafts")
        return None
    if not config.website.url:
        log.error("social.publish needs website.url in config.yaml (Instagram fetches the pictures from "
                  "the site): the posts are sent as drafts instead")
        return None
    return MetaClient(secrets.meta_page_token, session)


def publish_social(deals: list[Deal], *, config: Config, links: LinkBuilder, storage: Storage,
                   telegram: TelegramClient | None, drafts_chat: bool, meta: MetaClient | None, marker: str,
                   dry_run: bool, now: datetime, local_now: datetime, output: Callable[[str], None]) -> int:
    """The Instagram/Facebook posts of this run (src/social.py): the free channel's deals,
    the week's top deals on social.weekly_day, and a city guide on a day without a post.

    `deals` are the free channel's posts of this run, best first. Only the first
    social.max_posts_per_day of a day are taken, and a deal already handled
    (same route, date and price band) is skipped, like a channel skips a repost.
    With `meta` the bot posts and reports to the owner; a post that fails is sent
    as a draft, so it can go out by hand. Either way the post counts as handled.
    Returns 1 if the owner's chat could not be reached, else 0. A problem here
    never stops the Telegram posts: they are already out.
    """
    social = config.social
    if not social.enabled:
        return 0
    if not dry_run and not drafts_chat:
        log.warning("Social posts are on in config.yaml but TELEGRAM_DRAFTS_CHAT_ID is not set: nothing sent")
        return 0
    if in_quiet_hours(local_now.time(), config.quiet_hours) and not dry_run:
        return 0   # the channel posts wait for the morning, and so do these

    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    pictures_folder = config.website.output_dir / PICTURES_FOLDER
    if meta is not None:
        gone = prune_pictures(pictures_folder, now - timedelta(days=social.keep_pictures_days))
        if gone:
            log.info("[social] Removed %d posted picture(s) older than %d days from the site", gone,
                     social.keep_pictures_days)
    # The booking link for Facebook carries its own SubID, so its clicks show apart in the stats.
    social_links = LinkBuilder(replace(links.settings, sub_id=social.sub_id), marker,
                               origin=config.origin, currency=config.currency)
    # The pictures are drawn into a folder of their own. A real run deletes it once
    # they are sent; a dry run keeps it, so the pictures can be looked at.
    folder = Path(tempfile.mkdtemp(prefix="flyfromtirana-social-"))
    failures = 0
    posted_today = storage.count_posts(SOCIAL_CHANNEL, day_start) + storage.count_posts(WEEKLY_CHANNEL, day_start)

    def deliver(post: Post, channel: str, quotes: list[Quote]) -> bool:
        """Post it, or send its draft, and remember it. False when the owner's chat failed."""
        if dry_run:
            output(f"\n----- DRY RUN [social] · {post.title} · pictures in {folder} -----\n{post.caption}\n")
            return True
        try:
            if meta is None:
                send_draft(telegram, post)
                log.info("[social] Sent the draft: %s", post.title)
            else:
                post_by_bot(post, meta, social.publish, pictures_folder, config.website.url, telegram, now)
        except TelegramError as exc:
            log.error("[social] Could not reach the owner's chat about %s: %s", post.title, exc)
            return False
        for quote in quotes:
            storage.record_post(channel, quote, price_band(quote.price, config.rules.price_band_eur), now)
        return True

    # 1) The deals the free channel just posted, best first, up to the day's quota.
    left = social.max_posts_per_day - storage.count_posts(SOCIAL_CHANNEL, day_start)
    since = now - timedelta(days=config.rules.repost_cooldown_days)
    for deal in deals:
        if left <= 0:
            log.info("[social] Today's %d deal post(s) are already out", social.max_posts_per_day)
            break
        best = deal.best
        band = price_band(best.price, config.rules.price_band_eur)
        if storage.was_posted(SOCIAL_CHANNEL, best.origin, best.destination, best.depart_date, band, since):
            continue
        post = deal_post(deal, config, social_links, folder, photo=post_photo(deal.route.iata))
        if deliver(post, SOCIAL_CHANNEL, deal.quotes):
            left -= 1
            posted_today += 1
        else:
            failures += 1

    # 2) The week's top deals, once on the chosen weekday: what the website lists as deals.
    if (social.weekly_day is not None and local_now.weekday() == social.weekly_day
            and storage.count_posts(WEEKLY_CHANNEL, day_start) == 0):
        top = [offer for offer in build_offers(config, storage, now) if offer.is_deal][: social.weekly_count]
        if len(top) < WEEKLY_MIN_DEALS:
            log.info("[social] Only %d deal(s) on the website: no top-of-the-week post", len(top))
        else:
            photos = {offer.route.iata: post_photo(offer.route.iata) for offer in top}
            post = weekly_post(top, config, folder, photos=photos, now=local_now)
            if deliver(post, WEEKLY_CHANNEL, [offer.best for offer in top]):
                posted_today += 1
            else:
                failures += 1

    # 3) A city guide, when the day had no post and the clock has passed filler_hour.
    if (social.filler_hour is not None and local_now.hour >= social.filler_hour and posted_today == 0
            and storage.count_posts(GUIDE_CHANNEL, day_start) == 0):
        choice = choose_guide(config, storage)
        page = page_url(config, config.language, choice[0].slug) if choice else None
        if choice is None or not page:
            log.info("[social] No city guide to post (needs a guide in %s and website.url)", config.language)
        else:
            guide, iata = choice
            price = city_price(build_offers(config, storage, now), guide.city)
            post = guide_post(guide, iata, price, config, folder, photo=post_photo(iata), page_url=page)
            # Remembered as a "post" of the city's route on this day, so the cities take turns.
            marker_quote = Quote(origin=config.origin, destination=iata, depart_date=local_now.date(),
                                 price=price or 0, airline="", transfers=0, duration_min=None, link=None,
                                 fetched_at=now)
            if not deliver(post, GUIDE_CHANNEL, [marker_quote]):
                failures += 1

    if not dry_run:
        shutil.rmtree(folder, ignore_errors=True)
    return 1 if failures else 0


def choose_guide(config: Config, storage: Storage) -> tuple[Guide, str] | None:
    """The city guide to post next, and the route (airport code) to show for it.

    Cities with a guide take turns: a city never posted comes first (in the order of
    config.yaml's routes), then the one posted longest ago. None without any guide.
    """
    guides = site_guides(config, config.language)
    if not guides:
        return None
    last = storage.last_posts(GUIDE_CHANNEL)
    cities = []   # (when last posted, position in config.yaml, city, first airport)
    for position, route in enumerate(config.routes):
        if route.city in guides and route.city not in [city for _, _, city, _ in cities]:
            codes = [r.iata for r in config.routes if r.city == route.city]
            posted = max((last[code] for code in codes if code in last), default=None)
            cities.append((posted or datetime.min.replace(tzinfo=timezone.utc), position, route.city, route.iata))
    _, _, city, iata = min(cities)
    return guides[city], iata


def city_price(offers: list[Deal], city: str) -> float | None:
    """The cheapest current price to a city among the website's offers (any of its airports)."""
    prices = [offer.best.price for offer in offers if offer.route.city == city]
    return min(prices) if prices else None


def post_by_bot(post: Post, meta: MetaClient, platforms: tuple[str, ...], pictures_folder: Path,
                site_url: str, telegram: TelegramClient, now: datetime) -> list[tuple[str, str | None | MetaError]]:
    """Put the pictures on the site, post on each platform, report to the owner.

    A post that failed is also sent as a draft, so the owner can make it by hand.
    Returns what happened per post (see social.post_on_platforms).
    """
    feed_urls, story_url = put_pictures_online(post, pictures_folder, site_url, now)
    outcomes = post_on_platforms(post, meta, platforms, feed_urls, story_url)
    for what, outcome in outcomes:
        if isinstance(outcome, MetaError):
            log.error("[social] %s failed for %s: %s", what, post.title, outcome)
        else:
            log.info("[social] %s done for %s", what, post.title)
    telegram.send_message(report(post, outcomes))
    if failed(outcomes):
        send_draft(telegram, post)
    return outcomes


def in_quiet_hours(local_time: time, quiet_hours: tuple[time, time] | None) -> bool:
    if not quiet_hours:
        return False
    start, end = quiet_hours
    if start <= end:
        return start <= local_time < end
    return local_time >= start or local_time < end  # window crosses midnight, e.g. 23:00-08:00


if __name__ == "__main__":
    sys.exit(main())
