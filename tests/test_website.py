"""The static website: which routes it lists, in what order, and what the page contains."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.config import LinkTemplate, Route, load_config
from src.deals import PREMIUM_CHANNEL, price_band
from src.formatter import PLACEHOLDER
from src.links import LinkBuilder
from src.storage import Storage
from src.formatter import load_template
from src.website import (build_offers, build_site, city_label, country_code, load_site_template, main,
                         render_site, telegram_url)
from tests.conftest import NOW, make_quote, seed_history

NOV = date(2026, 11, 1)


@pytest.fixture
def config(tmp_path):
    base = load_config()
    # Most tests look at the page without a premium channel; the ones about
    # premium's head start switch it on themselves (with_premium below).
    return replace(base, db_path=tmp_path / "prices.db",
                   website=replace(base.website, output_dir=tmp_path / "site"),
                   premium=replace(base.premium, enabled=False))


def with_premium(config):
    return replace(config, premium=replace(config.premium, enabled=True, free_delay_hours=6))


def premium_posted(storage, destination, depart, price, hours_ago):
    storage.record_post(PREMIUM_CHANNEL, make_quote(price, depart, destination=destination),
                        price_band(price, 10), posted_at=NOW - timedelta(hours=hours_ago))


def latest_scan(storage, destination, prices: dict[date, float], back: dict[date, float] | None = None,
                fetched_at=NOW):
    """Pretend the newest run saw these prices: outbound by date, plus flights back."""
    quotes = [make_quote(p, d, destination=destination, fetched_at=fetched_at) for d, p in prices.items()]
    quotes += [make_quote(p, d, origin=destination, destination="TIA", fetched_at=fetched_at)
               for d, p in (back or {}).items()]
    storage.save_quotes(quotes)


def two_routes(storage):
    """BGY: usually €60, now €19 (a deal). VIE: usually €50, now €45 (not a deal). ATH: no prices."""
    seed_history(storage, [60] * 12, destination="BGY", days_ago=2)
    latest_scan(storage, "BGY", {NOV + timedelta(days=20): 19, NOV + timedelta(days=22): 20,
                                 NOV + timedelta(days=25): 30, NOV + timedelta(days=30): 61},
                back={NOV + timedelta(days=27): 24})
    seed_history(storage, [50] * 12, destination="VIE", days_ago=2)
    latest_scan(storage, "VIE", {NOV + timedelta(days=10): 45, NOV + timedelta(days=12): 48})


def test_offers_list_deals_first_and_bundle_cheap_dates(config, storage):
    two_routes(storage)
    offers = build_offers(config, storage, NOW)

    assert [o.route.iata for o in offers] == ["BGY", "VIE"]
    bgy, vie = offers
    assert bgy.is_deal and bgy.median == 60
    assert [q.price for q in bgy.quotes] == [19, 20]       # €30 is more than 10% above €19
    assert bgy.return_quote.price == 24
    assert not vie.is_deal and vie.best.price == 45 and vie.median == 50


# --- premium's head start: the page waits like the free channel does -----------

def test_premium_deals_are_held_back_until_the_free_channel_may_have_them(config, storage):
    config = with_premium(config)
    two_routes(storage)
    # Premium has not posted BGY yet: its three deal prices (€19, €20, €30) stay off the page.
    bgy = next(o for o in build_offers(config, storage, NOW) if o.route.iata == "BGY")
    assert not bgy.is_deal and bgy.best.price == 61

    # Posted on premium two hours ago: still premium's alone.
    premium_posted(storage, "BGY", NOV + timedelta(days=20), 19, hours_ago=2)
    bgy = next(o for o in build_offers(config, storage, NOW) if o.route.iata == "BGY")
    assert bgy.best.price == 61


def test_premium_deals_show_once_premium_had_them_long_enough(config, storage):
    config = with_premium(config)
    two_routes(storage)
    premium_posted(storage, "BGY", NOV + timedelta(days=20), 19, hours_ago=7)
    premium_posted(storage, "BGY", NOV + timedelta(days=22), 20, hours_ago=7)

    offers = build_offers(config, storage, NOW)
    assert [o.route.iata for o in offers] == ["BGY", "VIE"]   # the deal is back at the top
    bgy = offers[0]
    assert bgy.is_deal and [q.price for q in bgy.quotes] == [19, 20]


def test_ordinary_prices_never_wait_for_premium(config, storage):
    config = with_premium(config)
    two_routes(storage)
    vie = next(o for o in build_offers(config, storage, NOW) if o.route.iata == "VIE")
    assert vie.best.price == 45                               # €45 against a usual €50 is no deal


def test_route_is_left_off_while_all_its_prices_are_premium_only(config, storage):
    config = with_premium(config)
    seed_history(storage, [60] * 12, destination="BGY", days_ago=2)
    latest_scan(storage, "BGY", {NOV + timedelta(days=20): 19})
    assert build_offers(config, storage, NOW) == []
    assert [o.best.price for o in build_offers(config, storage, NOW, premium_delay=False)] == [19]


def test_past_dates_and_stale_routes_are_left_off(config, storage):
    latest_scan(storage, "BGY", {date(2026, 10, 1): 10, NOV + timedelta(days=20): 50})
    latest_scan(storage, "VIE", {NOV + timedelta(days=5): 12}, fetched_at=NOW - timedelta(days=3))
    offers = build_offers(config, storage, NOW)

    assert [o.route.iata for o in offers] == ["BGY"]      # VIE's prices are too old
    assert offers[0].best.depart_date == NOV + timedelta(days=20)


def test_latest_quotes_returns_only_the_newest_scan(storage):
    latest_scan(storage, "BGY", {NOV: 50}, fetched_at=NOW - timedelta(hours=3))
    latest_scan(storage, "BGY", {NOV: 40, NOV + timedelta(days=1): 45})
    assert [q.price for q in storage.latest_quotes("TIA", "BGY")] == [40, 45]
    assert storage.latest_quotes("TIA", "ATH") == []
    assert storage.last_fetched_at() == NOW


def test_page_contains_rows_links_and_no_leftover_placeholders(config, storage):
    two_routes(storage)
    offers = build_offers(config, storage, NOW)
    links = LinkBuilder(replace(config.links, sub_id="website"), "12345")
    page = render_site(offers, config, links, updated_at=NOW, now=NOW)

    # The data attributes feed the page's JavaScript (search card, sort, filter).
    assert '<li class="row deal c-it" data-iata="BGY" data-date="2026-11-21" data-price="19" data-saving="68">' in page
    assert '<li class="row c-at" data-iata="VIE" data-date="2026-11-11" data-price="45" data-saving="10">' in page
    assert page.index('data-iata="BGY"') < page.index('data-iata="VIE"')
    assert "€19" in page and "zakonisht €60" in page and "-68%" in page
    assert "që nga <b>€19</b>" in page                     # MIN_PRICE: the cheapest price on the page
    # The best deal's chip on the first slide: city, price and saving.
    assert '<b>Milan (Bergamo) nga €19</b></span><span class="td-save">-68%</span>' in page
    # The search card's strip: search, deals, how it works (Premium lives in the nav only).
    assert 'class="stab" href="#si-funksionon"' in page
    assert 'class="stab" href="https://t.me' not in page
    assert 'Ofertat <span class="n">1</span>' in page      # DEAL_COUNT: one deal, on the Ofertat tabs
    assert '<span class="count">2</span>' in page          # ROUTE_COUNT: two routes
    # The deal is a banner in the hero carousel, the other route is not.
    assert '<li class="slide slide-deal c-it" data-iata="BGY">' in page
    assert 'slide-deal c-at' not in page
    # The search card: one <option> per route, deals grouped first, and the date window.
    assert '<optgroup label="Ofertat e momentit"><option value="BGY" data-date="2026-11-21" data-price="19">Milan (Bergamo), nga €19</option></optgroup>' in page
    assert '<optgroup label="Destinacionet e tjera"><option value="VIE" data-date="2026-11-11" data-price="45">Vienna, nga €45</option></optgroup>' in page
    assert 'value="2026-10-10" min="2026-10-10" max="2026-12-08"' in page   # tomorrow .. +60 days (lookahead_days)
    assert "<!--" not in page                              # template notes never reach the page
    assert "Kthimi nga €24" in page
    assert "marker=12345.website" in page                  # the site's own SubID
    assert 'href="https://t.me/flyfromtirana"' in page
    assert "Përditësuar më 9 Tet 2026, 12:00" in page      # 10:00 UTC = 12:00 in Tirana
    assert not PLACEHOLDER.search(page)                   # every {NAME} was filled in or dropped


def test_values_are_html_escaped(config, storage):
    odd = Route(iata="XYZ", city="A<b> & C", city_en="C", flag="")
    config = replace(config, routes=[odd])
    latest_scan(storage, "XYZ", {NOV: 30})
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    assert "A&lt;b&gt; &amp; C" in page and "A<b>" not in page


def test_build_site_writes_files_and_handles_an_empty_database(config):
    index = build_site(config, "12345", now=NOW)
    assert index == config.website.output_dir / "index.html"
    assert (config.website.output_dir / ".nojekyll").exists()
    page = index.read_text(encoding="utf-8")
    assert "Ende nuk ka çmime" in page
    assert '<li class="row' not in page and "Përditësuar" not in page
    assert 'class="top-deal"' not in page                  # no deals, no chip on the first slide


def test_cli_builds_from_a_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAVELPAYOUTS_MARKER", raising=False)
    (tmp_path / "config.yaml").write_text(
        "routes:\n  - { iata: BGY, city: Milano }\nwebsite:\n  output_dir: site\n", encoding="utf-8")
    assert main(["--config", str(tmp_path / "config.yaml")]) == 0
    assert (tmp_path / "site" / "index.html").exists()


def test_hero_shows_at_most_three_deals(config, storage):
    for i, iata in enumerate(["BGY", "VIE", "ATH", "MUC", "FCO"]):
        seed_history(storage, [80] * 12, destination=iata, days_ago=2)
        latest_scan(storage, iata, {NOV + timedelta(days=i): 20 + i})   # every route is a deal (-70%+)
    offers = build_offers(config, storage, NOW)
    assert all(o.is_deal for o in offers) and len(offers) == 5
    page = render_site(offers, config, LinkBuilder(config.links, "1"), updated_at=NOW, now=NOW)
    assert page.count('class="slide slide-deal') == 3                   # the three best, biggest saving first
    assert page.count('<li class="row deal') == 5                       # 5 rows in the list...
    assert page.count('<li class="dcard deal') == 5                     # ...and 5 photo cards in the carousel


def test_site_templates_lose_their_comments():
    assert "<!--" in load_template("site_row.html")        # the note for editors is in the file...
    for name in ("site.html", "site_row.html", "site_hero.html", "site_card.html"):
        assert "<!--" not in load_site_template(name)      # ...but never in what gets rendered


def test_city_label():
    assert city_label(Route(iata="BGY", city="Milano", city_en="Milan", flag="", airport="Bergamo")) == "Milano (Bergamo)"
    assert city_label(Route(iata="VIE", city="Vjenë", city_en="Vienna", flag="")) == "Vjenë"


def test_country_code():
    assert country_code("🇮🇹") == "it"
    assert country_code("🇬🇧") == "gb"
    assert country_code("") is None                        # no flag configured
    assert country_code("✈️") is None                      # not a flag


def test_telegram_url():
    assert telegram_url("@flyfromtirana") == "https://t.me/flyfromtirana"
    assert telegram_url("-1001234567890") is None          # private channel id: no public link
    assert telegram_url("") is None


def test_photo_cards_show_every_route_deals_first(config, storage):
    two_routes(storage)
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    cards = page[page.index('class="strip stories"'):page.index('data-ctl="stories"')]
    assert cards.index('data-iata="BGY"') < cards.index('data-iata="VIE"')      # the deal comes first
    assert '<li class="dcard deal c-it" data-iata="BGY">' in cards
    assert '<img src="./img/dest/bgy.webp"' in cards                           # the route's photo
    assert "Milan, Italy" in cards and "Vienna, Austria" in cards              # city and country, in English
    assert "Milano" not in page and "Vjenë" not in page                        # no Albanian city names
    assert cards.count('class="sticker deal"') == 1                            # the sticker is for deals only
    assert '<span class="dair">Bergamo</span>' in cards                        # the airport, for multi-airport cities


def test_route_without_a_photo_keeps_its_colours(config, storage):
    odd = Route(iata="XYZ", city="Diku", city_en="Somewhere", flag="🇮🇹")
    config = replace(config, routes=[odd])
    latest_scan(storage, "XYZ", {NOV: 30})
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    card = page[page.index('<li class="dcard'):page.index("</li>", page.index('<li class="dcard'))]
    assert 'class="dcard c-it"' in card and "<img" not in card


def test_build_copies_the_photos_and_shows_no_credits(config, storage):
    two_routes(storage)
    index = build_site(config, "12345", now=NOW)
    out = config.website.output_dir
    assert (out / "img" / "dest" / "bgy.webp").exists()
    assert (out / "img" / "services" / "esim.webp").exists()
    assert not (out / "img" / "credits.json").exists()                         # the list of sources stays behind
    page = index.read_text(encoding="utf-8")
    # Every photo on the page is Unsplash or CC0, which need no credit: the footer has no credits block.
    assert '<details class="credits">' not in page and "Fotot dhe licencat" not in page
    for img in ("img/services/esim.webp", "img/services/insurance.webp"):
        assert img in page                                                     # the service cards


def test_photo_that_requires_a_credit_gets_one_while_it_is_on_the_page(config, storage):
    # The hotel card's photo is CC BY. With no hotel link the card is not on the page (see the
    # test below); once a link is set, the card appears and its author must be named.
    two_routes(storage)
    partners = dict(config.links.partners, hotel=LinkTemplate(url="https://hotels.example/"))
    config = replace(config, links=replace(config.links, partners=partners))
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    assert "img/services/hotel.webp" in page
    start = page.index('<details class="credits">')
    credits = page[start:page.index("</details>", start)]
    assert credits.count("<li>") == 1
    assert "Hotele: " in credits and "CC BY 2.0" in credits and "Prerë dhe zvogëluar." in credits


def test_site_uses_its_own_partner_links(config, storage):
    # config.yaml: the site's eSIM link is not the one the Telegram posts use.
    assert config.website.links.partners["esim"].url != config.links.partners["esim"].url
    assert config.website.links.sub_id == "website" and config.links.sub_id == "telegram"
    two_routes(storage)
    # The link builder build_site() uses: the site's own links.
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.website.links, "12345"),
                       updated_at=NOW, now=NOW)
    assert config.website.links.partners["esim"].url in page
    assert config.links.partners["esim"].url not in page
    # Flight links go through the site's own Aviasales short link, with the search inside it.
    redirect = config.website.links.flight_wrapper.split("{url}")[0]
    assert redirect.startswith("https://") and config.links.flight_wrapper == ""
    assert f'class="book" href="{redirect}https%3A%2F%2Fwww.aviasales.com%2Fsearch%2FTIA2111BGY1%3F' in page


def test_website_links_fall_back_to_the_posts_links(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "routes:\n  - { iata: BGY, city: Milan }\n"
        "links:\n  sub_id: telegram\n  partners:\n"
        "    esim: { url: 'https://posts.example/esim' }\n"
        "    insurance: { url: 'https://posts.example/insurance' }\n"
        "website:\n  links:\n    flight: { wrapper: 'https://tp.example/r?u={url}' }\n"
        "    partners:\n      esim: { url: 'https://site.example/esim' }\n", encoding="utf-8")
    config = load_config(tmp_path / "config.yaml")
    site = config.website.links
    assert site.partners["esim"].url == "https://site.example/esim"            # the site's own
    assert site.partners["insurance"].url == "https://posts.example/insurance"  # not set for the site: the posts' link
    assert site.flight_wrapper == "https://tp.example/r?u={url}" and config.links.flight_wrapper == ""
    assert site.sub_id == "website"
    assert config.links.partners["esim"].url == "https://posts.example/esim"    # the posts are untouched


def test_service_card_without_a_link_is_left_out(config, storage):
    two_routes(storage)
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    assert "img/services/hotel.webp" not in page       # config.yaml has no hotel link yet
    assert "img/services/esim.webp" in page
