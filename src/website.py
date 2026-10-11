"""Builds the website: one static page with the cheapest price to every destination.

    python -m src.website               # writes docs/index.html (and docs/.nojekyll)
    python -m src.website --out /tmp/x  # write it somewhere else, e.g. to look at it locally
    python -m src.website --no-premium-delay   # also show what premium is still getting early

It only reads data/prices.db (no API calls), so it can run at any time. On the
server src/scheduler.py runs it after every scan, and the `web` container
serves the docs/ folder as the site.

Per route the page shows: the cheapest date from the newest scan (plus other
dates about as cheap), the usual price, the cheapest flight back and a booking
link. Every city with a guide in content/destinations/ also gets a page of its
own (/milan/, /rome/, ...): its current prices, then the guide (src/guides.py).
Routes that meet the free channel's deal rules come first, as "Ofertat
e momentit". With a premium channel the page follows the free channel: a price
premium members are still getting early is left out until the free channel may
have it too (build_offers below). Wording and layout live in templates/site.html (the page),
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
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Config, ConfigError, Route, load_config
from src.deals import Deal, cheapest_return, deal_reason, early_access_cutoff, is_premium_only
from src.formatter import (WORDS, build_values, format_dates, format_price, load_template, render,
                           saving_percent)
from src.guides import Entry, Guide, load_entry_rules, load_guides
from src.i18n import LANGUAGE_CODES, LANGUAGE_NAMES, Strings, load_strings, localize
from src.links import LinkBuilder
from src.photos import ASSETS_DIR, CREDITS_FILE, needs_credit
from src.storage import Storage

log = logging.getLogger("flyfromtirana.website")

# A route whose newest prices are older than this is left off the page (the API
# kept failing for it, say). Stale prices would only disappoint.
STALE_AFTER = timedelta(days=2)

# How many deals become banners in the hero carousel at the top of the page.
HERO_SLIDES = 3

# Photos for the page (ASSETS_DIR, from src/photos.py): assets/img/dest/<iata>.webp
# for a route's card, assets/img/services/<partner>.webp for the travel services,
# and credits.json with the author and licence of each one. A route without a
# photo simply gets its country-coloured card instead.

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
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the FlyFromTirana website from the price database.")
    parser.add_argument("--out", type=Path, help="output folder (default: website.output_dir in config.yaml)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="path to config.yaml")
    parser.add_argument("--no-premium-delay", action="store_true",
                        help="also show the prices premium is still getting early (for a local preview)")
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
        path = build_site(config, marker, out_dir=args.out, premium_delay=not args.no_premium_delay)
        log.info("Website written to %s", path)
        return 0
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return 2
    except Exception:
        log.exception("Unexpected error")
        return 1


def build_site(config: Config, marker: str, *, out_dir: Path | None = None,
               now: datetime | None = None, premium_delay: bool = True) -> Path:
    """Read the database, render the page, write it. Returns the path of index.html."""
    now = now or datetime.now(timezone.utc)
    out_dir = out_dir or config.website.output_dir
    # Same link builder as the posts, but with the site's own links and SubID
    # (website.links in config.yaml), so the Travelpayouts stats show which
    # clicks came from the website.
    links = LinkBuilder(config.website.links, marker, origin=config.origin, currency=config.currency)
    links.validate()
    with Storage(config.db_path) as storage:
        offers = build_offers(config, storage, now, premium_delay=premium_delay)
        updated_at = storage.last_fetched_at()
    # One copy of the site per language. The first language's pages sit at the top
    # (/, /milan/), every other language's in its own folder (/en/, /en/milan/).
    for language in config.website.languages:
        guides = site_guides(config, language)
        folder = out_dir / language_folder(config, language)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(
            render_site(offers, config, links, updated_at=updated_at, now=now, guides=guides, language=language),
            encoding="utf-8")
        # One page per city guide: /milan/index.html, so its address is /milan/.
        for guide in guides.values():
            (folder / guide.slug).mkdir(exist_ok=True)
            (folder / guide.slug / "index.html").write_text(
                render_guide_page(guide, guides, offers, config, links, updated_at=updated_at, now=now,
                                  language=language),
                encoding="utf-8")
        log.info("[%s] %d route(s) on the page, %d of them deals, %d city page(s)", language, len(offers),
                 sum(o.is_deal for o in offers), len(guides))
    write_sitemap(out_dir, config, now)
    copy_photos(out_dir)
    (out_dir / ".nojekyll").touch()  # harmless elsewhere; GitHub Pages needs it to serve the files as they are
    return out_dir / "index.html"


def copy_photos(out_dir: Path) -> None:
    """Put assets/img next to the page as img/ (the credits file stays behind)."""
    source = ASSETS_DIR / "img"
    if source.is_dir():
        shutil.copytree(source, out_dir / "img", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.json"))


def build_offers(config: Config, storage: Storage, now: datetime, *,
                 premium_delay: bool = True) -> list[Deal]:
    """One Deal per route that has current prices; its reason is None when it isn't a deal.

    Deals come first, biggest saving first, then the other routes, cheapest first.

    With a premium channel, the page waits like the free channel does: a price
    that is a deal for premium is left out until premium has had it for
    free_delay_hours. Until then the route shows its cheapest other date.
    premium_delay=False shows everything (a local preview).
    """
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    rules = config.rules
    premium = config.premium
    hold_back = premium_delay and premium.enabled
    cutoff = early_access_cutoff(now, premium.free_delay_hours)
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
        if hold_back:
            outbound = [q for q in outbound
                        if not is_premium_only(q, route, median, storage, premium.rules, now, cutoff)]
            if not outbound:
                log.info("%s: every price is still premium-only, leaving it off the page for now", route.iata)
                continue
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
                updated_at: datetime | None, now: datetime, guides: dict[str, Guide] | None = None,
                language: str | None = None) -> str:
    """The whole main page as HTML, in one language (default: the site's first).

    guides: that language's city guides that have a page (see site_guides).
    """
    guides = guides or {}
    language = language or config.website.languages[0]
    strings = load_strings(language)
    root = "./" if language == config.website.languages[0] else "../"   # from this page to the top of the site

    def rows(template_name: str, chosen: list[Deal]) -> list[str]:
        template = load_site_template(template_name, strings)
        return [render(template, row_values(o, config, links, guides, language=language, root=root)) for o in chosen]

    deal_rows = rows("site_row.html", [o for o in offers if o.is_deal])
    other_rows = rows("site_row.html", [o for o in offers if not o.is_deal])
    # The hero banners at the top of the page: the three best deals, one slide each.
    hero_slides = rows("site_hero.html", [o for o in offers if o.is_deal][:HERO_SLIDES])
    # The destinations carousel: one photo card per route, deals first (the order of offers).
    dest_cards = rows("site_card.html", offers)
    names = {city: guide.display_name for city, guide in guides.items()}   # "Milan" -> "Milano" on the Italian page

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
        "LANG": language,
        "UPDATED": format_updated(updated_at, config, language) if updated_at else None,
        "EMPTY": strings.words["empty"] if not offers else None,
        "PREMIUM_HOURS": f"{premium.free_delay_hours:g}" if premium_on else None,  # :g turns 6.0 into "6"
        "YEAR": str(local_now.year),
        # For the headline ("Sot që nga €14") and the badges next to the section headings.
        "MIN_PRICE": format_price(min(o.best.price for o in offers)) if offers else None,
        "DEAL_COUNT": str(len(deal_rows)) if deal_rows else None,
        "ROUTE_COUNT": str(len(offers)) if offers else None,
        "TOP_CITY": city_label(top.route, names.get(top.route.city)) if top else None,
        "TOP_PRICE": format_price(top.best.price) if top else None,
        "TOP_SAVING": saving_percent(top) if top else None,
        "GUIDE_COUNT": str(len(guides)) if guides else None,
        "GA_ON": "1" if config.website.google_analytics_id else None,
        # The search card's date picker only allows dates the scanner actually covers.
        "DATE_MIN": (today + timedelta(days=1)).isoformat(),
        "DATE_MAX": (today + timedelta(days=config.lookahead_days)).isoformat(),
    }
    urls = {
        "TELEGRAM_URL": telegram_url(config.channel_handle),
        "PREMIUM_LINK": premium.join_link if premium_on else None,
        "SITE_URL": page_url(config, language),
        "ASSETS": root,
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
    values["PHOTO_CREDITS"] = photo_credits(config, links, strings, names)
    values["DESTINATION_OPTIONS"] = destination_options(offers, strings, names)
    values["GUIDE_TILES"] = guide_tiles(guides, offers, config, root=root, language=language)
    values["LANG_MENU"] = language_menu(config, language, root)
    values["LANG_LINKS"] = language_links(config, language, root)
    values["HREFLANG"] = hreflang_links(config)
    values["JS_WORDS"] = script_words(strings)
    values["ANALYTICS"] = analytics_html(config, language)
    values["DRIVE"] = drive_html(config)
    return render(load_site_template("site.html", strings), values) + "\n"


def load_site_template(name: str, strings: Strings | None = None) -> str:
    """templates/<name> without its HTML comments, and with a language's wording put in.

    The comments are notes for whoever edits the template; stripping them here keeps
    them off the page (the row template would otherwise repeat its note once per route).
    strings: the language whose text replaces every {T_NAME} (see src/i18n.py).
    """
    template = re.sub(r"<!--.*?-->", "", load_template(name), flags=re.DOTALL)
    return localize(template, strings) if strings else template


def script_words(strings: Strings) -> str:
    """The words the main page's script needs, as the JavaScript object it calls `words`."""
    words = dict(strings.js, months=WORDS[strings.language]["months"])
    # "</" would end the <script> block early if a text ever contained it.
    return json.dumps(words, ensure_ascii=False).replace("</", "<\\/")


# --- languages ---------------------------------------------------------------------

def language_folder(config: Config, language: str) -> str:
    """Where a language's pages sit under the site's top folder: "" for the first language, "en/" for English."""
    return "" if language == config.website.languages[0] else f"{language}/"


def page_url(config: Config, language: str, slug: str | None = None) -> str | None:
    """A page's full address (None when website.url isn't set): the main page, or the city page `slug`."""
    base_url = site_base_url(config)
    if not base_url:
        return None
    return base_url + language_folder(config, language) + (f"{slug}/" if slug else "")


def languages_with(config: Config, slug: str | None) -> list[str]:
    """The languages a page exists in: all of them for the main page, those with the guide for a city page."""
    return [language for language in config.website.languages
            if slug is None or any(guide.slug == slug for guide in site_guides(config, language).values())]


def language_targets(config: Config, language: str, root: str, slug: str | None = None) -> list[dict]:
    """Where this page is in every language of the site, for the language switcher.

    root: the way from this page to the top of the site ("./", "../" or "../../").
    A city page that has no guide in a language leads to that language's main page.
    """
    translated = languages_with(config, slug)
    return [{
        "language": code,
        "href": html.escape(root + language_folder(config, code) + (f"{slug}/" if slug and code in translated else "")),
        "current": ' aria-current="page"' if code == language else "",
        "code": html.escape(LANGUAGE_CODES.get(code, code.upper())),            # "AL"
        "name": html.escape(LANGUAGE_NAMES.get(code, code.upper())),            # "Shqip"
    } for code in config.website.languages]


def language_menu(config: Config, language: str, root: str, slug: str | None = None) -> str | None:
    """The language dropdown above the top bar (templates/site_lang.html). None with only one language."""
    if len(config.website.languages) < 2:
        return None
    options = "".join(
        '<a href="{href}" lang="{language}" hreflang="{language}"{current}><b>{code}</b><span>{name}</span></a>'.format(**t)
        for t in language_targets(config, language, root, slug))
    return render(load_site_template("site_lang.html"), {
        "LANG_CODE": html.escape(LANGUAGE_CODES.get(language, language.upper())),
        "LANG_LABEL": html.escape(load_strings(language).words["languages"]),
        "LANG_OPTIONS": options,
    })


def language_links(config: Config, language: str, root: str, slug: str | None = None) -> str | None:
    """The same links as a plain row (AL EN IT), for the footer. None with only one language."""
    if len(config.website.languages) < 2:
        return None
    links = "".join(
        '<a href="{href}" lang="{language}" hreflang="{language}"{current}>{code}<span class="sr"> {name}</span></a>'.format(**t)
        for t in language_targets(config, language, root, slug))
    label = html.escape(load_strings(language).words["languages"])
    return f'<nav class="langs" aria-label="{label}">{links}</nav>'


def hreflang_links(config: Config, slug: str | None = None) -> str | None:
    """<link rel="alternate" hreflang=...> tags, telling search engines about a page's other languages."""
    languages = languages_with(config, slug)
    if len(languages) < 2 or not site_base_url(config):
        return None
    tags = [f'<link rel="alternate" hreflang="{code}" href="{html.escape(page_url(config, code, slug))}">'
            for code in languages]
    # x-default: the page for visitors whose language the site doesn't have.
    tags.append(f'<link rel="alternate" hreflang="x-default" href="{html.escape(page_url(config, languages[0], slug))}">')
    return "\n".join(tags)


def row_values(offer: Deal, config: Config, links: LinkBuilder, guides: dict[str, Guide] | None = None, *,
               language: str | None = None, root: str = "./") -> dict[str, str | None]:
    """Everything templates/site_row.html and site_hero.html can use: the post placeholders plus a few of their own.

    language: the page's language (default: the site's first). root: the way from the
    page to the top of the site, where the photos are.
    """
    language = language or config.website.languages[0]
    values = build_values(offer, links, language=language, channel_handle=config.channel_handle,
                          airline_names=config.airlines)
    guide = (guides or {}).get(offer.route.city)
    countries = load_strings(language).words.get("countries", COUNTRY_NAMES)
    photo = route_photo(offer.route.iata)
    if guide and guide.name:   # the city's name in this language: "Milano"
        values["CITY"] = html.escape(guide.name, quote=False)
        values["CITY_UPPER"] = html.escape(guide.name.upper(), quote=False)
    values.update({
        "GUIDE_URL": f"{guide.slug}/" if guide else None,   # the city's own page, when it has a guide
        "IATA": offer.route.iata,
        "DEAL": "deal" if offer.is_deal else None,  # a CSS class; template parts using it vanish for other routes
        "COUNTRY": country_code(offer.route.flag),   # "it" for 🇮🇹: the CSS class that picks the card's colours
        "BEST_DATE": offer.best.depart_date.isoformat(),  # "2026-11-21": the page's JavaScript pre-fills the date picker with it
        # For the photo cards: "14 Nën", "Austri" and the route's photo (None when there isn't one).
        "BEST_DAY": format_dates([offer.best.depart_date], WORDS[language]["months"]),
        "COUNTRY_NAME": countries.get(country_code(offer.route.flag) or ""),
        "PHOTO": root + photo if photo else None,
    })
    return values


# --- the city pages ---------------------------------------------------------------

@lru_cache(maxsize=None)
def guides_in(language: str) -> dict[str, Guide]:
    """Every guide written in a language. Read once: a build asks for them many times."""
    return load_guides(language)


def site_guides(config: Config, language: str | None = None) -> dict[str, Guide]:
    """A language's guides that get a page: those of cities we fly to, in the order of config.yaml's routes."""
    guides = guides_in(language or config.website.languages[0])
    cities = dict.fromkeys(route.city for route in config.routes)   # each city once, in order
    return {city: guides[city] for city in cities if city in guides}


def site_base_url(config: Config) -> str | None:
    """'https://example.com/' (always with the last slash), or None when website.url isn't set."""
    return config.website.url.rstrip("/") + "/" if config.website.url else None


def guide_photo(guide: Guide) -> str | None:
    """'img/guides/milan.webp' when the city has a wide photo in assets/img/guides, else None."""
    path = f"img/guides/{guide.slug}.webp"
    return path if (ASSETS_DIR / path).exists() else None


def guide_tiles(guides: dict[str, Guide], offers: list[Deal], config: Config, *, pages: str = "",
                root: str = "./", skip: str | None = None, language: str | None = None) -> str | None:
    """One photo tile per city page: "Udhëzues për qytetet" on the main page, and
    "Destinacione të tjera" on a city page.

    pages: the way to the city pages of this language ("" on the main page, "../" on a
    city page). root: the way to the top of the site, where the photos are. skip: the
    city whose page this is, which doesn't link to itself.
    """
    cheapest: dict[str, float] = {}
    for offer in offers:
        city = offer.route.city
        cheapest[city] = min(offer.best.price, cheapest.get(city, offer.best.price))
    flags = {route.city: route.flag for route in reversed(config.routes)}   # a city's first route wins
    template = load_site_template("site_guide.html", load_strings(language or config.website.languages[0]))
    esc = html.escape
    tiles = []
    for guide in guides.values():
        if guide.city == skip:
            continue
        flag = flags.get(guide.city) or None
        photo = guide_photo(guide)
        tiles.append(render(template, {
            "CITY": esc(guide.display_name, quote=False),
            "FLAG": flag,
            "COUNTRY": country_code(flag or ""),
            "TAGLINE": esc(guide.tagline, quote=False) or None,
            "GUIDE_URL": f"{pages}{guide.slug}/",
            "GUIDE_PHOTO": root + photo if photo else None,
            "PRICE": format_price(cheapest[guide.city]) if guide.city in cheapest else None,
        }))
    return "\n".join(tiles) or None


def render_guide_page(guide: Guide, guides: dict[str, Guide], offers: list[Deal], config: Config,
                      links: LinkBuilder, *, updated_at: datetime | None, now: datetime,
                      language: str | None = None) -> str:
    """One city's page as HTML: its current prices (one ticket per airport), then its guide.

    guide and guides are in `language` (default: the site's first).
    """
    language = language or config.website.languages[0]
    strings = load_strings(language)
    # From this page to the top of the site: one folder up, two for a language with its own folder.
    root = "../" if language == config.website.languages[0] else "../../"
    routes = [route for route in config.routes if route.city == guide.city]
    city_offers = sorted((o for o in offers if o.route.city == guide.city), key=lambda o: o.best.price)
    best = city_offers[0] if city_offers else None
    fare_template = load_site_template("site_dest_fare.html", strings)
    fares = [render(fare_template, row_values(o, config, links, guides, language=language, root=root))
             for o in city_offers]

    local_now = now.astimezone(ZoneInfo(config.timezone))
    premium = config.premium
    premium_on = premium.enabled and bool(premium.join_link)
    first = routes[0]
    country = country_code(first.flag)
    # The city's own wide photo; without one, the photo of one of its routes.
    wide_photo = guide_photo(guide)
    photo = wide_photo or next((path for path in (route_photo(route.iata) for route in routes) if path), None)
    base_url = site_base_url(config)
    text = {
        "BRAND": config.brand,
        "CHANNEL": config.channel_handle,
        "LANG": language,
        "CITY": guide.display_name,
        "COUNTRY": country,
        "COUNTRY_NAME": strings.words.get("countries", COUNTRY_NAMES).get(country or ""),
        "FLAG": first.flag or None,
        "INTRO": guide.intro,
        "MIN_PRICE": format_price(best.best.price) if best else None,
        "UPDATED": format_updated(updated_at, config, language) if updated_at else None,
        "NO_FARES": strings.words["no_fares"] if not fares else None,
        "TRANSPORT": guide.transport or None,
        "BUDGET": guide.budget or None,
        "WHEN": guide.when or None,
        "ENTRY_RULES": load_entry_rules(language).get(country or ""),
        "DAYS": str(len(guide.itinerary)) if guide.itinerary else None,
        "PREMIUM_HOURS": f"{premium.free_delay_hours:g}" if premium_on else None,
        "YEAR": str(local_now.year),
        "GA_ON": "1" if config.website.google_analytics_id else None,
    }
    urls = {
        "HOME": "../",   # this language's main page
        "GUIDE_PHOTO": root + wide_photo if wide_photo else None,
        "PHOTO": root + photo if photo and not wide_photo else None,
        "PHOTO_URL": base_url + photo if base_url and photo else None,
        "PAGE_URL": page_url(config, language, guide.slug),
        "TELEGRAM_URL": telegram_url(config.channel_handle),
        "PREMIUM_LINK": premium.join_link if premium_on else None,
    }
    # The travel-service links. A partner URL template may mention the city and dates
    # (a hotel search): the cheapest flight's date stands in, or today without one.
    checkin = best.best.depart_date if best else local_now.date()
    for name in links.partner_names:
        urls[f"{name.upper()}_LINK"] = links.partner(name, first, checkin, checkin + timedelta(days=3))

    esc = html.escape
    values = {key: esc(value, quote=False) if value else None for key, value in text.items()}
    values.update({key: esc(value, quote=True) if value else None for key, value in urls.items()})
    # These are HTML already, so they go in as they are.
    values["FARES"] = "\n".join(fares) or None
    def cards(entries: list[Entry]) -> str | None:
        """Sights, neighbourhoods, dishes and day trips are all drawn the same: a name, then its text."""
        return "".join(f"<li><b>{esc(e.name)}</b>{esc(e.text)}</li>" for e in entries) or None

    values["SIGHTS"] = cards(guide.sights)
    values["AREAS"] = cards(guide.areas)
    values["FOOD"] = cards(guide.food)
    values["DAYTRIPS"] = cards(guide.daytrips)
    values["ITINERARY"] = "".join(f"<li>{esc(day)}</li>" for day in guide.itinerary) or None
    values["AIRPORTS"] = "".join(
        f'<div class="airport"><h3>{esc(route.airport or route.city)} ({esc(route.iata)})</h3>'
        f"<p>{esc(guide.airports[route.iata])}</p></div>"
        for route in routes if route.iata in guide.airports) or None
    values["TIPS"] = "".join(f"<li>{esc(tip)}</li>" for tip in guide.tips) or None
    values["FAQ"] = "".join(f'<div class="qa"><h3>{esc(item.q)}</h3><p>{esc(item.a)}</p></div>'
                            for item in guide.faq) or None
    # A link in "Në këtë faqe" only for the parts this guide has.
    for part in ("SIGHTS", "ITINERARY", "AREAS", "FOOD", "AIRPORTS", "TRANSPORT", "DAYTRIPS"):
        values[f"{part}_ON"] = "1" if values[part] else None
    values["OTHER_GUIDES"] = guide_tiles(guides, offers, config, pages="../", root=root, skip=guide.city,
                                         language=language)
    values["LANG_MENU"] = language_menu(config, language, root, guide.slug)
    values["LANG_LINKS"] = language_links(config, language, root, guide.slug)
    values["HREFLANG"] = hreflang_links(config, guide.slug)
    values["ANALYTICS"] = analytics_html(config, language)
    values["DRIVE"] = drive_html(config)
    return render(load_site_template("site_dest.html", strings), values) + "\n"


def analytics_html(config: Config, language: str | None = None) -> str | None:
    """The Google Analytics block (templates/site_analytics.html), or None when no ID is set."""
    analytics_id = config.website.google_analytics_id
    if not analytics_id:
        return None
    ask_first = config.website.google_analytics_ask_first
    # Exactly one of GA_ASK / GA_AUTO has a value: it picks the box's wording and behaviour.
    values = {"GA_ID": analytics_id, "GA_ASK": "1" if ask_first else None, "GA_AUTO": None if ask_first else "1"}
    strings = load_strings(language or config.website.languages[0])
    return render(load_site_template("site_analytics.html", strings), values)


def drive_html(config: Config) -> str | None:
    """The Travelpayouts Drive snippet (templates/site_drive.html), or None when no script is set."""
    script = config.website.travelpayouts_drive_script
    if not script:
        return None
    return render(load_site_template("site_drive.html"), {"DRIVE_URL": script})


def write_sitemap(out_dir: Path, config: Config, now: datetime) -> None:
    """sitemap.xml and robots.txt, so search engines find every page: the main page and the city
    pages, in every language.

    Both need full addresses, so they are only written when website.url is set.
    """
    base_url = site_base_url(config)
    if not base_url:
        return
    day = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
    pages = []
    for language in config.website.languages:
        pages.append(page_url(config, language))
        pages += [page_url(config, language, guide.slug) for guide in site_guides(config, language).values()]
    entries = "".join(f"  <url><loc>{html.escape(page)}</loc><lastmod>{day}</lastmod></url>\n" for page in pages)
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{entries}</urlset>\n', encoding="utf-8")
    (out_dir / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base_url}sitemap.xml\n",
                                        encoding="utf-8")


