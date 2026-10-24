"""FlyFromTirana entry point: scan prices -> store -> detect deals -> post -> social drafts.

After a full scan it also sends the price alerts people set with the bot (src/alerts.py) and
checks the owner's own trip watches (src/trips.py).

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

from src.alerts import send_alerts
from src.bot import alert_link
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
from src.growth import whatsapp_link
from src.guides import Guide
from src.links import LinkBuilder
from src.meta import MetaClient, MetaError
from src.photos import post_photo
from src.reels import pick_music
from src.scanner import PriceScanner, Quote, RouteFetchError, ScannerError
from src.social import (GUIDE_CHANNEL, PICTURES_FOLDER, SOCIAL_CHANNEL, WEEKLY_CHANNEL, WEEKLY_MIN_DEALS, Post,
                        deal_post, failed, guide_post, post_on_platforms, prune_pictures, put_pictures_online,
                        report, send_draft, weekly_post)
from src.storage import Storage
from src.telegram import TelegramClient, TelegramError
from src.trips import send_trip_watches
from src.website import build_offers, page_url, site_guides

log = logging.getLogger("flyfromtirana")
EVER = datetime(2000, 1, 1, tzinfo=timezone.utc)   # "since the start", for counting every post ever made


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
        max_price_age_days=config.max_price_age_days,
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

        def social_post() -> int:
            """The Instagram/Facebook post whose time has come (social.post_times), if any: the
            best deal on the website, posted by the bot or sent to the owner as a draft."""
            return publish_social(telegram=telegram_for(secrets.telegram_drafts_chat_id),
                                  drafts_chat=bool(secrets.telegram_drafts_chat_id),
                                  meta=meta_client(config, secrets, session, dry_run=dry_run),
                                  marker=secrets.travelpayouts_marker, **common)

        def alerts(**options) -> int:
            """The price alerts people set with the bot (src/alerts.py)."""
            return send_alerts(config, storage, token=secrets.telegram_bot_token,
                               premium_chat=secrets.telegram_premium_channel_id, links=links, now=now,
                               quiet=in_quiet_hours(local_now.time(), config.quiet_hours), dry_run=dry_run,
                               session=session, output=output, **options)

        exit_code = 0
        if premium.enabled:
            deals = find_deals(routes, outbound, inbound, storage, config.origin, premium.rules, now,
                               PREMIUM_CHANNEL)
            best = rank_deals(deals, premium.max_posts_per_run)
            log.info("[premium] %d route(s) with new deals, posting the best %d", len(deals), len(best))
            exit_code |= publish(best, channel=PREMIUM_CHANNEL, template=f"{config.language}_premium",
                                 telegram=telegram_for(secrets.telegram_premium_channel_id), **common)
        if premium_only:
            # Premium members' alerts straight away, from these (unsaved) prices. The quick scan
            # keeps the posting times too: a full run only comes every full_run_every_minutes.
            exit_code |= alerts(premium_only=True, scanned=(outbound, inbound))
            return exit_code | social_post()

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
                             premium_link=premium.join_link if premium.enabled else None, share=True, **common)

        # 3) The Instagram/Facebook post, when its time has come, the price alerts, and the
        #    trips only the owner hears about (trip_watches)
        exit_code |= social_post()
        exit_code |= alerts()
        owner = secrets.telegram_drafts_chat_id   # the owner's own chat with the bot
        exit_code |= send_trip_watches(config, storage, scanner, owner=telegram_for(owner) if owner else None,
                                       now=now, quiet=in_quiet_hours(local_now.time(), config.quiet_hours),
                                       dry_run=dry_run, output=output)

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
            local_now: datetime, output: Callable[[str], None], premium_link: str | None = None,
            share: bool = False) -> int:
    """Send (or, in a dry run, print) each deal to one channel. Returns 1 if any post failed, else 0.

    share: give each post a "send it to a friend on WhatsApp" link (the free channel's posts:
    premium members pay to have the deals first).
    """
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
                           website_url=config.website.url, photo=photo,
                           share_link=whatsapp_link(deal, config, config.language) if share else None,
                           alert_link=alert_link(config, deal.route.iata))
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


def publish_social(*, config: Config, links: LinkBuilder, storage: Storage, telegram: TelegramClient | None,
                   drafts_chat: bool, meta: MetaClient | None, marker: str, dry_run: bool, now: datetime,
                   local_now: datetime, output: Callable[[str], None]) -> int:
    """The Instagram/Facebook post of this run, when one is due (src/social.py).

    The posts go out at social.post_times (local), one per time, at the first run
    at or after it: so they are hours apart, never two in a row. What goes out:
      1. on social.weekly_day, the day's first post is the week's top deals;
      2. otherwise the best deal the website shows that these accounts haven't
         had: not a city already posted today, and not a city posted at this
         price or cheaper within repost_cooldown_days, whatever the date or airport;
      3. at the day's last time, when the day had no post at all, a city guide.
    With `meta` the bot posts and reports to the owner; a post that fails is sent
    as a draft, so it can go out by hand. Either way the post counts as made.
    Returns 1 if the owner's chat could not be reached, else 0. A problem here
    never stops the Telegram posts: they are already out. A dry run shows the
    post that would go out next, whatever the clock says.
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

    def posts_since(moment: datetime) -> int:
        return sum(storage.count_posts(name, moment) for name in (SOCIAL_CHANNEL, WEEKLY_CHANNEL, GUIDE_CHANNEL))

    begun = posting_time(social.post_times, local_now)
    if dry_run:
        slot_start, last_of_day = day_start, True   # pretend: show what would go out next
    elif begun is None:
        return 0   # before the day's first posting time
    else:
        slot_start, last_of_day = begun
        if posts_since(slot_start):
            return 0   # this time's post is out (quietly: the quick scan comes by every 15 minutes)
    posted_today = posts_since(day_start)

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

    # What the website lists as deals right now, best saving first (the free channel's rules,
    # and like the free channel it leaves out what premium is still alone with).
    deals = [offer for offer in build_offers(config, storage, now) if offer.is_deal]
    post: Post | None = None
    channel, quotes = "", []

    # 1) On the weekly day, the first post is the week's top deals.
    if (social.weekly_day is not None and local_now.weekday() == social.weekly_day
            and storage.count_posts(WEEKLY_CHANNEL, day_start) == 0):
        top = deals[: social.weekly_count]
        if len(top) < WEEKLY_MIN_DEALS:
            log.info("[social] Only %d deal(s) on the website: no top-of-the-week post", len(top))
        else:
            photos = {offer.route.iata: post_photo(offer.route.iata) for offer in top}
            post = weekly_post(top, config, folder, photos=photos, now=local_now)
            channel, quotes = WEEKLY_CHANNEL, [offer.best for offer in top]

    # 2) Otherwise the best deal these accounts haven't had.
    if post is None:
        deal = choose_deal(deals, config, storage, now=now, day_start=day_start)
        if deal is not None:
            # Each Reel gets the next track: how many deal posts came before it decides which.
            music = pick_music(storage.count_posts(SOCIAL_CHANNEL, EVER)) if social.reels else None
            post = deal_post(deal, config, social_links, folder, photo=post_photo(deal.route.iata), music=music)
            channel, quotes = SOCIAL_CHANNEL, deal.quotes

    # 3) Otherwise, at the day's last time, a city guide when the day had no post at all.
    if post is None and last_of_day and posted_today == 0:
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
            channel, quotes = GUIDE_CHANNEL, [marker_quote]

    failures = 0
    if post is None:
        # Said once per posting time, not at every quick scan after it.
        first_look = dry_run or local_now - slot_start < timedelta(minutes=20)
        log.log(logging.INFO if first_look else logging.DEBUG,
                "[social] Nothing to post at %s: no deal on the website these accounts haven't had",
                local_now.strftime("%H:%M"))
    elif not deliver(post, channel, quotes):
        failures = 1

    if not dry_run:
        shutil.rmtree(folder, ignore_errors=True)
    return 1 if failures else 0


