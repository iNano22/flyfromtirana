"""The website in several languages: each one's wording, pages, addresses and the links between them."""
import re
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src import website
from src.config import ConfigError, load_config
from src.formatter import PLACEHOLDER, WORDS, load_template
from src.i18n import LANGUAGE_NAMES, STRINGS_DIR, TOKEN, load_strings, localize
from src.links import LinkBuilder
from src.website import (build_offers, build_site, language_links, language_menu, render_guide_page,
                         render_site, site_guides)
from tests.conftest import NOW, make_quote, seed_history

NOV = date(2026, 11, 1)
LANGUAGES = load_config().website.languages
TEMPLATES = sorted(path.name for path in STRINGS_DIR.parent.glob("site*.html"))


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db",
                   website=replace(base.website, output_dir=tmp_path / "site", google_analytics_id=""),
                   premium=replace(base.premium, enabled=False))


def milan_prices(storage):
    """Bergamo: usually €60, now €19 (a deal) with a €24 flight back. Malpensa: €45, no history."""
    seed_history(storage, [60] * 12, destination="BGY", days_ago=2)
    storage.save_quotes([make_quote(19, NOV + timedelta(days=20), destination="BGY"),
                         make_quote(24, NOV + timedelta(days=27), origin="BGY", destination="TIA"),
                         make_quote(45, NOV + timedelta(days=10), destination="MXP")])


def main_page(config, storage, language):
    return render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.website.links, "12345"),
                       updated_at=NOW, now=NOW, guides=site_guides(config, language), language=language)


def milan_page(config, storage, language):
    guides = site_guides(config, language)
    return render_guide_page(guides["Milan"], guides, build_offers(config, storage, NOW), config,
                             LinkBuilder(config.website.links, "12345"), updated_at=NOW, now=NOW, language=language)


def visible_text(page: str) -> str:
    """The page without its styles and scripts: what a visitor reads."""
    return re.sub(r"<(style|script)\b.*?</\1>", "", page, flags=re.DOTALL)


# --- the wording files ---------------------------------------------------------------

def test_site_has_albanian_english_and_italian():
    assert LANGUAGES == ("sq", "en", "it")                 # config.yaml; Albanian first = at the top of the site
    assert all(code in LANGUAGE_NAMES and code in WORDS for code in LANGUAGES)


@pytest.mark.parametrize("language", LANGUAGES[1:])
def test_every_language_has_the_same_entries(language):
    first, other = load_strings(LANGUAGES[0]), load_strings(language)
    assert set(other.page) == set(first.page)
    assert set(other.js) == set(first.js)
    assert set(other.words) - {"countries"} == set(first.words) - {"countries"}
    assert set(other.words["services"]) == set(first.words["services"])
    for name, text in first.page.items():
        # A translation uses the same values ({CITY}, {MIN_PRICE}, ...) as the Albanian text.
        assert set(PLACEHOLDER.findall(other.page[name])) == set(PLACEHOLDER.findall(text)), name
        assert other.page[name].strip(), name
    for name, text in first.js.items():
        assert set(re.findall(r"\{[a-z]+\}", other.js[name])) == set(re.findall(r"\{[a-z]+\}", text)), name


def test_templates_only_ask_for_texts_that_exist():
    used = set()
    for name in TEMPLATES:
        used |= set(TOKEN.findall(load_template(name)))
    strings = load_strings(LANGUAGES[0])
    assert used <= set(strings.page)
    assert set(strings.page) <= used                       # and no entry is left over, unused


def test_missing_text_or_language_is_a_clear_error():
    with pytest.raises(ConfigError, match="NO_SUCH_TEXT"):
        localize("<p>{T_NO_SUCH_TEXT}</p>", load_strings("sq"))
    with pytest.raises(ConfigError, match="xx"):
        load_strings("xx")


# --- the main page in each language ---------------------------------------------------

