"""The city guides: the text of each destination's own page on the website.

One file per city and language in content/destinations/<language>/<slug>.yaml
(see sq/milan.yaml for the shape). src/website.py turns each one into the page
/<slug>/ (Albanian) or /<language>/<slug>/, with the current prices on top and
the guide underneath. A city without a file in a language simply has no page
in that language. content/countries/<language>.yaml adds what is the same for
every city of a country: the documents an Albanian citizen needs to get in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.config import PROJECT_ROOT, ConfigError

GUIDES_DIR = PROJECT_ROOT / "content" / "destinations"   # one folder per language inside
COUNTRIES_DIR = PROJECT_ROOT / "content" / "countries"   # one file per language inside
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")   # "milan", "new-york": safe in a web address


@dataclass(frozen=True)
class Entry:
    """A named thing with a sentence or two about it: a sight, a neighbourhood, a dish, a day trip."""

    name: str   # "Duomo di Milano"
    text: str


@dataclass(frozen=True)
class Question:
    q: str   # "Malpensa apo Bergamo?"
    a: str   # the answer


@dataclass(frozen=True)
class Guide:
    """One city's guide. Only the first three are required; a part that is empty is left off the page."""

    city: str                  # the city name used for its routes in config.yaml, e.g. "Milan"
    slug: str                  # the page's address: "milan" -> /milan/
    intro: str                 # two or three sentences under the headline
    name: str = ""             # the city's name in this guide's language, when it differs: "Milano"
    tagline: str = ""          # a few words under the city's name on the main page
    sights: list[Entry] = field(default_factory=list)       # what to see
    itinerary: list[str] = field(default_factory=list)      # a plan, one entry per day
    areas: list[Entry] = field(default_factory=list)        # where to stay
    food: list[Entry] = field(default_factory=list)         # what to eat
    airports: dict[str, str] = field(default_factory=dict)  # airport code -> how to reach the centre from it
    transport: str = ""        # getting around the city
    daytrips: list[Entry] = field(default_factory=list)     # places a day trip away
    budget: str = ""           # typical prices
    when: str = ""             # the best time to go
    tips: list[str] = field(default_factory=list)           # short practical tips
    faq: list[Question] = field(default_factory=list)       # questions people ask about this city

    @property
    def display_name(self) -> str:
        """The city's name as this guide's language writes it ("Milano"), else the route's name ("Milan")."""
        return self.name or self.city


def load_guides(language: str = "sq", *, directory: Path | None = None) -> dict[str, Guide]:
    """Every guide written in `language`, keyed by city name. A broken file is a ConfigError naming it.

    directory: read the guides from this folder instead of content/destinations/<language>.
    """
    directory = directory or GUIDES_DIR / language
    guides: dict[str, Guide] = {}
    slugs: dict[str, str] = {}
    if not directory.is_dir():
        return guides
    for path in sorted(directory.glob("*.yaml")):
        try:
            guide = _build_guide(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except KeyError as exc:
            raise ConfigError(f"{path.name} is missing the required key {exc}") from None
        except (TypeError, ValueError, AttributeError, yaml.YAMLError) as exc:
            raise ConfigError(f"{path.name} is not a valid guide: {exc}") from None
        if guide.city in guides or guide.slug in slugs:
            other = slugs.get(guide.slug) or guide.city
            raise ConfigError(f"{path.name}: the city or slug is already used by another guide ({other})")
        guides[guide.city] = guide
        slugs[guide.slug] = path.name
    return guides


def _build_guide(raw: dict) -> Guide:
    slug = str(raw["slug"]).strip()
    if not SLUG.fullmatch(slug):
        raise ValueError(f"slug {slug!r} may only have lowercase letters, digits and hyphens")
    return Guide(
        city=str(raw["city"]).strip(),
        slug=slug,
        intro=_text(raw["intro"]),
        name=_text(raw.get("name") or ""),
        tagline=_text(raw.get("tagline") or ""),
        sights=_entries(raw.get("sights")),
        itinerary=[_text(day) for day in raw.get("itinerary") or []],
        areas=_entries(raw.get("areas")),
        food=_entries(raw.get("food")),
        airports={str(code).upper(): _text(text) for code, text in (raw.get("airports") or {}).items()},
        transport=_text(raw.get("transport") or ""),
        daytrips=_entries(raw.get("daytrips")),
        budget=_text(raw.get("budget") or ""),
        when=_text(raw.get("when") or ""),
        tips=[_text(tip) for tip in raw.get("tips") or []],
        faq=[Question(q=_text(item["q"]), a=_text(item["a"])) for item in raw.get("faq") or []],
    )


def _entries(raw: list | None) -> list[Entry]:
    return [Entry(name=_text(item["name"]), text=_text(item["text"])) for item in raw or []]


def load_entry_rules(language: str = "sq", *, path: Path | None = None) -> dict[str, str]:
    """Country code ("it") -> what an Albanian citizen needs to enter that country, in `language`."""
    path = path or COUNTRIES_DIR / f"{language}.yaml"
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(code).lower(): _text(text) for code, text in raw.items()}
    except (TypeError, AttributeError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path.name} is not valid: {exc}") from None


def _text(value) -> str:
    """One paragraph: YAML's line breaks and extra spaces become single spaces."""
    return " ".join(str(value).split())
