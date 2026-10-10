"""FlyFromTirana entry point: scan prices -> store -> detect deals -> post.

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
import sys
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
from src.links import LinkBuilder
from src.photos import post_photo
from src.scanner import PriceScanner, Quote, RouteFetchError, ScannerError
from src.storage import Storage
from src.telegram import TelegramClient, TelegramError

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


def in_quiet_hours(local_time: time, quiet_hours: tuple[time, time] | None) -> bool:
    if not quiet_hours:
        return False
    start, end = quiet_hours
    if start <= end:
        return start <= local_time < end
    return local_time >= start or local_time < end  # window crosses midnight, e.g. 23:00-08:00


if __name__ == "__main__":
    sys.exit(main())
