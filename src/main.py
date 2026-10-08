"""FlyFromTirana entry point: scan prices -> store -> detect deals -> post.

Run from the repo root:
    python -m src.main --dry-run                     # print posts instead of sending them
    python -m src.main --dry-run --routes BGY,VIE -v # a couple of routes, debug logging
    python -m src.main                               # the real thing (what GitHub Actions runs)

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

from src.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Config, ConfigError, Route, Secrets, load_config, load_secrets
from src.deals import Deal, find_route_deal, price_band, rank_deals
from src.formatter import format_post
from src.links import LinkBuilder
from src.scanner import PriceScanner, Quote, RouteFetchError, ScannerError
from src.storage import Storage
from src.telegram import TelegramClient, TelegramError

log = logging.getLogger("flyfromtirana")

# Name used in the posted_deals table. Future channels (Instagram, premium,
# English) get their own name, so each one dedupes independently.
CHANNEL = "telegram"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # urllib3's debug lines include request URLs, and the Telegram URL contains the bot token.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(PROJECT_ROOT / ".env")  # does nothing if there's no .env (e.g. on GitHub)
    try:
        config = load_config(args.config)
        if args.ignore_quiet_hours:
            config = replace(config, quiet_hours=None)
        secrets = load_secrets(dry_run=args.dry_run)
        only_routes = {code.strip().upper() for code in args.routes.split(",")} if args.routes else None
        return run(config, secrets, dry_run=args.dry_run, only_routes=only_routes)
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
    session: requests.Session | None = None,
    now: datetime | None = None,
    output: Callable[[str], None] = print,
) -> int:
    """One full scan. `session`, `now` and `output` are only passed in by tests."""
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo(config.timezone))
    session = session or requests.Session()

    routes = select_routes(config.routes, only_routes)
    links = LinkBuilder(config.links, secrets.travelpayouts_marker, origin=config.origin)
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
    telegram = None if dry_run else TelegramClient(secrets.telegram_bot_token, secrets.telegram_channel_id, session)

    log.info("Scanning %d route(s) from %s%s", len(routes), config.origin, " (dry run)" if dry_run else "")
    with Storage(config.db_path) as storage:
        # 1) Fetch and store prices
        outbound, inbound = scan_routes(scanner, storage, config, routes, local_now.date(), now)
        if not outbound:
            log.error("Every route failed to fetch, nothing to do")
            return 1

        # 2) Find deals, best first
        deals = []
        for route in routes:
            if route.iata not in outbound:
                continue
            deal = find_route_deal(route, config.origin, outbound[route.iata], inbound.get(route.iata, []),
                                   storage, config.rules, now, CHANNEL)
            if deal:
                deals.append(deal)
        best = rank_deals(deals, config.max_posts_per_run)
        log.info("%d route(s) with new deals, posting the best %d", len(deals), len(best))

        # 3) Post them
        exit_code = publish(best, config=config, links=links, storage=storage, telegram=telegram,
                            dry_run=dry_run, now=now, local_now=local_now, output=output)

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
                today: date, now: datetime) -> tuple[dict[str, list[Quote]], dict[str, list[Quote]]]:
    """Fetch and save prices for every route, both directions.

    Returns (outbound, inbound), keyed by destination code. Routes that failed
    are left out; a route with no prices maps to an empty list.
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
        storage.save_quotes(outbound[route.iata] + inbound[route.iata])
    return outbound, inbound


def publish(deals: list[Deal], *, config: Config, links: LinkBuilder, storage: Storage,
            telegram: TelegramClient | None, dry_run: bool, now: datetime, local_now: datetime,
            output: Callable[[str], None]) -> int:
    """Send (or, in a dry run, print) each deal. Returns 1 if any post failed, else 0."""
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
        text = format_post(deal, links, language=config.language,
                           channel_handle=config.channel_handle, airline_names=config.airlines)
        if dry_run:
            output(f"\n----- DRY RUN · {deal.summary()} -----\n{text}\n")
            continue
        try:
            message_id = telegram.send_message(text)
        except TelegramError as exc:
            log.error("Could not post %s: %s", deal.summary(), exc)
            failures += 1
            continue
        # Remember every date shown in the post, so none of them is reposted too soon.
        for quote in deal.quotes:
            storage.record_post(CHANNEL, quote, price_band(quote.price, config.rules.price_band_eur), now)
        log.info("Posted %s (message %s)", deal.summary(), message_id)
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
