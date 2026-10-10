"""Loads config.yaml and the secret environment variables.

Everything the rest of the code needs from configuration is turned into small
dataclasses here, so a typo in config.yaml fails at startup with a clear
message instead of crashing halfway through a run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Partner link slots that always exist, so the template can reference
# {HOTEL_LINK} etc. even before they are filled in config.yaml.
DEFAULT_PARTNERS = ("hotel", "esim", "insurance", "compensation", "car_rental")


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
    wrapper: str = ""  # optional tracking redirect; {url} is replaced by the encoded target


@dataclass(frozen=True)
class LinkSettings:
    sub_id: str = ""
    flight_base_url: str = "https://www.aviasales.com"
    flight_wrapper: str = ""
    partners: dict[str, LinkTemplate] = field(default_factory=dict)  # "hotel" -> LinkTemplate


@dataclass(frozen=True)
class PremiumSettings:
    """Paid early-access channel. The free channel only gets a deal free_delay_hours later."""

    enabled: bool = False
    free_delay_hours: float = 6
    max_posts_per_run: int = 4
    join_link: str = ""                                    # paid invite link, advertised in free posts
    rules: DealRules = field(default_factory=DealRules)    # usually looser than the free channel's


@dataclass(frozen=True)
class WebsiteSettings:
    """The static site built by src/website.py (the docs/ folder, served by the `web` container)."""

    output_dir: Path = PROJECT_ROOT / "docs"
    sub_id: str = "website"   # Travelpayouts SubID for links on the site ("telegram" is used in posts)
    url: str = ""             # public address, for the page's canonical/og:url tags; empty = left out


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
    currency: str
    market: str | None
    request_delay_seconds: float
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


@dataclass(frozen=True)
class Secrets:
    travelpayouts_token: str
    travelpayouts_marker: str = ""
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_premium_channel_id: str = ""


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

    db_path = Path(raw.get("db_path", "data/prices.db"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    return Config(
        brand=str(raw.get("brand", "FlyFromTirana")),
        channel_handle=str(raw.get("channel_handle", "")),
        origin=str(raw.get("origin", "TIA")).upper(),
        language=str(raw.get("language", "sq")),
        timezone=str(raw.get("timezone", "Europe/Tirane")),
        routes=routes,
        lookahead_days=int(raw.get("lookahead_days", 60)),
        currency=str(raw.get("currency", "eur")).lower(),
        market=raw.get("market") or None,
        request_delay_seconds=float(raw.get("request_delay_seconds", 0.5)),
        scan_return_flights=bool(raw.get("scan_return_flights", True)),
        rules=rules,
        max_posts_per_run=int(raw.get("max_posts_per_run", 3)),
        quiet_hours=quiet_hours,
        db_path=db_path,
        history_retention_days=int(raw.get("history_retention_days", 45)),
        links=_build_links(raw.get("links") or {}),
        airlines={str(k).upper(): str(v) for k, v in (raw.get("airlines") or {}).items()},
        premium=_build_premium(raw.get("premium") or {}, rules),
        website=_build_website(raw.get("website") or {}, base_dir),
    )


def _build_website(raw: dict, base_dir: Path) -> WebsiteSettings:
    output_dir = Path(raw.get("output_dir") or "docs")
    if not output_dir.is_absolute():
        output_dir = base_dir / output_dir
    return WebsiteSettings(
        output_dir=output_dir,
        sub_id=str(raw.get("sub_id") or "website"),
        url=str(raw.get("url") or "").strip(),
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
    return PremiumSettings(
        enabled=bool(raw.get("enabled", False)),
        free_delay_hours=float(raw.get("free_delay_hours", 6)),
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
    for name, template in (raw.get("partners") or {}).items():
        template = template or {}
        partners[str(name).lower()] = LinkTemplate(
            url=str(template.get("url") or ""),
            wrapper=str(template.get("wrapper") or ""),
        )
    return LinkSettings(
        sub_id=str(raw.get("sub_id") or ""),
        flight_base_url=str(flight.get("base_url") or "https://www.aviasales.com").rstrip("/"),
        flight_wrapper=str(flight.get("wrapper") or ""),
        partners=partners,
    )


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
    return Secrets(
        travelpayouts_token=env["TRAVELPAYOUTS_TOKEN"],
        travelpayouts_marker=env["TRAVELPAYOUTS_MARKER"],
        telegram_bot_token=env["TELEGRAM_BOT_TOKEN"],
        telegram_channel_id=env["TELEGRAM_CHANNEL_ID"],
        telegram_premium_channel_id=env.get("TELEGRAM_PREMIUM_CHANNEL_ID", ""),
    )
