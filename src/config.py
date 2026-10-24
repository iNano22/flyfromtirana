"""Loads config.yaml and the secret environment variables.

Everything the rest of the code needs from configuration is turned into small
dataclasses here, so a typo in config.yaml fails at startup with a clear
message instead of crashing halfway through a run.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Partner link slots that always exist, so the template can reference
# {HOTEL_LINK} etc. even before they are filled in config.yaml.
DEFAULT_PARTNERS = ("hotel", "esim", "insurance", "compensation", "car_rental", "tickets", "transfer", "luggage",
                    "tours")
# A scan of every route takes a couple of minutes, so scanning more often than
# this would only queue runs up behind each other.
MIN_PREMIUM_SCAN_MINUTES = 5


class ConfigError(Exception):
    """config.yaml or a required environment variable is missing or invalid."""


@dataclass(frozen=True)
class Route:
    """One destination we scan from the origin (TIA)."""

    iata: str                                    # airport (or city) code, e.g. "BGY"
    city: str                                    # Albanian name used in posts, e.g. "Milano"
    city_en: str                                 # English name, used in hotel search links
    flag: str                                    # flag emoji
    airport: str = ""                            # optional label for multi-airport cities, e.g. "Bergamo"
    absolute_threshold_eur: float | None = None  # any price at/below this is a deal


@dataclass(frozen=True)
class DealRules:
    """Knobs for deal detection and dedupe (explained in config.yaml)."""

    discount_pct: float = 40
    threshold_min_discount_pct: float = 25
    median_window_days: int = 30
    min_samples_for_median: int = 10
    repost_cooldown_days: int = 7
    price_band_eur: float = 10
    date_price_tolerance_pct: float = 10
    max_dates_per_post: int = 4
    return_min_days: int = 2
    return_max_days: int = 14


@dataclass(frozen=True)
class LinkTemplate:
    url: str = ""      # target URL template; empty = this link is left out of posts
    # Optional tracking link around the target: {url} is replaced by the encoded target (a
    # redirect: ...?u={url}), {plain_url} by the target as it is (an ID added to its end).
    wrapper: str = ""


@dataclass(frozen=True)
class LinkSettings:
    sub_id: str = ""
    flight_base_url: str = "https://www.aviasales.com"
    flight_wrapper: str = ""
    partners: dict[str, LinkTemplate] = field(default_factory=dict)  # "hotel" -> LinkTemplate
    # A partner's own page for a city: "tickets" -> {"Milan": "https://...milan..."}. A post or a
    # page about that city links there instead of to the partner's general url.
    city_pages: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PremiumSettings:
    """Paid early-access channel. The free channel only gets a deal free_delay_hours later."""

    enabled: bool = False
    free_delay_hours: float = 6
    scan_every_minutes: float = 0                          # quick premium scans between full runs; 0 = none
    max_posts_per_run: int = 4
    join_link: str = ""                                    # paid invite link, advertised in free posts
    rules: DealRules = field(default_factory=DealRules)    # usually looser than the free channel's


SOCIAL_PLATFORMS = ("instagram", "facebook")


@dataclass(frozen=True)
class SocialSettings:
    """Instagram/Facebook posts: the pictures and caption of a deal, posted by the bot or sent to the
    owner's chat as a draft (src/social.py)."""

    enabled: bool = False
    # When the posts go out (local time), one post per time, at the first run at or after
    # it: the best deal on the website the accounts haven't had. So a day has at most this
    # many posts, hours apart, when people are on their phones.
    post_times: tuple[time, ...] = (time(12, 0), time(19, 30))
    handle: str = ""                # the account's name on the cards, e.g. "@flyfromtirana" (empty = channel_handle)
    sub_id: str = "facebook"        # Travelpayouts SubID of the booking link in the Facebook post
    hashtags: str = ""              # the last line of every caption
    # Where the bot posts by itself once META_PAGE_TOKEN is set: "instagram" (a feed post and a
    # story) and/or "facebook" (a Page post with the booking link). Empty = only the Telegram drafts.
    publish: tuple[str, ...] = ()
    keep_pictures_days: int = 7     # the posted pictures stay on the website this long (Instagram fetches them there)
    # Keeping the accounts busy: on one weekday (0 = Monday ... 6 = Sunday; None = never)
    # the day's first post is a "top deals of the week" carousel, and the day's last
    # post time goes to a city guide when the day had no post at all.
    weekly_day: int | None = None
    weekly_count: int = 5
    # Deal posts go to Instagram as a Reel (a short video of the card, src/reels.py) instead of
    # the feed picture. Needs ffmpeg; without it the post stays a picture.
    reels: bool = False


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# The places that advertise the free channel, each with its own invite link (invite_links in
# config.yaml), so the weekly report can say where the new members came from (src/growth.py).
INVITE_SOURCES = ("instagram", "facebook", "website", "whatsapp", "bot")


