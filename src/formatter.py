"""Turns a Deal into the text of a post.

The wording lives in templates/<language>.txt, so it can be changed without
touching code. Template rules:
  {NAME}      replaced with a value (full list in build_values below)
  [[ ... ]]   optional part: removed if any {NAME} inside it has no value
  A line with a {NAME} that has no value (outside [[ ]]) is removed entirely.
Telegram gets the text with parse_mode=HTML, so templates can use
<a href="...">, <b> and <i>. Values are HTML-escaped automatically.

Adding a language = add templates/<lang>.txt and a WORDS entry below.
"""
from __future__ import annotations

import html
import math
import re
from datetime import date, timedelta

from src.config import PROJECT_ROOT, ConfigError
from src.deals import Deal
from src.links import LinkBuilder

TEMPLATES_DIR = PROJECT_ROOT / "templates"
DEFAULT_HOTEL_NIGHTS = 3  # hotel check-out when there's no return flight to go by

# Words the code has to produce itself (dates, stops, duration), per language.
WORDS = {
    "sq": {
        "months": ["Jan", "Shk", "Mar", "Pri", "Maj", "Qer", "Korr", "Gush", "Sht", "Tet", "Nën", "Dhj"],
        "direct": "direkt",
        "with_stops": "me ndalesë",
        "hours": "orë",
        "minutes": "min",
    },
    "en": {
        "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "direct": "direct",
        "with_stops": "with stops",
        "hours": "h",
        "minutes": "min",
    },
}

PLACEHOLDER = re.compile(r"\{([A-Z_]+)\}")
OPTIONAL_PART = re.compile(r"\[\[(.*?)\]\]")


def format_post(deal: Deal, links: LinkBuilder, *, language: str, channel_handle: str,
                airline_names: dict[str, str], template: str | None = None,
                premium_link: str | None = None) -> str:
    """template: file name in templates/ without .txt (default: the language, e.g. "sq")."""
    values = build_values(deal, links, language=language, channel_handle=channel_handle,
                          airline_names=airline_names, premium_link=premium_link)
    return render(load_template(template or language), values)


def load_template(name: str) -> str:
    """templates/<name>.txt, or templates/<name> when the name has its own extension (e.g. site.html)."""
    path = TEMPLATES_DIR / (name if "." in name else f"{name}.txt")
    if not path.exists():
        raise ConfigError(f"No post template {name!r} (expected {path})")
    return path.read_text(encoding="utf-8")


def build_values(deal: Deal, links: LinkBuilder, *, language: str, channel_handle: str,
                 airline_names: dict[str, str], premium_link: str | None = None) -> dict[str, str | None]:
    """Every {NAME} a template can use. None = no value (the line/part is left out)."""
    if language not in WORDS:
        raise ConfigError(f"Unsupported language {language!r}; add it to WORDS in src/formatter.py")
    words = WORDS[language]
    best = deal.best
    checkin = best.depart_date
    checkout = deal.return_quote.depart_date if deal.return_quote else checkin + timedelta(days=DEFAULT_HOTEL_NIGHTS)

    text = {
        "CITY": deal.route.city,
        "CITY_UPPER": deal.route.city.upper(),
        "AIRPORT": deal.route.airport or None,
        "FLAG": deal.route.flag,
        "PRICE": format_price(best.price),
        "MEDIAN": f"{deal.median:.0f}" if deal.median else None,
        "DATES": format_dates([q.depart_date for q in deal.quotes], words["months"]),
        "AIRLINE": airline_names.get(best.airline, best.airline) or None,
        "STOPS": words["direct"] if best.is_direct else words["with_stops"],
        "DURATION": format_duration(best.duration_min, words),
        "RETURN_PRICE": format_price(deal.return_quote.price) if deal.return_quote else None,
        "CHANNEL": channel_handle,
        "PREMIUM_HOURS": str(deal.premium_lead_hours) if deal.premium_lead_hours else None,
    }
    urls = {"FLIGHT_LINK": links.flight(best), "PREMIUM_LINK": premium_link or None}
    for name in links.partner_names:  # hotel -> HOTEL_LINK, esim -> ESIM_LINK, ...
        urls[f"{name.upper()}_LINK"] = links.partner(name, deal.route, checkin, checkout)

    values = {key: html.escape(value, quote=False) if value else None for key, value in text.items()}
    values.update({key: html.escape(value, quote=True) if value else None for key, value in urls.items()})
    return values


def render(template: str, values: dict[str, str | None]) -> str:
    lines = []
    for line in template.splitlines():
        if not line.strip():
            lines.append("")  # keep blank lines the template has on purpose
            continue
        # 1) Optional [[parts]]: keep the inside if all its values exist, else drop it.
        line = OPTIONAL_PART.sub(lambda m: m.group(1) if _has_values(m.group(1), values) else "", line)
        # 2) Drop the whole line if a value is missing, or nothing is left of it.
        if not line.strip() or not _has_values(line, values):
            continue
        lines.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], line).strip())
    return "\n".join(lines).strip()


def _has_values(text: str, values: dict[str, str | None]) -> bool:
    for name in PLACEHOLDER.findall(text):
        if name not in values:
            raise ConfigError(f"Unknown placeholder {{{name}}} in post template")
        if not values[name]:
            return False
    return True


def format_price(price: float) -> str:
    """Whole euros, rounded up: never advertise a price lower than the real one."""
    return str(math.ceil(round(price, 2)))


def format_dates(dates: list[date], month_names: list[str]) -> str:
    """'15 Tet, 18 Tet, 2 Nën' in date order."""
    return ", ".join(f"{d.day} {month_names[d.month - 1]}" for d in sorted(set(dates)))


def format_duration(minutes: int | None, words: dict) -> str | None:
    """'2 orë 15 min', '55 min', or None if unknown."""
    if not minutes:
        return None
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} {words['hours']}")
    if mins:
        parts.append(f"{mins} {words['minutes']}")
    return " ".join(parts)