def route_photo(iata: str) -> str | None:
    """'img/dest/vie.webp' when assets/img/dest/vie.webp exists, else None (the card shows its colours)."""
    path = f"img/dest/{iata.lower()}.webp"
    return path if (ASSETS_DIR / path).exists() else None


def photo_credits(config: Config, links: LinkBuilder, strings: Strings,
                  names: dict[str, str] | None = None) -> str | None:
    """The footer's photo credits, as HTML. None (no credits block at all) when no photo needs one.

    Only photos whose licence requires a credit are listed, and only while they
    are on the page. The photos in use are Unsplash or CC0, which require none,
    so the footer normally has no credits. A CC BY or CC BY-SA photo would bring
    its credit back, because those licences ask for exactly this: who took it,
    where it comes from, which licence, and that it was changed (cropped and resized).
    """
    if not CREDITS_FILE.exists():
        return None
    # What each photo shows, by its slot in credits.json: a route's city, or a travel service.
    places = {route.iata: city_label(route, (names or {}).get(route.city)) for route in config.routes}
    # A service card is only on the page when its partner link is set in config.yaml.
    partners = links.settings.partners
    places.update({slot: name for slot, name in strings.words["services"].items()
                   if slot in partners and partners[slot].url})
    esc = html.escape
    items = []
    for photo in json.loads(CREDITS_FILE.read_text(encoding="utf-8")):
        if not (ASSETS_DIR / photo["file"]).exists():
            continue
        if photo["slot"] not in places or not needs_credit(photo["license"]):
            continue
        title = f'<a href="{esc(photo["source_url"])}">{esc(photo["title"])}</a>' if photo["source_url"] else esc(photo["title"])
        creator = esc(photo["creator"])
        if photo.get("creator_url"):
            creator = f'<a href="{esc(photo["creator_url"])}">{creator}</a>'
        licence = esc(photo["license"])
        if photo.get("license_url"):
            licence = f'<a href="{esc(photo["license_url"])}">{licence}</a>'
        line = strings.words["credit"].format(place=esc(places[photo["slot"]]), title=title, author=creator,
                                              licence=licence)
        items.append(f"<li>{line}</li>")
    return "".join(items) or None