@dataclass(frozen=True)
class AlertSettings:
    """The price alerts people set in a private chat with the bot (src/bot.py, src/alerts.py)."""

    enabled: bool = False
    bot_username: str = ""                               # "FlyFromTiranaBot", for the "alert me" links
    prices: tuple[int, ...] = (20, 30, 40, 50, 70, 100)  # the price buttons, in euros
    max_per_person: int = 10
    sub_id: str = "alerts"                               # Travelpayouts SubID of the alerts' booking links


@dataclass(frozen=True)
class TripWatch:
    """A trip only the owner hears about (src/trips.py): out and back to `destination` between
    two dates, for a few days, at max_price or less for both flights together."""

    name: str                       # "Memmingen before Christmas": the message's title
    destination: str                # airport code, e.g. "FMM"; it needn't be one of the routes
    city: str                       # "Memmingen"
    earliest_departure: date
    latest_return: date
    min_days: int                   # how long the trip is, counting the day out and the day back
    max_days: int
    weekend: bool = False           # True = the trip must include a Saturday and a Sunday
    max_price: float | None = None  # euros, both flights together; None = any price


@dataclass(frozen=True)
class GrowthSettings:
    """The owner's weekly report on the Telegram channels (src/growth.py)."""

    report_day: int | None = 0          # 0 = Monday ... 6 = Sunday; None = no report
    report_time: time = time(9, 0)      # local; the report goes out at the first full run after it


@dataclass(frozen=True)
class WebsiteSettings:
    """The static site built by src/website.py (the docs/ folder, served by the `web` container)."""

    output_dir: Path = PROJECT_ROOT / "docs"
    # The site's languages. The first one's pages sit at the top of the site (/, /milan/),
    # every other one's in a folder of its own (/en/, /en/milan/).
    languages: tuple[str, ...] = ("sq",)
    sub_id: str = "website"   # Travelpayouts SubID for links on the site ("telegram" is used in posts)
    # The site's affiliate links: the ones the posts use, except where
    # website.links in config.yaml gives the site its own (and with sub_id above).
    links: LinkSettings = field(default_factory=LinkSettings)
    url: str = ""             # public address: linked in posts and used for the page's
                              # canonical/og:url tags; empty = left out
    google_analytics_id: str = ""   # "G-XXXXXXXXXX" turns Google Analytics on
    # True: Analytics waits for the visitor's "Pranoj". False: it counts from the first
    # page view and the visitor can only switch it off afterwards.
    google_analytics_ask_first: bool = True
    # The address of this project's Travelpayouts Drive script; empty = no Drive on the site.
    travelpayouts_drive_script: str = ""
    # How many activities GetYourGuide's widget shows on a city page; 0 = no widget.
    getyourguide_activities: int = 0
    # Who runs the site and where to write about personal data, for the privacy page
    # (/privacy/). Without both, the page leaves out its "who we are" part.
    operator: str = ""
    contact_email: str = ""


