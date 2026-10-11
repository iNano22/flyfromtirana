"""The city guides (content/destinations) and the pages the website builds from them."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.config import ConfigError, load_config
from src.formatter import PLACEHOLDER
from src.guides import Guide, load_entry_rules, load_guides
from src.links import LinkBuilder
from src.photos import ASSETS_DIR
from src.website import (build_offers, build_site, country_code, guide_tiles, render_guide_page, render_site,
                         site_guides)
from tests.conftest import NOW, make_quote, seed_history

NOV = date(2026, 11, 1)


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


# --- the guide files -------------------------------------------------------------

def test_every_city_has_a_guide_that_covers_its_airports():
    config = load_config()
    guides = load_guides()
    airports = {}
    for route in config.routes:
        airports.setdefault(route.city, set()).add(route.iata)
    assert set(guides) == set(airports)                    # one guide per city we fly to, and no stray ones
    for city, guide in guides.items():
        assert set(guide.airports) == airports[city], city # how to reach the centre, for each of its airports
        assert guide.intro and guide.budget and guide.when and guide.transport and guide.tips, city
        assert guide.tagline, city                         # the line on its tile on the main page
        # Every guide has all the parts of a full page.
        assert len(guide.sights) >= 5 and len(guide.itinerary) >= 3 and len(guide.areas) >= 3, city
        assert len(guide.food) >= 3 and len(guide.daytrips) >= 3 and len(guide.faq) >= 2, city
    # Every wide photo belongs to a guide (the file is named after the guide's slug).
    slugs = {guide.slug for guide in guides.values()}
    assert {path.stem for path in (ASSETS_DIR / "img" / "guides").glob("*.webp")} <= slugs


def test_every_country_we_fly_to_has_its_entry_rules():
    rules = load_entry_rules()
    countries = {country_code(route.flag) for route in load_config().routes}
    assert countries <= set(rules)                         # the "Çfarë dokumentesh duhen?" answer
    assert "pa vizë" in rules["it"] and "vizë vizitori" in rules["gb"]
    assert load_entry_rules(path=load_config().db_path.parent / "no-such-file.yaml") == {}


def test_guides_are_listed_in_the_order_of_the_routes():
    config = load_config()
    assert list(site_guides(config))[:3] == ["Milan", "Rome", "Bologna"]
    only_vienna = replace(config, routes=[r for r in config.routes if r.iata == "VIE"])
    assert list(site_guides(only_vienna)) == ["Vienna"]    # a guide without a route gets no page


def test_broken_guide_file_names_the_file(tmp_path):
    (tmp_path / "nowhere.yaml").write_text("city: Nowhere\nslug: No Where\nintro: x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nowhere.yaml.*slug"):
        load_guides(directory=tmp_path)
    (tmp_path / "nowhere.yaml").write_text("city: Nowhere\nintro: x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nowhere.yaml.*slug"):
        load_guides(directory=tmp_path)
    assert load_guides(directory=tmp_path / "missing") == {}   # no folder: no guides, no error


# --- a city page -----------------------------------------------------------------

def milan_page(config, storage):
    guides = site_guides(config)
    offers = build_offers(config, storage, NOW)
    return render_guide_page(guides["Milan"], guides, offers, config, LinkBuilder(config.website.links, "12345"),
                             updated_at=NOW, now=NOW)


def test_city_page_shows_prices_then_the_guide(config, storage):
    milan_prices(storage)
    page = milan_page(config, storage)

    assert "<title>Fluturime të lira Tirana – Milan: çmime dhe udhëzues | FlyFromTirana</title>" in page
    assert '<link rel="canonical" href="https://flyfromtirana.devbay.cloud/milan/">' in page
    assert '<body class="c-it">' in page                                     # Italy's colours
    assert 'nga <b>€19</b> vajtje' in page                                   # the cheapest of the city's airports
    # One ticket per airport, cheapest first; only Bergamo is a deal.
    assert page.count('<li class="fare') == 2
    assert page.index("<span class=\"code\">BGY</span>") < page.index("<span class=\"code\">MXP</span>")
    assert '<li class="fare deal">' in page and page.count("<span class=\"tag deal\">") == 1
    assert "Kthimi nga €24" in page and "zakonisht €60 <b>-68%</b>" in page
    assert 'class="btn-book" href="https://aviasales.tp.st/' in page         # the site's own tracked links
    # The guide, part by part.
    assert "<li><b>Duomo di Milano</b>" in page
    assert '<h2>Itinerar për 3 ditë</h2><ol class="days"><li>Duomo me ngjitje në tarracë' in page
    assert "<h2>Ku të qëndrosh</h2>" in page and "<li><b>Navigli</b>" in page
    assert "<h2>Çfarë të hash</h2>" in page and "<li><b>Risotto alla milanese</b>" in page
    assert "<h3>Malpensa (MXP)</h3>" in page and "<h3>Bergamo (BGY)</h3>" in page
    assert "<h2>Si të lëvizësh në qytet</h2><p>Qendra përshkohet në këmbë" in page
    assert "<h2>Udhëtime ditore nga Milan</h2>" in page and "<li><b>Liqeni i Komos</b>" in page
    assert "Malpensa Express" in page and "<h3>Sa kushton</h3>" in page and "<h3>Kur të shkosh</h3>" in page
    # Questions: the city's own, the country's entry rules, and the one every page has.
    assert '<div class="qa"><h3>Malpensa apo Bergamo: cili aeroport është më i mirë?</h3>' in page
    assert "<h3>Çfarë dokumentesh duhen?</h3><p>Italia është në zonën Shengen." in page
    assert "<h3>Kur janë biletat më të lira?</h3>" in page
    # The jump links at the top go to parts that exist.
    for anchor in ("shiko", "itinerari", "fjetja", "ushqimi", "aeroporti", "transporti", "udhetime-ditore", "pyetje"):
        assert f'<a href="#{anchor}"' in page and f'id="{anchor}"' in page, anchor
    # Links back to the main page and on to the other cities, but not to itself.
    assert '<a class="brand" href="../">' in page
    assert '<img class="dphoto wide" src="../img/guides/milan.webp"' in page  # the city's own wide photo...
    assert "../img/dest/" not in page                                        # ...instead of a route's photo
    assert '<meta property="og:image" content="https://flyfromtirana.devbay.cloud/img/guides/milan.webp">' in page
    assert 'Made by <a href="https://devbay.cloud">Devbay.cloud</a>' in page
    # "Destinacione të tjera": a photo tile for each of the 18 other cities.
    others = page[page.index('<ul class="gtiles"'):page.index("</ul>", page.index('<ul class="gtiles"'))]
    assert others.count('<li class="gtile') == 18 and 'href="../milan/"' not in others
    assert '<a href="../rome/">' in others and '<img src="../img/guides/rome.webp"' in others
    assert "Koloseu, Vatikani dhe pasta" in others and '<use href="#i-right"/>' in others
    assert '<symbol id="i-right"' in page                                    # the tiles' arrow is drawn on this page too
    assert "googletagmanager" not in page and "Cilësimet e cookies" not in page   # analytics is off
    assert "<!--" not in page and not PLACEHOLDER.search(page)


def test_city_page_without_prices_says_so(config, storage):
    page = milan_page(config, storage)
    assert "Për momentin nuk kemi çmime për këtë destinacion." in page
    assert '<li class="fare' not in page and 'class="from-price"' not in page
    assert "<li><b>Duomo di Milano</b>" in page                              # the guide is still there
    assert not PLACEHOLDER.search(page)


def test_build_writes_a_page_per_city_and_a_sitemap(config):
    build_site(config, "12345", now=NOW)
    out = config.website.output_dir
    cities = site_guides(config)
    assert len(cities) == 19
    for guide in cities.values():
        assert (out / guide.slug / "index.html").exists(), guide.slug
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    # The main page and every city, once per language of the site.
    assert sitemap.count("<url>") == 20 * len(config.website.languages)
    assert "<loc>https://flyfromtirana.devbay.cloud/</loc>" in sitemap
    assert "<loc>https://flyfromtirana.devbay.cloud/milan/</loc><lastmod>2026-10-09</lastmod>" in sitemap
    assert "Sitemap: https://flyfromtirana.devbay.cloud/sitemap.xml" in (out / "robots.txt").read_text()


def test_no_sitemap_without_a_site_address(config):
    config = replace(config, website=replace(config.website, url=""))
    build_site(config, "12345", now=NOW)
    out = config.website.output_dir
    assert (out / "milan" / "index.html").exists()
    assert not (out / "sitemap.xml").exists() and not (out / "robots.txt").exists()
    assert 'rel="canonical"' not in (out / "milan" / "index.html").read_text(encoding="utf-8")


def test_main_page_links_to_the_city_pages(config, storage):
    milan_prices(storage)
    guides = site_guides(config)
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.website.links, "1"),
                       updated_at=NOW, now=NOW, guides=guides)
    assert 'id="udhezues"' in page and '<span class="count">19</span>' in page
    tiles = page[page.index('<ul class="strip gtiles"'):page.index("</ul>", page.index('<ul class="strip gtiles"'))]
    assert tiles.count('<li class="gtile') == 19
    milan = tiles[tiles.index('<a href="milan/">'):tiles.index('<a href="rome/">')]
    assert '<img src="./img/guides/milan.webp"' in milan                      # its wide photo
    assert "<small>nga</small> €19" in milan                                  # the city's cheapest price
    assert "Milan</b>" in milan and "Modë, Duomo dhe aperitiv në Navigli" in milan and "Lexo udhëzuesin" in milan
    rome = tiles[tiles.index('<a href="rome/">'):tiles.index('<a href="bologna/">')]
    assert "gprice" not in rome and "Rome</b>" in rome                        # no price yet: no sticker
    assert '<a href="milan/">Udhëzues për Milan</a>' in page                  # on Milan's tickets
    assert "Udhëzues për 19 qytete" in page                                   # footer
    assert 'Made by <a href="https://devbay.cloud">Devbay.cloud</a>' in page
    without = render_site([], config, LinkBuilder(config.website.links, "1"), updated_at=NOW, now=NOW)
    assert 'id="udhezues"' not in without and "Udhëzues" not in without


def test_city_without_a_wide_photo_still_gets_a_tile_and_a_page(config, storage):
    milan_prices(storage)
    plain = Guide(city="Milan", slug="no-photo-here", tagline="", intro="Intro.", sights=[], airports={},
                  budget="", when="", tips=[])
    guides = {"Milan": plain}
    tile = guide_tiles(guides, build_offers(config, storage, NOW), config)
    assert '<li class="gtile c-it">' in tile and "<img" not in tile          # the country's colours instead
    assert '<a href="no-photo-here/">' in tile and "gtag" not in tile and not PLACEHOLDER.search(tile)
    page = render_guide_page(plain, guides, build_offers(config, storage, NOW), config,
                             LinkBuilder(config.website.links, "1"), updated_at=NOW, now=NOW)
    assert '<img class="dphoto" src="../img/dest/mxp.webp"' in page          # falls back to a route's photo
    assert "dphoto wide" not in page.split("</style>")[1] and not PLACEHOLDER.search(page)
    # A short guide: the parts it lacks are left off, with their headings and jump links.
    body = page.split("</style>")[1]
    for missing in ("Itinerar", "Ku të qëndrosh", "Çfarë të hash", "Udhëtime ditore", "Si të lëvizësh"):
        assert missing not in body, missing
    assert 'href="#itinerari"' not in body and 'href="#pyetje"' in body      # the questions block is always there
    assert "<h3>Çfarë dokumentesh duhen?</h3>" in body


# --- Google Analytics ---------------------------------------------------------------

def with_analytics(config, *, ask_first):
    return replace(config, website=replace(config.website, google_analytics_id="G-TEST12345",
                                           google_analytics_ask_first=ask_first))


def analytics_pages(config, storage):
    """The main page and a city page, built with this config."""
    milan_prices(storage)
    guides = site_guides(config)
    offers = build_offers(config, storage, NOW)
    links = LinkBuilder(config.website.links, "1")
    return (render_site(offers, config, links, updated_at=NOW, now=NOW, guides=guides),
            render_guide_page(guides["Milan"], guides, offers, config, links, updated_at=NOW, now=NOW))


def test_analytics_can_wait_for_the_visitors_answer(config, storage):
    for page in analytics_pages(with_analytics(config, ask_first=True), storage):
        assert "var id = 'G-TEST12345'" in page
        assert '<div class="consent" id="consent" role="dialog"' in page          # no data-auto: wait for a yes
        assert "A na lejon të vendosim cookies" in page and ">Pranoj</button>" in page
        assert "Në rregull" not in page and ">Refuzoj</button>" in page
        assert '<script async src="https://www.googletagmanager.com' not in page   # the script adds the tag itself
        assert 'data-consent-open="1">Cilësimet e cookies</a>' in page
        assert "<!--" not in page and not PLACEHOLDER.search(page)


def test_analytics_can_count_from_the_first_page_view(config, storage):
    for page in analytics_pages(with_analytics(config, ask_first=False), storage):
        assert '<div class="consent" id="consent" data-auto="1" role="dialog"' in page   # the script starts right away
        assert "Kjo faqe përdor Google Analytics" in page and ">Në rregull</button>" in page
        assert "A na lejon" not in page and "Pranoj" not in page
        assert ">Refuzoj</button>" in page                                        # the visitor can still say no
        assert "<!--" not in page and not PLACEHOLDER.search(page)


def test_config_counts_from_the_first_page_view(tmp_path):
    assert load_config().website.google_analytics_ask_first is False    # config.yaml: the owner's choice
    path = tmp_path / "config.yaml"
    path.write_text("routes:\n  - { iata: BGY, city: Milan }\n", encoding="utf-8")
    assert load_config(path).website.google_analytics_ask_first is True  # not set: ask first


# --- Travelpayouts Drive ---------------------------------------------------------------

def test_drive_script_is_in_the_head_of_every_page(config, storage):
    config = replace(config, website=replace(config.website,
                                             travelpayouts_drive_script="https://drive.example/abc.js?t=1"))
    for page in analytics_pages(config, storage):
        head = page[:page.index("</head>")]
        assert "script.src = 'https://drive.example/abc.js?t=1';" in head
        assert '<script nowprocket data-noptimize="1" data-cfasync="false"' in head   # the snippet as Travelpayouts gives it
        assert "<!--" not in page and not PLACEHOLDER.search(page)


def test_no_drive_script_without_one_in_the_config(config, storage):
    assert load_config().website.travelpayouts_drive_script.startswith("https://")   # config.yaml has the site's
    config = replace(config, website=replace(config.website, travelpayouts_drive_script=""))
    for page in analytics_pages(config, storage):
        assert "nowprocket" not in page and "document.head.appendChild" not in page[:page.index("</head>")]


def test_drive_script_must_be_a_plain_https_address(tmp_path):
    def load(value):
        path = tmp_path / "config.yaml"
        path.write_text("routes:\n  - { iata: BGY, city: Milan }\nwebsite:\n"
                        f"  travelpayouts_drive_script: {value}\n", encoding="utf-8")
        return load_config(path).website.travelpayouts_drive_script

    assert load('"https://emrldtp.cc/abc.js?t=1"') == "https://emrldtp.cc/abc.js?t=1"
    assert load('""') == ""
    for bad in ('"http://emrldtp.cc/abc.js"', "\"https://x.example/a.js';alert(1)//\"", '"<script src=x>"'):
        with pytest.raises(ConfigError, match="travelpayouts_drive_script"):
            load(bad)


def test_analytics_id_must_look_like_one(tmp_path):
    def load(analytics_id):
        path = tmp_path / "config.yaml"
        path.write_text(f"routes:\n  - {{ iata: BGY, city: Milan }}\nwebsite:\n  google_analytics_id: {analytics_id}\n",
                        encoding="utf-8")
        return load_config(path).website.google_analytics_id

    assert load("G-AB12CD34EF") == "G-AB12CD34EF"
    assert load('""') == ""
    with pytest.raises(ConfigError, match="google_analytics_id"):
        load("UA-12345-1")                                 # the old kind of ID
    with pytest.raises(ConfigError, match="google_analytics_id"):
        load("\"G-1');alert(1)//\"")                       # never reaches the page's script