def posting_time(post_times: tuple[time, ...], local_now: datetime) -> tuple[datetime, bool] | None:
    """The posting time now in force: when it began today (local), and whether it is the day's last.

    None before the day's first time. A time stays in force until the next one
    begins, so a post that missed its run (a redeploy, say) still goes out.
    """
    begun = [local_now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0) for at in post_times]
    begun = [moment for moment in begun if moment <= local_now]
    if not begun:
        return None
    return begun[-1], len(begun) == len(post_times)


def choose_deal(deals: list[Deal], config: Config, storage: Storage, *, now: datetime,
                day_start: datetime) -> Deal | None:
    """The best of the website's deals (best first) the social accounts haven't had, or None.

    Skipped: a city already posted today, and a city posted at this price band or a
    cheaper one within repost_cooldown_days, whatever the date or the airport: the post
    shows a city at a price, so to a follower another date (or Malpensa instead of
    Bergamo) at the same price is the same post. A cheaper price is news again.
    """
    cities = {route.iata: route.city for route in config.routes}
    cities_today = {cities.get(code, code) for code, at in storage.last_posts(SOCIAL_CHANNEL).items()
                    if at >= day_start}
    since = now - timedelta(days=config.rules.repost_cooldown_days)
    for deal in deals:
        city, best = deal.route.city, deal.best
        if city in cities_today:
            log.debug("[social] %s: %s was posted today already", deal.route.iata, city)
            continue
        band = price_band(best.price, config.rules.price_band_eur)
        airports = [code for code, name in cities.items() if name == city]
        if any(storage.was_posted(SOCIAL_CHANNEL, best.origin, code, None, band, since) for code in airports):
            log.debug("[social] %s at €%.0f was posted in the last %d days", city, best.price,
                      config.rules.repost_cooldown_days)
            continue
        return deal
    return None


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
    online = put_pictures_online(post, pictures_folder, site_url, now)
    outcomes = post_on_platforms(post, meta, platforms, online)
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