@dataclass(frozen=True)
class Config:
    brand: str
    channel_handle: str
    origin: str
    language: str
    timezone: str
    routes: list[Route]
    # scanning
    lookahead_days: int
    full_run_every_minutes: int          # scan, free channel and site rebuild this often (src/scheduler.py)
    currency: str
    market: str | None
    request_delay_seconds: float
    max_price_age_days: int | None      # leave out prices found longer ago than this; None = any age
    scan_return_flights: bool
    # deals + posting
    rules: DealRules
    max_posts_per_run: int
    quiet_hours: tuple[time, time] | None  # (start, end) in local time, or None
    # storage
    db_path: Path
    history_retention_days: int
    # links + display
    links: LinkSettings
    airlines: dict[str, str]
    premium: PremiumSettings = field(default_factory=PremiumSettings)
    website: WebsiteSettings = field(default_factory=WebsiteSettings)
    social: SocialSettings = field(default_factory=SocialSettings)
    post_photos: bool = True               # send each post with its destination photo
    # The free channel's invite link for each place that advertises it: "website" -> "https://t.me/+...".
    # A place without one uses the channel's public address (src/growth.py, join_link).
    invite_links: dict[str, str] = field(default_factory=dict)
    growth: GrowthSettings = field(default_factory=GrowthSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    trip_watches: tuple[TripWatch, ...] = ()


@dataclass(frozen=True)
class Secrets:
    travelpayouts_token: str
    travelpayouts_marker: str = ""
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_premium_channel_id: str = ""
    telegram_drafts_chat_id: str = ""      # where the Instagram/Facebook drafts and reports go; empty = none
    meta_page_token: str = ""              # the Facebook Page's token (posts to Instagram and the Page); empty = drafts only


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        return _build_config(raw, base_dir=path.parent)
    except KeyError as exc:
        raise ConfigError(f"{path.name} is missing the required key {exc}") from None
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path.name} has an invalid value: {exc}") from None


