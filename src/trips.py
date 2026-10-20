"""Trips only the owner hears about (trip_watches in config.yaml): "tell me when Tirana ⇄ Memmingen
is cheap for 3-4 days before Christmas, with a weekend in it".

After every full scan (main.run, every 3 hours) each watch looks at the cached prices for its
dates, two ways:

  - a flight out and a flight back, each a one-way ticket (how Wizz Air and Ryanair sell them);
  - return tickets, out and back in one booking (fewer of them: few people search those).

A trip fits when it leaves on or after earliest_departure, is back by latest_return, lasts
min_days to max_days (counting the day out and the day back: Friday to Sunday is 3 days) and,
with weekend: true, has a Saturday and its Sunday in it.

When the cheapest trip that fits costs max_price or less, the owner's chat (TELEGRAM_DRAFTS_CHAT_ID,
or else TELEGRAM_ALERT_CHAT_ID) gets the best few, each with an Aviasales search for its dates.
The same price band again only after repost_cooldown_days; a cheaper band is news straight away.
That is remembered in posted_deals, as the "channel" trip:<name>. Nothing goes out in quiet hours
(it waits for the morning) and a watch whose dates have passed does nothing. The destination
needn't be one of the routes: nothing about it goes to the channels or the website.

    python -m src.trips      # what the watches find now: every trip that fits, and the
                             # message it would send (sends nothing, remembers nothing)
"""
from __future__ import annotations

import html
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.config import PROJECT_ROOT, Config, ConfigError, TripWatch, load_config, load_secrets
from src.deals import price_band
from src.formatter import format_price
from src.scanner import PriceScanner, Quote, ReturnFare, RouteFetchError, ScannerError
from src.storage import Storage
from src.telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)

MAX_TRIPS = 3   # how many trips one message lists, the cheapest first
SATURDAY = 5    # date.weekday(): Monday = 0


@dataclass(frozen=True)
class Trip:
    """One way to make the trip: out on `depart`, back on `back`, `price` for both flights."""

    depart: date
    back: date
    price: float
    airline_out: str                 # IATA codes; the same for a return ticket
    airline_back: str
    stops: int = 0                   # stops out + stops back; 0 = direct both ways
    out_price: float | None = None   # the two one-way tickets; both None for a return ticket
    back_price: float | None = None

    @property
    def days(self) -> int:
        return (self.back - self.depart).days + 1


def watch_channel(watch: TripWatch) -> str:
    """The name a watch's messages go under in posted_deals."""
    return f"trip:{watch.name}"


def send_trip_watches(config: Config, storage: Storage, scanner: PriceScanner, *,
                      owner: TelegramClient | None, now: datetime, quiet: bool, dry_run: bool = False,
                      output: Callable[[str], None] = print) -> int:
    """Check every watch and message the owner about the ones with news. Returns 1 if Telegram
    couldn't be reached, else 0.

    owner: the owner's chat (None in a dry run, or when no chat is set). quiet: it is quiet hours
    (nothing is checked or sent; a dry run still shows what would go out).
    """
    if not config.trip_watches or (quiet and not dry_run):
        return 0
    if owner is None and not dry_run:
        log.warning("[trips] trip_watches are set, but neither TELEGRAM_DRAFTS_CHAT_ID nor "
                    "TELEGRAM_ALERT_CHAT_ID is: nobody to tell")
        return 0
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    failures = 0
    for watch in config.trip_watches:
        trips = [trip for trip in watch_trips(watch, scanner, config.origin, today, now)
                 if watch.max_price is None or trip.price <= watch.max_price]
        if not trips:
            log.info("[trips] %s: no trip at the price yet", watch.name)
            continue
        best = trips[0]
        channel = watch_channel(watch)
        band = price_band(best.price, config.rules.price_band_eur)
        since = now - timedelta(days=config.rules.repost_cooldown_days)
        if storage.was_posted(channel, config.origin, watch.destination, None, band, since):
            log.info("[trips] %s: cheapest €%s, already sent", watch.name, format_price(best.price))
            continue
        text = trip_text(watch, trips, config)
        if dry_run:
            output(f"\n----- DRY RUN [trip watch: {watch.name}] -----\n{text}\n")
            continue
        try:
            owner.send_message(text)
        except TelegramError as exc:
            log.error("[trips] Could not send %s: %s", watch.name, exc)
            failures += 1
            continue
        # One row for the message: the cheapest trip's day out, at both flights' price.
        storage.record_post(channel, Quote(config.origin, watch.destination, best.depart, best.price,
                                           best.airline_out, best.stops, None, None, now), band, now)
        log.info("[trips] Sent %s: from €%s", watch.name, format_price(best.price))
    return 1 if failures else 0