def test_english_main_page(config, storage):
    milan_prices(storage)
    page = main_page(config, storage, "en")
    assert '<html lang="en">' in page
    assert "<title>FlyFromTirana: cheap flights from Tirana</title>" in page
    assert '<link rel="canonical" href="https://flyfromtirana.devbay.cloud/en/">' in page
    assert "<h1><span>Fly cheap</span><span>from Tirana</span></h1>" in page
    assert "Today from <b>€19</b> one way" in page and "Updated 9 Oct 2026, 12:00, Tirana time" in page
    # A ticket in the list: English labels, English month names, the same tracked link.
    assert '<span class="lbl">Departure</span>21 Nov' in page and "Wizz Air, direct, 1 h 35 min" in page
    assert "Return from €24" in page and "usually €60" in page and ">Book</a>" in page
    assert '<a href="milan/">Guide to Milan</a>' in page
    assert '<optgroup label="Deals of the moment"><option value="BGY" data-date="2026-11-21" data-price="19">Milan (Bergamo), from €19</option></optgroup>' in page
    # The photos are one folder up, at the top of the site.
    assert '<img src="../img/dest/bgy.webp"' in page and '<img src="../img/guides/milan.webp"' in page
    assert '<img src="../img/services/esim.webp"' in page
    # The script gets its words in English too.
    assert 'var words = {"prev": "previous"' in page and '"months": ["Jan", "Feb"' in page
    assert not PLACEHOLDER.search(page) and "<!--" not in page


def test_italian_main_page_uses_italian_city_and_country_names(config, storage):
    milan_prices(storage)
    page = main_page(config, storage, "it")
    assert '<html lang="it">' in page and "<h1><span>Vola low cost</span><span>da Tirana</span></h1>" in page
    assert '<h3 class="city">Milano <span class="airport">Bergamo</span></h3>' in page
    assert "Milano (Bergamo), da €19" in page and "Milano, Italia" in page
    assert '<span class="lbl">Partenza</span>21 nov' in page and "Wizz Air, diretto, 1 h 35 min" in page
    assert '<a href="milan/">Guida di Milano</a>' in page           # the address keeps the English slug
    assert ">Prenota</a>" in page and not PLACEHOLDER.search(page)


@pytest.mark.parametrize("language", LANGUAGES[1:])
def test_translated_pages_have_no_albanian_left(config, storage, language):
    milan_prices(storage)
    for page in (main_page(config, storage, language), milan_page(config, storage, language)):
        text = visible_text(page)
        # The language dropdown names Albanian in Albanian ("Shqip"); nothing else should be.
        for word in ("Rezervo", "Udhëzues", "Ofert", "nga €", "Nisja", "Kthimi", "vajtje", "zakonisht", "Dita", "Çmim"):
            assert word not in text, f"{word!r} is still on the {language} page"


def test_albanian_main_page_is_at_the_top_and_unchanged(config, storage):
    milan_prices(storage)
    page = main_page(config, storage, "sq")
    assert page == render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.website.links, "12345"),
                               updated_at=NOW, now=NOW, guides=site_guides(config))   # sq is the default
    assert '<html lang="sq">' in page and '<link rel="canonical" href="https://flyfromtirana.devbay.cloud/">' in page
    assert "<h1><span>Fluturo lirë</span><span>nga Tirana</span></h1>" in page
    assert '<img src="./img/dest/bgy.webp"' in page


# --- moving between the languages ------------------------------------------------------

def test_main_pages_link_to_each_other(config, storage):
    milan_prices(storage)
    sq, en = main_page(config, storage, "sq"), main_page(config, storage, "en")
    # The dropdown above the top bar: closed it shows the page's own language as a code...
    assert '<details class="lang">' in sq and '<span class="sr">Gjuha: </span>AL<svg' in sq
    assert '<span class="sr">Language: </span>EN<svg' in en
    # ...and open it lists AL, EN and IT, each leading to the same page in that language.
    assert ('<nav class="lang-menu" aria-label="Gjuha">'
            '<a href="./" lang="sq" hreflang="sq" aria-current="page"><b>AL</b><span>Shqip</span></a>'
            '<a href="./en/" lang="en" hreflang="en"><b>EN</b><span>English</span></a>'
            '<a href="./it/" lang="it" hreflang="it"><b>IT</b><span>Italiano</span></a></nav>') in sq
    assert ('<a href="../" lang="sq" hreflang="sq"><b>AL</b><span>Shqip</span></a>'
            '<a href="../en/" lang="en" hreflang="en" aria-current="page"><b>EN</b><span>English</span></a>'
            '<a href="../it/" lang="it" hreflang="it"><b>IT</b><span>Italiano</span></a>') in en
    # The footer has the same links as a plain row.
    assert ('<div class="flangs"><nav class="langs" aria-label="Language">'
            '<a href="../" lang="sq" hreflang="sq">AL<span class="sr"> Shqip</span></a>') in en
    for page in (sq, en):   # and search engines are told about all of them, on every one
        for code, address in (("sq", ""), ("en", "en/"), ("it", "it/"), ("x-default", "")):
            assert f'<link rel="alternate" hreflang="{code}" href="https://flyfromtirana.devbay.cloud/{address}">' in page


