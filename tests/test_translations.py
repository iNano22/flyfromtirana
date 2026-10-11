"""The city guides in the other languages: same cities, same shape, same facts as the Albanian ones.

A guide is written once per language (content/destinations/<language>/<slug>.yaml).
These tests catch a translation that lost a part, an entry or a number.
"""
import re
from collections import Counter

import pytest
import yaml

from src.config import load_config
from src.guides import COUNTRIES_DIR, GUIDES_DIR, load_entry_rules, load_guides

LANGUAGES = load_config().website.languages   # config.yaml: [sq, en, it]
SOURCE = LANGUAGES[0]                         # the language the guides are written in first
TRANSLATIONS = LANGUAGES[1:]                  # the languages they are translated into
LISTS = ("sights", "itinerary", "areas", "food", "daytrips", "tips", "faq")
SLUGS = sorted(path.stem for path in (GUIDES_DIR / SOURCE).glob("*.yaml"))


def read(language: str, slug: str) -> dict:
    path = GUIDES_DIR / language / f"{slug}.yaml"
    assert path.exists(), f"{path} is missing: every guide needs a file in every language"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def numbers(value) -> Counter:
    """Every number written in digits anywhere in a guide: prices, minutes, years."""
    return Counter(re.findall(r"\d+", yaml.safe_dump(value, allow_unicode=True)))


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_every_guide_loads_in_every_language(language):
    guides = load_guides(language)
    assert sorted(guide.slug for guide in guides.values()) == SLUGS
    assert set(guides) == set(load_guides(SOURCE))         # keyed by the same city names


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("language", TRANSLATIONS)
def test_translated_guide_has_the_same_shape(language, slug):
    source, translated = read(SOURCE, slug), read(language, slug)
    assert translated["city"] == source["city"] and translated["slug"] == source["slug"]   # never translated
    assert set(translated) - {"name"} == set(source) - {"name"}                             # the same parts
    for part in LISTS:
        assert len(translated[part]) == len(source[part]), f"{part}: a different number of entries"
    assert list(translated["airports"]) == list(source["airports"])                         # the same airports
    for part in ("intro", "tagline", "transport", "budget", "when"):
        assert str(translated[part]).strip(), f"{part} is empty"


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("language", TRANSLATIONS)
def test_translated_guide_keeps_every_number(language, slug):
    # Prices, times and years must survive the translation. It may add a number of its
    # own ("19th century" for "shekulli XIX"), but never lose or change one.
    lost = numbers(read(SOURCE, slug)) - numbers(read(language, slug))
    assert not lost, f"numbers in the Albanian guide that the {language} one lacks: {dict(lost)}"


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_entry_rules_cover_the_same_countries(language):
    source, translated = load_entry_rules(SOURCE), load_entry_rules(language)
    assert (COUNTRIES_DIR / f"{language}.yaml").exists()
    assert set(translated) == set(source) and all(translated.values())
    for country in source:
        lost = numbers(source[country]) - numbers(translated[country])
        assert not lost, f"{country}: numbers lost in the {language} entry rules: {dict(lost)}"
