"""Builds the website: one static page with the cheapest price to every destination.

    python -m src.website               # writes docs/index.html (and docs/.nojekyll)
    python -m src.website --out /tmp/x  # write it somewhere else, e.g. to look at it locally

It only reads data/prices.db (no API calls), so it can run at any time. On the
server src/scheduler.py runs it after every scan, and the `web` container
serves the docs/ folder as the site.

Per route the page shows: the cheapest date from the newest scan (plus other
dates about as cheap), the usual price, the cheapest flight back and a booking
link. Routes that meet the free channel's deal rules come first, as "Ofertat
e momentit". Wording and layout live in templates/site.html (the page),
templates/site_row.html (one route in the list), site_card.html (one photo card
in the destinations carousel) and site_hero.html (one hero banner), which follow
the same {NAME} and [[optional]] rules as the post templates (see src/formatter.py).

Photos live in assets/img (dest/<iata>.webp per route, services/<partner>.webp),
with their authors and licences in assets/img/credits.json. Every build copies
them next to the page and lists the credits in the footer.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Config, ConfigError, Route, load_config
from src.deals import Deal, cheapest_return, deal_reason
from src.formatter import WORDS, build_values, format_dates, format_price, load_template, render
from src.links import LinkBuilder
from src.storage import Storage

log = logging.getLogger("flyfromtirana.website")

# A route whose newest prices are older than this is left off the page (the API
# kept failing for it, say). Stale prices would only disappoint.
STALE_AFTER = timedelta(days=2)

# How many deals become banners in the hero carousel at the top of the page.
HERO_SLIDES = 3

# Photos for the page: assets/img/dest/<iata>.webp for a route's card,
# assets/img/services/<partner>.webp for the travel services, and credits.json
# with the author and licence of each one. A route without a photo simply gets
# its country-coloured card instead.
ASSETS_DIR = PROJECT_ROOT / "assets"
CREDITS_FILE = ASSETS_DIR / "img" / "credits.json"

# Country names for the photo cards ("Vienna, Austria"), keyed by the code taken from
# the route's flag. Place names are in English, like the city names in config.yaml.
# A country missing here just shows the city on its own.
COUNTRY_NAMES = {
    "it": "Italy", "gr": "Greece", "gb": "United Kingdom", "de": "Germany", "at": "Austria",
    "ch": "Switzerland", "es": "Spain", "tr": "Turkey", "be": "Belgium", "fr": "France",
    "nl": "Netherlands", "pt": "Portugal", "hr": "Croatia", "hu": "Hungary", "pl": "Poland",
    "cz": "Czechia", "dk": "Denmark", "se": "Sweden", "no": "Norway", "ie": "Ireland",
    "mt": "Malta", "cy": "Cyprus", "ae": "United Arab Emirates",
}

# What the credits list calls the service photos.
SERVICE_PHOTO_NAMES = {
    "esim": "eSIM", "insurance": "Sigurim udhëtimi", "car_rental": "Makinë me qira",
    "compensation": "Kompensim", "hotel": "Hotele",
}

# The one sentence the code has to produce itself; all other wording is in the templates.
TEXTS = {
    "sq": {"empty": "Ende nuk ka çmime. Skanimi i parë përfundon brenda pak orësh."},
    "en": {"empty": "No prices yet. The first scan finishes within a few hours."},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the FlyFromTirana website from the price database.")
    parser.add_argument("--out", type=Path, help="output folder (default: website.output_dir in config.yaml)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv(PROJECT_ROOT / ".env")  # does nothing if there's no .env (e.g. on the server)
    try:
        config = load_config(args.config)
        marker = os.environ.get("TRAVELPAYOUTS_MARKER", "").strip()
        if not marker:
            log.warning("TRAVELPAYOUTS_MARKER is not set: booking links on the site carry no affiliate marker")
        path = build_site(config, marker, out_dir=args.out)
        log.info("Website written to %s", path)
        return 0
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return 2
    except Exception:
        log.exception("Unexpected error")
        return 1


def build_site(config: Config, marker: str, *, out_dir: Path | None = None,
               now: datetime | None = None) -> Path:
    """Read the database, render the page, write it. Returns the path of index.html."""
    now = now or datetime.now(timezone.utc)
    out_dir = out_dir or config.website.output_dir
    # Same link builder as the posts, but with the site's own SubID, so the
    # Travelpayouts stats show which clicks came from the website.
    links = LinkBuilder(replace(config.links, sub_id=config.website.sub_id), marker,
                        origin=config.origin, currency=config.currency)
    links.validate()
    with Storage(config.db_path) as storage:
        offers = build_offers(config, storage, now)
        updated_at = storage.last_fetched_at()
    page = render_site(offers, config, links, updated_at=updated_at, now=now)

    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    copy_photos(out_dir)
    (out_dir / ".nojekyll").touch()  # harmless elsewhere; GitHub Pages needs it to serve the files as they are
    log.info("%d route(s) on the page, %d of them deals", len(offers), sum(o.is_deal for o in offers))
    return index


def copy_photos(out_dir: Path) -> None:
    """Put assets/img next to the page as img/ (the credits file stays behind)."""
    source = ASSETS_DIR / "img"
    if source.is_dir():
        shutil.copytree(source, out_dir / "img", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.json"))


def build_offers(config: Config, storage: Storage, now: datetime) -> list[Deal]:
    """One Deal per route that has current prices; its reason is None when it isn't a deal.

    Deals come first, biggest saving first, then the other routes, cheapest first.
    """
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    rules = config.rules
    offers = []
    for route in config.routes:
        outbound = [q for q in storage.latest_quotes(config.origin, route.iata) if q.depart_date > today]
        if not outbound:
            continue
        if outbound[0].fetched_at < now - STALE_AFTER:
            log.info("%s: newest prices are from %s, leaving it off the page", route.iata, outbound[0].fetched_at)
            continue
        median, samples = storage.route_median(config.origin, route.iata,
                                               since=now - timedelta(days=rules.median_window_days))
        if samples < rules.min_samples_for_median:
            median = None
        by_price = sorted(outbound, key=lambda q: q.price)
        best = by_price[0]
        reason = deal_reason(best.price, median, route.absolute_threshold_eur,
                             rules.discount_pct, rules.threshold_min_discount_pct)
        # Same bundling as a post: the cheapest date plus other dates about as cheap.
        max_price = best.price * (1 + rules.date_price_tolerance_pct / 100)
        shown = [q for q in by_price if q.price <= max_price][: rules.max_dates_per_post]
        inbound = storage.latest_quotes(route.iata, config.origin)
        offers.append(Deal(route=route, quotes=shown, median=median, reason=reason,
                           return_quote=cheapest_return(best, inbound, rules)))
    deals = sorted((o for o in offers if o.is_deal), key=lambda o: o.score)
    others = sorted((o for o in offers if not o.is_deal), key=lambda o: o.best.price)
    return deals + others


def render_site(offers: list[Deal], config: Config, links: LinkBuilder, *,
                updated_at: datetime | None, now: datetime) -> str:
    """The whole page as HTML."""
    language = config.language
    if language not in TEXTS or language not in WORDS:
        raise ConfigError(f"Unsupported language {language!r}; add it to TEXTS in src/website.py")
    row_template = load_site_template("site_row.html")
    deal_rows = [render(row_template, row_values(o, config, links)) for o in offers if o.is_deal]
    other_rows = [render(row_template, row_values(o, config, links)) for o in offers if not o.is_deal]
    # The hero banners at the top of the page: the three best deals, one slide each.
    hero_template = load_site_template("site_hero.html")
    hero_slides = [render(hero_template, row_values(o, config, links)) for o in offers if o.is_deal][:HERO_SLIDES]
    # The destinations carousel: one photo card per route, deals first (the order of offers).
    card_template = load_site_template("site_card.html")
    dest_cards = [render(card_template, row_values(o, config, links)) for o in offers]

    local_now = now.astimezone(ZoneInfo(config.timezone))
    today = local_now.date()
    premium = config.premium
    premium_on = premium.enabled and bool(premium.join_link)
    # The best deal (offers start with the deals, biggest saving first) gets a chip on the
    # hero's first slide, so a phone shows a real price before anyone swipes.
    top = offers[0] if offers and offers[0].is_deal else None
    text = {
        "BRAND": config.brand,
        "CHANNEL": config.channel_handle,
        "LOOKAHEAD_DAYS": str(config.lookahead_days),
        "UPDATED": format_updated(updated_at, config) if updated_at else None,
        "EMPTY": TEXTS[language]["empty"] if not offers else None,
        "PREMIUM_HOURS": f"{premium.free_delay_hours:g}" if premium_on else None,  # :g turns 6.0 into "6"
        "YEAR": str(local_now.year),
        # For the headline ("Sot që nga €14") and the badges next to the section headings.
        "MIN_PRICE": format_price(min(o.best.price for o in offers)) if offers else None,
        "DEAL_COUNT": str(len(deal_rows)) if deal_rows else None,
        "ROUTE_COUNT": str(len(offers)) if offers else None,
        "TOP_CITY": city_label(top.route) if top else None,
        "TOP_PRICE": format_price(top.best.price) if top else None,
        "TOP_SAVING": saving_percent(top) if top else None,
        # The search card's date picker only allows dates the scanner actually covers.
        "DATE_MIN": (today + timedelta(days=1)).isoformat(),
        "DATE_MAX": (today + timedelta(days=config.lookahead_days)).isoformat(),
    }
    urls = {
        "TELEGRAM_URL": telegram_url(config.channel_handle),
        "PREMIUM_LINK": premium.join_link if premium_on else None,
        "SITE_URL": config.website.url or None,
    }
    # Footer links (eSIM, insurance, ...). A partner URL template may mention a city
    # and dates (that's for per-post hotel links); here a generic route and today stand in.
    route = offers[0].route if offers else config.routes[0]
    for name in links.partner_names:
        urls[f"{name.upper()}_LINK"] = links.partner(name, route, today, today + timedelta(days=3))

    values = {key: html.escape(value, quote=False) if value else None for key, value in text.items()}
    values.update({key: html.escape(value, quote=True) if value else None for key, value in urls.items()})
    # These are HTML already, so they go in as they are.
    values["DEAL_ROWS"] = "\n".join(deal_rows) or None
    values["PRICE_ROWS"] = "\n".join(other_rows) or None
    values["HERO_SLIDES"] = "\n".join(hero_slides) or None
    values["DEST_CARDS"] = "\n".join(dest_cards) or None
    values["PHOTO_CREDITS"] = photo_credits(config)
    values["DESTINATION_OPTIONS"] = destination_options(offers)
    return render(load_site_template("site.html"), values) + "\n"


def load_site_template(name: str) -> str:
    """templates/<name> without its HTML comments.

    The comments are notes for whoever edits the template; stripping them here keeps
    them off the page (the row template would otherwise repeat its note once per route).
    """
    return re.sub(r"<!--.*?-->", "", load_template(name), flags=re.DOTALL)


def row_values(offer: Deal, config: Config, links: LinkBuilder) -> dict[str, str | None]:
    """Everything templates/site_row.html and site_hero.html can use: the post placeholders plus a few of their own."""
    values = build_values(offer, links, language=config.language, channel_handle=config.channel_handle,
                          airline_names=config.airlines)
    values.update({
        "IATA": offer.route.iata,
        "DEAL": "deal" if offer.is_deal else None,  # a CSS class; template parts using it vanish for other routes
        "SAVING": saving_percent(offer),
        "COUNTRY": country_code(offer.route.flag),   # "it" for 🇮🇹: the CSS class that picks the card's colours
        "BEST_DATE": offer.best.depart_date.isoformat(),  # "2026-11-21": the page's JavaScript pre-fills the date picker with it
        # For the photo cards: "14 Nën", "Austri" and the route's photo (None when there isn't one).
        "BEST_DAY": format_dates([offer.best.depart_date], WORDS[config.language]["months"]),
        "COUNTRY_NAME": COUNTRY_NAMES.get(country_code(offer.route.flag) or ""),
        "PHOTO": route_photo(offer.route.iata),
    })
    return values


def route_photo(iata: str) -> str | None:
    """'img/dest/vie.webp' when assets/img/dest/vie.webp exists, else None (the card shows its colours)."""
    path = f"img/dest/{iata.lower()}.webp"
    return path if (ASSETS_DIR / path).exists() else None


def photo_credits(config: Config) -> str | None:
    """The footer's list of photo credits (author, source and licence of each photo), as HTML.

    Most photos are CC BY or CC BY-SA, which ask for exactly this: who took it, where
    it comes from, which licence, and that it was changed (cropped and resized).
    """
    if not CREDITS_FILE.exists():
        return None
    names = {route.iata: city_label(route) for route in config.routes}
    names.update(SERVICE_PHOTO_NAMES)
    esc = html.escape
    items = []
    for photo in json.loads(CREDITS_FILE.read_text(encoding="utf-8")):
        if not (ASSETS_DIR / photo["file"]).exists():
            continue
        title = f'<a href="{esc(photo["source_url"])}">{esc(photo["title"])}</a>' if photo["source_url"] else esc(photo["title"])
        creator = esc(photo["creator"])
        if photo.get("creator_url"):
            creator = f'<a href="{esc(photo["creator_url"])}">{creator}</a>'
        licence = esc(photo["license"])
        if photo.get("license_url"):
            licence = f'<a href="{esc(photo["license_url"])}">{licence}</a>'
        label = esc(names.get(photo["slot"], photo["slot"]))
        items.append(f"<li>{label}: {title}, nga {creator}, {licence}. Prerë dhe zvogëluar.</li>")
    return "".join(items) or None


def saving_percent(offer: Deal) -> str | None:
    """'68' when the price is 68% below the route's usual price; None without a usual price (or a saving)."""
    saving = round((1 - offer.best.price / offer.median) * 100) if offer.median else 0
    return str(saving) if saving >= 1 else None