def _build_config(raw: dict, base_dir: Path) -> Config:
    routes = [_build_route(r) for r in raw["routes"] or []]
    if not routes:
        raise ValueError("'routes' must list at least one destination")
    codes = [r.iata for r in routes]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        raise ValueError(f"duplicate route(s): {', '.join(duplicates)}")

    rules = DealRules(
        discount_pct=float(raw.get("deal_discount_pct", 40)),
        threshold_min_discount_pct=float(raw.get("threshold_min_discount_pct", 25)),
        median_window_days=int(raw.get("median_window_days", 30)),
        min_samples_for_median=int(raw.get("min_samples_for_median", 10)),
        repost_cooldown_days=int(raw.get("repost_cooldown_days", 7)),
        price_band_eur=float(raw.get("price_band_eur", 10)),
        date_price_tolerance_pct=float(raw.get("date_price_tolerance_pct", 10)),
        max_dates_per_post=int(raw.get("max_dates_per_post", 4)),
        return_min_days=int(raw.get("return_min_days", 2)),
        return_max_days=int(raw.get("return_max_days", 14)),
    )
    if not 0 < rules.discount_pct < 100:
        raise ValueError("deal_discount_pct must be between 0 and 100")
    if rules.price_band_eur <= 0:
        raise ValueError("price_band_eur must be greater than 0")

    quiet = raw.get("quiet_hours")
    quiet_hours = None
    if quiet:
        quiet_hours = (time.fromisoformat(str(quiet["start"])), time.fromisoformat(str(quiet["end"])))

    full_every = int(raw.get("full_run_every_minutes", 180))
    if full_every < 5 or (24 * 60) % full_every:
        raise ValueError(f"full_run_every_minutes must be 5 or more and fit a day evenly (15, 30, 60, 180...), "
                         f"not {full_every}")
    max_age = raw.get("max_price_age_days", 2)
    if max_age is not None and int(max_age) < 0:
        raise ValueError(f"max_price_age_days must be 0 or more (or null), not {max_age}")

    db_path = Path(raw.get("db_path", "data/prices.db"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path
    links = _build_links(raw.get("links") or {})
    # A city page under a misspelt name would never be used, so say so now.
    cities = {route.city for route in routes}
    for partner, pages in links.city_pages.items():
        if partner not in links.partners:
            raise ValueError(f"links.city_pages has pages for {partner!r}, which is not under links.partners")
        for city in pages:
            if city not in cities:
                raise ValueError(f"links.city_pages.{partner} has a page for {city!r}, which is no route's city")

    return Config(
        brand=str(raw.get("brand", "FlyFromTirana")),
        channel_handle=str(raw.get("channel_handle", "")),
        origin=str(raw.get("origin", "TIA")).upper(),
        language=str(raw.get("language", "sq")),
        timezone=str(raw.get("timezone", "Europe/Tirane")),
        routes=routes,
        lookahead_days=int(raw.get("lookahead_days", 60)),
        full_run_every_minutes=full_every,
        currency=str(raw.get("currency", "eur")).lower(),
        market=raw.get("market") or None,
        request_delay_seconds=float(raw.get("request_delay_seconds", 0.5)),
        max_price_age_days=None if max_age is None else int(max_age),
        scan_return_flights=bool(raw.get("scan_return_flights", True)),
        rules=rules,
        max_posts_per_run=int(raw.get("max_posts_per_run", 3)),
        quiet_hours=quiet_hours,
        db_path=db_path,
        history_retention_days=int(raw.get("history_retention_days", 45)),
        links=links,
        airlines={str(k).upper(): str(v) for k, v in (raw.get("airlines") or {}).items()},
        premium=_build_premium(raw.get("premium") or {}, rules),
        website=_build_website(raw.get("website") or {}, base_dir, links, str(raw.get("language", "sq"))),
        social=_build_social(raw.get("social") or {}),
        post_photos=bool(raw.get("post_photos", True)),
        invite_links=_invite_links(raw.get("invite_links") or {}),
        growth=_build_growth(raw.get("growth") or {}),
        alerts=_build_alerts(raw.get("alerts") or {}),
        trip_watches=tuple(_build_trip_watch(watch) for watch in raw.get("trip_watches") or []),
    )


def _build_alerts(raw: dict) -> AlertSettings:
    prices = tuple(int(price) for price in raw.get("prices") or AlertSettings.prices)
    if not prices or any(price <= 0 for price in prices) or list(prices) != sorted(set(prices)):
        raise ValueError("alerts.prices must be whole euros from the lowest to the highest, e.g. [20, 30, 50]")
    username = str(raw.get("bot_username") or "").strip().lstrip("@")
    enabled = bool(raw.get("enabled", False))
    if enabled and not username:
        raise ValueError("alerts.bot_username is needed while alerts are on (the bot's name, e.g. FlyFromTiranaBot)")
    return AlertSettings(enabled=enabled, bot_username=username, prices=prices,
                         max_per_person=int(raw.get("max_per_person", 10)),
                         sub_id=str(raw.get("sub_id") or "alerts").strip())


def _build_trip_watch(raw: dict) -> TripWatch:
    name = str(raw.get("name") or "").strip()
    destination = str(raw["destination"]).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", destination):
        raise ValueError(f"trip_watches: destination {destination!r} must be a 3-letter airport code")
    # YAML reads 2026-12-01 as a date already; a quoted "2026-12-01" comes as text.
    earliest, latest = (date.fromisoformat(str(raw[key])) for key in ("earliest_departure", "latest_return"))
    if latest < earliest:
        raise ValueError(f"trip_watches {name!r}: latest_return is before earliest_departure")
    min_days, max_days = int(raw["min_days"]), int(raw.get("max_days") or raw["min_days"])
    if not 1 <= min_days <= max_days:
        raise ValueError(f"trip_watches {name!r}: min_days must be 1 or more, and max_days at least min_days")
    max_price = raw.get("max_price")
    if max_price is not None and float(max_price) <= 0:
        raise ValueError(f"trip_watches {name!r}: max_price must be more than 0 (or empty, for any price)")
    return TripWatch(name=name or f"Trip to {destination}", destination=destination,
                     city=str(raw.get("city") or destination), earliest_departure=earliest, latest_return=latest,
                     min_days=min_days, max_days=max_days, weekend=bool(raw.get("weekend", False)),
                     max_price=float(max_price) if max_price is not None else None)


def _invite_links(raw: dict) -> dict[str, str]:
    """{"website": "https://t.me/+AbC..."} from config.yaml; the places left empty are left out."""
    links = {}
    for source, url in raw.items():
        source, url = str(source).strip().lower(), str(url or "").strip()
        if source not in INVITE_SOURCES:
            raise ValueError(f"invite_links only knows {', '.join(INVITE_SOURCES)}, not {source!r}")
        if url and not url.startswith("https://t.me/"):
            raise ValueError(f"invite_links.{source} must be a Telegram link (https://t.me/+...)")
        if url:
            links[source] = url
    return links


def _build_growth(raw: dict) -> GrowthSettings:
    report_day = _weekday(raw.get("report_day", "monday"), "growth.report_day")
    report_time = raw.get("report_time", "09:00")
    if isinstance(report_time, int):
        raise ValueError('growth.report_time: write the time with quotes ("09:00"), YAML reads 09:00 as a number')
    return GrowthSettings(report_day=report_day, report_time=time.fromisoformat(str(report_time).strip()))


def _build_social(raw: dict) -> SocialSettings:
    post_times = _post_times(raw.get("post_times", ["12:00", "19:30"]))
    publish = tuple(str(name).strip().lower() for name in raw.get("publish") or [])
    unknown = [name for name in publish if name not in SOCIAL_PLATFORMS]
    if unknown:
        raise ValueError(f"social.publish only knows {', '.join(SOCIAL_PLATFORMS)}, not {', '.join(unknown)}")
    keep_days = int(raw.get("keep_pictures_days", 7))
    if keep_days < 1:
        raise ValueError("social.keep_pictures_days must be at least 1")
    weekly_day = _weekday(raw.get("weekly_day"))
    weekly_count = int(raw.get("weekly_count", 5))
    if not 2 <= weekly_count <= 9:   # the cover plus the deals must fit Instagram's 10 pictures
        raise ValueError("social.weekly_count must be between 2 and 9")
    return SocialSettings(
        enabled=bool(raw.get("enabled", False)),
        post_times=post_times,
        handle=str(raw.get("handle") or "").strip(),
        sub_id=str(raw.get("sub_id") or "facebook").strip(),
        hashtags=" ".join(str(raw.get("hashtags") or "").split()),   # one line, single spaces
        publish=publish,
        keep_pictures_days=keep_days,
        weekly_day=weekly_day,
        weekly_count=weekly_count,
        reels=bool(raw.get("reels", False)),
    )


def _post_times(raw) -> tuple[time, ...]:
    """["12:00", "19:30"] -> (time(12, 0), time(19, 30)): 1 to 6 times, in order, an hour or more apart."""
    if not isinstance(raw, list) or not raw:
        raise ValueError('social.post_times must be a list of times, e.g. ["12:00", "19:30"]')
    if any(isinstance(item, int) for item in raw):
        raise ValueError('social.post_times: write the times with quotes ("12:00"), YAML reads 12:00 as a number')
    try:
        times = tuple(time.fromisoformat(str(item).strip()) for item in raw)
    except ValueError as exc:
        raise ValueError(f"social.post_times: {exc} (use HH:MM)") from None
    if len(times) > 6:
        raise ValueError("social.post_times: more than 6 posts a day is too many for Instagram")
    minutes = [t.hour * 60 + t.minute for t in times]
    if any(later - earlier < 60 for earlier, later in zip(minutes, minutes[1:])):
        raise ValueError("social.post_times must be in order of the day and at least an hour apart,"
                         " so the posts never follow each other")
    return times


def _weekday(raw, key: str = "social.weekly_day") -> int | None:
    """"sunday" -> 6; a number 0-6 as it is; empty = None. key: the setting's name, for the error."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, int):
        if not 0 <= raw <= 6:
            raise ValueError(f"{key} must be a weekday name or 0 (Monday) to 6 (Sunday)")
        return raw
    name = str(raw).strip().lower()
    if name not in WEEKDAYS:
        raise ValueError(f"{key} must be a weekday name ({', '.join(WEEKDAYS)}) or empty")
    return WEEKDAYS.index(name)


def _build_website(raw: dict, base_dir: Path, links: LinkSettings, default_language: str) -> WebsiteSettings:
    output_dir = Path(raw.get("output_dir") or "docs")
    if not output_dir.is_absolute():
        output_dir = base_dir / output_dir
    # Without website.languages the site has one language: the one the posts are in.
    languages = tuple(str(code).strip().lower() for code in raw.get("languages") or [default_language])
    # A code becomes a folder name (/en/), so only plain two- or three-letter codes.
    if any(not re.fullmatch(r"[a-z]{2,3}", code) for code in languages) or len(set(languages)) != len(languages):
        raise ValueError("website.languages must be a list of different language codes, e.g. [sq, en, it]")
    sub_id = str(raw.get("sub_id") or "website")
    analytics_id = str(raw.get("google_analytics_id") or "").strip()
    # The ID is written into the page's JavaScript, so only the real shape is let through.
    if analytics_id and not re.fullmatch(r"G-[A-Z0-9]{4,20}", analytics_id):
        raise ValueError("website.google_analytics_id must look like G-XXXXXXXXXX (a GA4 Measurement ID)")
    drive_script = str(raw.get("travelpayouts_drive_script") or "").strip()
    # Like the Analytics ID, this is written into a script on the page: only a plain https address.
    if drive_script and not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._~/?=&%-]*", drive_script):
        raise ValueError("website.travelpayouts_drive_script must be the https address of the Drive script, "
                         "the one in script.src = '...' in the snippet Travelpayouts gives")
    activities = int(raw.get("getyourguide_activities") or 0)
    if not 0 <= activities <= 30:
        raise ValueError("website.getyourguide_activities must be between 0 (no widget) and 30")
    return WebsiteSettings(
        output_dir=output_dir,
        languages=languages,
        sub_id=sub_id,
        url=str(raw.get("url") or "").strip(),
        google_analytics_id=analytics_id,
        travelpayouts_drive_script=drive_script,
        getyourguide_activities=activities,
        google_analytics_ask_first=bool(raw.get("google_analytics_ask_first", True)),
        operator=str(raw.get("operator") or "").strip(),
        contact_email=str(raw.get("contact_email") or "").strip(),
        links=_website_links(raw.get("links") or {}, links, sub_id),
    )


def _website_links(raw: dict, links: LinkSettings, sub_id: str) -> LinkSettings:
    """The site's links: the posts' links, with anything set under website.links laid over them."""
    flight = raw.get("flight") or {}
    partners = dict(links.partners)
    partners.update(_build_partners(raw.get("partners") or {}))
    return replace(
        links,
        sub_id=sub_id,
        flight_base_url=str(flight.get("base_url") or links.flight_base_url).rstrip("/"),
        flight_wrapper=str(flight.get("wrapper") or links.flight_wrapper),
        partners=partners,
    )


def _build_premium(raw: dict, free_rules: DealRules) -> PremiumSettings:
    # Premium uses the free channel's rules, except for the keys set under premium:
    rules = replace(
        free_rules,
        discount_pct=float(raw.get("deal_discount_pct", free_rules.discount_pct)),
        threshold_min_discount_pct=float(raw.get("threshold_min_discount_pct", free_rules.threshold_min_discount_pct)),
    )
    if not 0 < rules.discount_pct < 100:
        raise ValueError("premium.deal_discount_pct must be between 0 and 100")
    scan_every_minutes = float(raw.get("scan_every_minutes") or 0)
    if scan_every_minutes and scan_every_minutes < MIN_PREMIUM_SCAN_MINUTES:
        raise ValueError(f"premium.scan_every_minutes must be 0 (off) or at least {MIN_PREMIUM_SCAN_MINUTES}")
    return PremiumSettings(
        enabled=bool(raw.get("enabled", False)),
        free_delay_hours=float(raw.get("free_delay_hours", 6)),
        scan_every_minutes=scan_every_minutes,
        max_posts_per_run=int(raw.get("max_posts_per_run", 4)),
        join_link=str(raw.get("join_link") or ""),
        rules=rules,
    )


def _build_route(raw: dict) -> Route:
    iata = str(raw["iata"]).strip().upper()
    if len(iata) != 3 or not iata.isalpha():
        raise ValueError(f"route iata {iata!r} must be a 3-letter code")
    threshold = raw.get("absolute_threshold_eur")
    return Route(
        iata=iata,
        city=str(raw["city"]),
        city_en=str(raw.get("city_en") or raw["city"]),
        flag=str(raw.get("flag", "")),
        airport=str(raw.get("airport") or ""),
        absolute_threshold_eur=float(threshold) if threshold is not None else None,
    )


def _build_links(raw: dict) -> LinkSettings:
    flight = raw.get("flight") or {}
    partners = {name: LinkTemplate() for name in DEFAULT_PARTNERS}
    partners.update(_build_partners(raw.get("partners") or {}))
    return LinkSettings(
        sub_id=str(raw.get("sub_id") or ""),
        flight_base_url=str(flight.get("base_url") or "https://www.aviasales.com").rstrip("/"),
        flight_wrapper=str(flight.get("wrapper") or ""),
        partners=partners,
        city_pages={str(name).lower(): {str(city): str(url) for city, url in (pages or {}).items()}
                    for name, pages in (raw.get("city_pages") or {}).items()},
    )


def _build_partners(raw: dict) -> dict[str, LinkTemplate]:
    """{"hotel": {"url": ..., "wrapper": ...}} from config.yaml -> {"hotel": LinkTemplate}."""
    partners = {}
    for name, template in raw.items():
        template = template or {}
        partners[str(name).lower()] = LinkTemplate(
            url=str(template.get("url") or ""),
            wrapper=str(template.get("wrapper") or ""),
        )
    return partners


def require_env(*names: str) -> dict[str, str]:
    """Read environment variables, failing with one clear message listing all missing ones."""
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            f"Missing environment variable(s): {', '.join(missing)}. "
            "Locally: copy .env.example to .env and fill it in. "
            "On the server: set them in the deploy tool's environment variables."
        )
    return values


def load_secrets(*, dry_run: bool, premium: bool = False) -> Secrets:
    """A real run needs everything; a dry run only needs the Travelpayouts token."""
    if dry_run:
        env = require_env("TRAVELPAYOUTS_TOKEN")
        return Secrets(
            travelpayouts_token=env["TRAVELPAYOUTS_TOKEN"],
            travelpayouts_marker=os.environ.get("TRAVELPAYOUTS_MARKER", "").strip(),
        )
    names = ["TRAVELPAYOUTS_TOKEN", "TRAVELPAYOUTS_MARKER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]
    if premium:
        names.append("TELEGRAM_PREMIUM_CHANNEL_ID")
    env = require_env(*names)
    # The drafts go to their own chat, or else to the chat the alerts go to: usually
    # both are the owner's chat with the bot, so one variable can serve both.
    drafts_chat = (os.environ.get("TELEGRAM_DRAFTS_CHAT_ID", "").strip()
                   or os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip())
    return Secrets(
        travelpayouts_token=env["TRAVELPAYOUTS_TOKEN"],
        travelpayouts_marker=env["TRAVELPAYOUTS_MARKER"],
        telegram_bot_token=env["TELEGRAM_BOT_TOKEN"],
        telegram_channel_id=env["TELEGRAM_CHANNEL_ID"],
        telegram_premium_channel_id=env.get("TELEGRAM_PREMIUM_CHANNEL_ID", ""),
        telegram_drafts_chat_id=drafts_chat,
        meta_page_token=os.environ.get("META_PAGE_TOKEN", "").strip(),
    )