def watch_trips(watch: TripWatch, scanner: PriceScanner, origin: str, today: date, now: datetime) -> list[Trip]:
    """Every trip that fits the watch, at its cheapest, the cheapest first. Asks the price API
    (3 calls a month of dates); a direction it can't get is left out, never fatal."""
    first_day = max(watch.earliest_departure, today + timedelta(days=1))
    if first_day + timedelta(days=watch.min_days - 1) > watch.latest_return:
        log.info("[trips] %s: its dates have passed", watch.name)
        return []
    last_day = watch.latest_return

    def ask(what: str, fetch: Callable[[], list]) -> list:
        try:
            return fetch()
        except RouteFetchError as exc:
            log.warning("[trips] %s: no %s (%s)", watch.name, what, exc)
            return []

    outbound = ask("prices out", lambda: scanner.fetch_dates(origin, watch.destination, first_day, last_day, now))
    inbound = ask("prices back", lambda: scanner.fetch_dates(watch.destination, origin, first_day, last_day, now))
    fares = ask("return tickets", lambda: scanner.fetch_return_fares(origin, watch.destination, first_day, last_day))
    return find_trips(watch, outbound, inbound, fares)


def find_trips(watch: TripWatch, outbound: list[Quote], inbound: list[Quote], fares: list[ReturnFare]) -> list[Trip]:
    """The trips that fit the watch, from one-way prices and return tickets: the cheapest per
    pair of dates, the cheapest first."""
    best: dict[tuple[date, date], Trip] = {}

    def keep(trip: Trip) -> None:
        dates = (trip.depart, trip.back)
        if dates not in best or trip.price < best[dates].price:
            best[dates] = trip

    for out in outbound:
        for back in inbound:
            if fits(watch, out.depart_date, back.depart_date):
                keep(Trip(out.depart_date, back.depart_date, out.price + back.price, out.airline, back.airline,
                          out.transfers + back.transfers, out.price, back.price))
    for fare in fares:
        if fits(watch, fare.depart_date, fare.return_date):
            keep(Trip(fare.depart_date, fare.return_date, fare.price, fare.airline, fare.airline, fare.transfers))
    return sorted(best.values(), key=lambda trip: (trip.price, trip.depart))


def fits(watch: TripWatch, depart: date, back: date) -> bool:
    """Is out on `depart`, back on `back` a trip the watch asks for?"""
    days = (back - depart).days + 1
    if depart < watch.earliest_departure or back > watch.latest_return:
        return False
    if not watch.min_days <= days <= watch.max_days:
        return False
    return not watch.weekend or has_weekend(depart, back)


def has_weekend(first: date, last: date) -> bool:
    """Is there a Saturday from first to last whose Sunday is in there too?"""
    return any((first + timedelta(days=n)).weekday() == SATURDAY for n in range((last - first).days))


def trip_text(watch: TripWatch, trips: list[Trip], config: Config) -> str:
    """The owner's message (Telegram HTML): the cheapest trips, each linked to its Aviasales search."""
    city = html.escape(watch.city)
    lines = [f"🧳 <b>{html.escape(watch.name)}</b>",
             f"Tirana ⇄ {city} from <b>€{format_price(trips[0].price)}</b>, both flights", ""]
    for trip in trips[:MAX_TRIPS]:
        when = f"{day_label(trip.depart)} → {day_label(trip.back)}"
        if trip.out_price is None:
            how = "return ticket"
        else:
            how = f"€{format_price(trip.out_price)} out + €{format_price(trip.back_price)} back"
        airlines = [config.airlines.get(code, code) for code in (trip.airline_out, trip.airline_back) if code]
        if len(set(airlines)) == 1:
            how += f" · {html.escape(airlines[0])}"
        elif airlines:
            how += f" · {html.escape(airlines[0])} out, {html.escape(airlines[1])} back"
        if trip.stops:
            how += f" · {trip.stops} stop{'s' if trip.stops > 1 else ''}"
        link = html.escape(search_link(config, watch.destination, trip), quote=True)
        lines.append(f"• <a href=\"{link}\">{when}</a> ({trip.days} days): <b>€{format_price(trip.price)}</b>, {how}")
    lines += ["", "<i>Prices people found on Aviasales lately: the link shows today's.</i>"]
    return "\n".join(lines)


def day_label(day: date) -> str:
    """'Fri 18 Dec'."""
    return f"{day:%a} {day.day} {day:%b}"


def search_link(config: Config, destination: str, trip: Trip) -> str:
    """Aviasales' search for the trip's dates, out and back, 1 adult. No affiliate marker: it is
    the owner's own booking."""
    path = f"{config.origin}{trip.depart:%d%m}{destination}{trip.back:%d%m}1"
    return f"{config.links.flight_base_url}/search/{path}?currency={config.currency}"


def main() -> int:
    """Print what each watch finds now. Sends nothing and remembers nothing."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
        secrets = load_secrets(dry_run=True)
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return 2
    if not config.trip_watches:
        print("No trip_watches in config.yaml")
        return 0
    scanner = PriceScanner(secrets.travelpayouts_token, currency=config.currency, market=config.market,
                           request_delay_seconds=config.request_delay_seconds)
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    for watch in config.trip_watches:
        try:
            trips = watch_trips(watch, scanner, config.origin, today, now)
        except ScannerError as exc:
            log.error("%s", exc)
            return 1
        limit = "any price" if watch.max_price is None else f"up to €{format_price(watch.max_price)}"
        print(f"\n{watch.name} ({limit}): {len(trips)} trip(s) that fit")
        for trip in trips:
            print(f"  {day_label(trip.depart)} -> {day_label(trip.back)}: €{format_price(trip.price)}")
        within = [trip for trip in trips if watch.max_price is None or trip.price <= watch.max_price]
        if within:
            print("\nThe message:\n" + trip_text(watch, within, config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