def test_city_page_in_english(config, storage):
    milan_prices(storage)
    page = milan_page(config, storage, "en")
    assert '<html lang="en">' in page
    assert "<title>Cheap flights Tirana – Milan: prices and guide | FlyFromTirana</title>" in page
    assert '<link rel="canonical" href="https://flyfromtirana.devbay.cloud/en/milan/">' in page
    assert '<link rel="alternate" hreflang="it" href="https://flyfromtirana.devbay.cloud/it/milan/">' in page
    assert "from <b>€19</b> one way" in page and "<h2>What to see in Milan</h2>" in page
    assert "<h2>A 3-day itinerary</h2>" in page and 'content: "Day " counter(day)' in page
    assert "<h3>What documents do I need?</h3><p>" in page and "Schengen" in page
    # Two folders up to the photos, one up to the English main page and the other English city pages.
    assert '<img class="dphoto wide" src="../../img/guides/milan.webp"' in page
    assert '<a class="brand" href="../">' in page and '<a href="../rome/">' in page
    assert '<img src="../../img/guides/rome.webp"' in page
    # The same city in the other languages.
    assert '<a href="../../milan/" lang="sq" hreflang="sq"><b>AL</b><span>Shqip</span></a>' in page
    assert '<a href="../../it/milan/" lang="it" hreflang="it"><b>IT</b><span>Italiano</span></a>' in page
    assert not PLACEHOLDER.search(page) and "<!--" not in page


def test_city_page_in_italian_is_about_milano(config, storage):
    milan_prices(storage)
    page = milan_page(config, storage, "it")
    assert "<title>Voli economici Tirana – Milano: prezzi e guida | FlyFromTirana</title>" in page
    assert "<h2>Cosa vedere a Milano</h2>" in page and '<p class="fare-airport">Milano (Bergamo)</p>' in page
    assert 'alt="Veduta di Milano, Italia"' in page and 'content: "Giorno " counter(day)' in page
    assert "Roma</b>" in page                              # the tiles of the other cities, in Italian too


def test_city_without_a_guide_in_a_language_links_to_that_languages_main_page(config, monkeypatch):
    real = website.guides_in
    monkeypatch.setattr(website, "guides_in", lambda language: {} if language == "it" else real(language))
    for links in (language_menu(config, "sq", "../", "milan"), language_links(config, "sq", "../", "milan")):
        assert '<a href="../en/milan/" lang="en" hreflang="en">' in links
        assert '<a href="../it/" lang="it" hreflang="it">' in links                  # no Italian Milan page
    assert website.hreflang_links(config, "milan").count("hreflang=") == 3            # sq, en and x-default


def test_site_with_one_language_has_no_switcher(config, storage):
    milan_prices(storage)
    config = replace(config, website=replace(config.website, languages=("sq",)))
    for page in (main_page(config, storage, "sq"), milan_page(config, storage, "sq")):
        assert 'class="langbar"' not in page and '<details class="lang">' not in page
        assert 'class="flangs"' not in page and "hreflang" not in page


# --- the build ----------------------------------------------------------------------------

def test_build_writes_every_language(config):
    build_site(config, "12345", now=NOW)
    out = config.website.output_dir
    for folder in ("", "en/", "it/"):
        assert (out / folder / "index.html").exists() and (out / folder / "milan" / "index.html").exists(), folder
    assert '<html lang="it">' in (out / "it" / "rome" / "index.html").read_text(encoding="utf-8")
    assert (out / "img" / "dest" / "bgy.webp").exists() and not (out / "en" / "img").exists()   # the photos, once
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    for address in ("", "milan/", "en/", "en/milan/", "it/", "it/zurich/"):
        assert f"<loc>https://flyfromtirana.devbay.cloud/{address}</loc>" in sitemap


def test_languages_setting_is_read_and_checked(tmp_path):
    def load(extra):
        path = tmp_path / "config.yaml"
        path.write_text(f"language: sq\nroutes:\n  - {{ iata: BGY, city: Milan }}\n{extra}", encoding="utf-8")
        return load_config(path).website.languages

    assert load("") == ("sq",)                             # not set: the language of the posts, alone
    assert load("website:\n  languages: [en, sq]\n") == ("en", "sq")
    for bad in ("[sq, sq]", "[sq, '../x']", "[english]"):
        with pytest.raises(ConfigError, match="languages"):
            load(f"website:\n  languages: {bad}\n")