def city_label(route: Route, name: str | None = None) -> str:
    """'Milan (Bergamo)', or just the city when it has one airport. name: the city in the page's language."""
    return (name or route.city) + (f" ({route.airport})" if route.airport else "")


def destination_options(offers: list[Deal], strings: Strings, names: dict[str, str] | None = None) -> str | None:
    """The <option> list for the search card's "Për" (to) select, one per route on the page.

    Deals come in their own group at the top. Each option carries the route's cheapest
    date and price as data attributes, so the page's JavaScript can pre-fill the date
    picker and show the price without looking anything up.
    """
    def option(offer: Deal) -> str:
        city = city_label(offer.route, (names or {}).get(offer.route.city))
        label = strings.words["option"].format(city=city, price=format_price(offer.best.price))
        return (f'<option value="{html.escape(offer.route.iata)}" data-date="{offer.best.depart_date.isoformat()}"'
                f' data-price="{format_price(offer.best.price)}">{html.escape(label, quote=False)}</option>')

    groups = [(strings.words["deals_group"], [o for o in offers if o.is_deal]),
              (strings.words["others_group"], [o for o in offers if not o.is_deal])]
    parts = []
    for title, group in groups:
        if group:
            by_city = sorted(group, key=lambda o: (o.route.city, o.route.airport))
            parts.append(f'<optgroup label="{html.escape(title)}">' + "".join(option(o) for o in by_city)
                         + "</optgroup>")
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


def format_updated(moment: datetime, config: Config, language: str | None = None) -> str:
    """'9 Tet 2026, 12:00' in the configured timezone, with the language's month names."""
    local = moment.astimezone(ZoneInfo(config.timezone))
    months = WORDS[language or config.website.languages[0]]["months"]
    return f"{local.day} {months[local.month - 1]} {local.year}, {local:%H:%M}"


if __name__ == "__main__":
    sys.exit(main())