def city_label(route: Route) -> str:
    """'Milano (Bergamo)', or just the city when it has one airport."""
    return route.city + (f" ({route.airport})" if route.airport else "")


def destination_options(offers: list[Deal]) -> str | None:
    """The <option> list for the search card's "Për" (to) select, one per route on the page.

    Deals come in their own group at the top. Each option carries the route's cheapest
    date and price as data attributes, so the page's JavaScript can pre-fill the date
    picker and show the price without looking anything up.
    """
    def option(offer: Deal) -> str:
        label = f"{city_label(offer.route)}, nga €{format_price(offer.best.price)}"
        return (f'<option value="{html.escape(offer.route.iata)}" data-date="{offer.best.depart_date.isoformat()}"'
                f' data-price="{format_price(offer.best.price)}">{html.escape(label, quote=False)}</option>')

    groups = [("Ofertat e momentit", [o for o in offers if o.is_deal]),
              ("Destinacionet e tjera", [o for o in offers if not o.is_deal])]
    parts = []
    for title, group in groups:
        if group:
            by_city = sorted(group, key=lambda o: (o.route.city, o.route.airport))
            parts.append(f'<optgroup label="{title}">' + "".join(option(o) for o in by_city) + "</optgroup>")
    return "\n".join(parts) or None


def country_code(flag: str) -> str | None:
    """'🇮🇹' -> 'it'. A flag emoji is two "regional indicator" letters; anything else gives None."""
    letters = [chr(ord(ch) - 0x1F1E6 + ord("a")) for ch in flag if 0x1F1E6 <= ord(ch) <= 0x1F1FF]
    return "".join(letters) if len(letters) == 2 else None


def telegram_url(channel_handle: str) -> str | None:
    """'@flyfromtirana' -> 'https://t.me/flyfromtirana'. A numeric (private) id has no public link."""
    handle = channel_handle.strip().lstrip("@")
    if not handle or handle.lstrip("-").isdigit():
        return None
    return f"https://t.me/{handle}"


def format_updated(moment: datetime, config: Config) -> str:
    """'9 Tet 2026, 12:00' in the configured timezone, with the post template's month names."""
    local = moment.astimezone(ZoneInfo(config.timezone))
    months = WORDS[config.language]["months"]
    return f"{local.day} {months[local.month - 1]} {local.year}, {local:%H:%M}"


if __name__ == "__main__":
    sys.exit(main())
