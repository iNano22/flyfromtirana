"""The city guides: the text of each destination's own page on the website.

One file per city in content/destinations/<slug>.yaml (see milan.yaml for the
shape). src/website.py turns each one into the page /<slug>/, with the
current prices on top and the guide underneath. A city without a file simply
has no page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config import PROJECT_ROOT, ConfigError

GUIDES_DIR = PROJECT_ROOT / "content" / "destinations"
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")   # "milan", "new-york": safe in a web address


@dataclass(frozen=True)
class Sight:
    name: str   # "Duomo di Milano"
    text: str   # one sentence about it


@dataclass(frozen=True)
class Guide:
    city: str                  # the city name used for its routes in config.yaml, e.g. "Milan"
    slug: str                  # the page's address: "milan" -> /milan/
    intro: str                 # two or three sentences under the headline
    sights: list[Sight]        # what to see
    airports: dict[str, str]   # airport code -> how to get from that airport to the centre
    budget: str                # typical prices
    when: str                  # the best time to go
    tips: list[str]            # short practical tips


def load_guides(directory: Path = GUIDES_DIR) -> dict[str, Guide]:
    """Every guide in the folder, keyed by city name. A broken file is a ConfigError naming it."""
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
        sights=[Sight(name=_text(s["name"]), text=_text(s["text"])) for s in raw.get("sights") or []],
        airports={str(code).upper(): _text(text) for code, text in (raw.get("airports") or {}).items()},
        budget=_text(raw.get("budget") or ""),
        when=_text(raw.get("when") or ""),
        tips=[_text(tip) for tip in raw.get("tips") or []],
    )


def _text(value) -> str:
    """One paragraph: YAML's line breaks and extra spaces become single spaces."""
    return " ".join(str(value).split())
