"""The static website: which routes it lists, in what order, and what the page contains."""
from dataclasses import replace
import re
from datetime import date, time, timedelta
from urllib.parse import quote

import pytest

from src.config import ConfigError, LinkTemplate, Route, load_config
from src.deals import PREMIUM_CHANNEL, price_band
from src.formatter import PLACEHOLDER
from src.links import LinkBuilder
from src.storage import Storage
from src.formatter import load_template
from src.growth import join_link
from src.photos import ASSETS_DIR
from src.website import (build_offers, build_site, city_label, country_code, load_site_template, main,
                         render_privacy, render_site)
from tests.conftest import NOW, make_quote, seed_history, with_tours

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


def test_a_cheaper_price_waits_again_although_premium_had_the_date(config, storage):
    config = with_premium(config)
    two_routes(storage)
    # Premium was sent 21 Nov at €25 seven hours ago. Today's €19 is a cheaper price band: premium
    # has not had that price yet, so the page keeps it back, like the free channel does.
    premium_posted(storage, "BGY", NOV + timedelta(days=20), 25, hours_ago=7)
    bgy = next(o for o in build_offers(config, storage, NOW) if o.route.iata == "BGY")
    assert bgy.best.price == 61
    # Once premium has had €19 itself for long enough, it shows.
    premium_posted(storage, "BGY", NOV + timedelta(days=20), 19, hours_ago=7)
    bgy = next(o for o in build_offers(config, storage, NOW) if o.route.iata == "BGY")
    assert bgy.is_deal and bgy.best.price == 19


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
    assert '<li class="row deal" data-iata="BGY" data-date="2026-11-21" data-price="19" data-saving="68">' in page
    assert '<li class="row" data-iata="VIE" data-date="2026-11-11" data-price="45" data-saving="10">' in page
    assert page.index('data-iata="BGY"') < page.index('data-iata="VIE"')
    assert "€19" in page and "zakonisht €60" in page and "-68%" in page
    assert "që nga <b>€19</b>" in page                     # MIN_PRICE: the cheapest price on the page
    # The board at the top: one line per route, the deal first, with its saving and its price.
    assert '<li class="brow deal" data-iata="BGY">' in page and '<li class="brow" data-iata="VIE">' in page
    assert '<span class="bsave">-68%</span>' in page and '<small>zakonisht €60</small><b>€19</b>' in page
    assert 'Ofertat <span class="n">1</span>' in page      # DEAL_COUNT: one deal, on the Ofertat tabs
    assert '<span class="count">2</span>' in page          # ROUTE_COUNT: two routes
    # The search card: one <option> per route, deals grouped first, and the date window.
    assert '<optgroup label="Ofertat e momentit"><option value="BGY" data-date="2026-11-21" data-price="19">Milan (Bergamo), nga €19</option></optgroup>' in page
    assert '<optgroup label="Destinacionet e tjera"><option value="VIE" data-date="2026-11-11" data-price="45">Vienna, nga €45</option></optgroup>' in page
    assert 'value="2026-10-10" min="2026-10-10" max="2026-12-08"' in page   # tomorrow .. +60 days (lookahead_days)
    assert "<!--" not in page                              # template notes never reach the page
    assert "Kthimi nga €24" in page
    assert "marker=12345.website" in page                  # the site's own SubID
    assert f'href="{join_link(config, "website")}"' in page   # the website's own invite link
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
    assert 'class="board"' not in page                     # no prices, no board at the top


def test_main_page_names_the_picture_for_link_previews(config):
    index = build_site(config, "12345", now=NOW)
    # Telegram, WhatsApp and the like need the picture's full address, and the file has to be there.
    assert '<meta property="og:image" content="https://flyfromtirana.al/img/share/sq.png">' in index.read_text(encoding="utf-8")
    assert (config.website.output_dir / "img" / "share" / "sq.png").exists()

    # Without the site's address there is no full address to give: the tag is left out.
    config = replace(config, website=replace(config.website, url=""))
    assert "og:image" not in build_site(config, "12345", now=NOW).read_text(encoding="utf-8")


def test_build_writes_the_page_for_an_address_that_does_not_exist(config):
    build_site(config, "12345", now=NOW)
    out = config.website.output_dir
    page = (out / "404.html").read_text(encoding="utf-8")
    assert "<h1>Faqja nuk u gjet</h1>" in page
    # The server shows this page in any folder, so its links start from the top of the site.
    assert '<a class="btn" href="/">Kthehu te faqja kryesore</a>' in page
    assert 'href="./' not in page and 'href="../' not in page
    assert f'href="{join_link(config, "website")}"' in page   # the website's own invite link
    assert '<meta name="robots" content="noindex">' in page                   # search engines leave it out...
    assert "404" not in (out / "sitemap.xml").read_text(encoding="utf-8")     # ...and the sitemap doesn't list it
    assert "<!--" not in page and not PLACEHOLDER.search(page)


def test_privacy_page_describes_what_the_site_uses(config):
    website = replace(config.website, google_analytics_id="G-TEST", google_analytics_ask_first=False,
                      operator="Devbay.cloud", contact_email="hello@devbay.cloud")
    config = replace(config, website=website)
    build_site(config, "12345", now=NOW)
    out = config.website.output_dir
    page = (out / "privacy" / "index.html").read_text(encoding="utf-8")
    assert "<h1>Privatësia dhe cookies</h1>" in page
    assert "Faqen e drejton Devbay.cloud" in page and "href='mailto:hello@devbay.cloud'" in page
    assert "Numërimi fillon kur hapet faqja" in page          # Analytics counts right away...
    assert "vetëm pasi shtyp «Pranoj»" not in page              # ...so not the wording for asking first
    assert "<code>ga-consent</code>" in page
    assert "GetYourGuide</h2>" in page                          # the tours link has a Partner ID
    assert '<link rel="canonical" href="https://flyfromtirana.al/privacy/">' in page
    assert "<!--" not in page and not PLACEHOLDER.search(page)
    # Linked from the footers and from the cookie box, and listed for search engines.
    main_page = (out / "index.html").read_text(encoding="utf-8")
    assert '<a href="./privacy/">Privatësia</a>' in main_page
    assert '<a href="./privacy/">Si i përdorim të dhënat</a>' in main_page
    assert '<a href="../privacy/">Privatësia</a>' in (out / "milan" / "index.html").read_text(encoding="utf-8")
    assert "<loc>https://flyfromtirana.al/privacy/</loc>" in (out / "sitemap.xml").read_text(encoding="utf-8")


def test_privacy_page_leaves_out_what_the_site_does_not_use(config):
    links = LinkBuilder(config.website.links, "12345")
    asks_first = replace(config, website=replace(config.website, google_analytics_id="G-TEST",
                                                 google_analytics_ask_first=True))
    assert "vetëm pasi shtyp «Pranoj»" in render_privacy(asks_first, links, now=NOW)

    # No Analytics, no contact address, no GetYourGuide: their parts are gone, the rest stays.
    partners = {name: link for name, link in config.website.links.partners.items() if name != "tours"}
    bare_links = replace(config.website.links, partners=partners)
    bare = replace(config, website=replace(config.website, google_analytics_id="", contact_email="", links=bare_links))
    page = render_privacy(bare, LinkBuilder(bare_links, "12345"), now=NOW)
    assert "Google Analytics</h2>" not in page and "ga-consent" not in page and "Cilësimet e cookies" not in page
    assert "Kush jemi" not in page and "GetYourGuide" not in page
    assert "Google Fonts" in page and "Cloudflare" in page and "Të drejtat e tua" in page
    assert not PLACEHOLDER.search(page)


def test_cli_builds_from_a_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAVELPAYOUTS_MARKER", raising=False)
    (tmp_path / "config.yaml").write_text(
        "routes:\n  - { iata: BGY, city: Milano }\nwebsite:\n  output_dir: site\n", encoding="utf-8")
    assert main(["--config", str(tmp_path / "config.yaml")]) == 0
    assert (tmp_path / "site" / "index.html").exists()


def test_hero_board_stops_at_its_number_of_lines(config, storage, monkeypatch):
    monkeypatch.setattr("src.website.HERO_ROWS", 3)
    for i, iata in enumerate(["BGY", "VIE", "ATH", "MUC", "FCO"]):
        seed_history(storage, [80] * 12, destination=iata, days_ago=2)
        latest_scan(storage, iata, {NOV + timedelta(days=i): 20 + i})   # every route is a deal (-70%+)
    offers = build_offers(config, storage, NOW)
    assert all(o.is_deal for o in offers) and len(offers) == 5
    page = render_site(offers, config, LinkBuilder(config.links, "1"), updated_at=NOW, now=NOW)
    assert page.count('<li class="brow deal') == 3                      # the three best, biggest saving first
    assert page.count('<li class="row deal') == 5                       # 5 lines in the table...
    assert page.count('<li class="dcard deal') == 5                     # ...and 5 photo cards


def test_premium_block_shows_only_while_there_is_a_premium_channel(config, storage):
    two_routes(storage)
    links = LinkBuilder(config.links, "1")
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert 'id="premium"' not in page and 'href="#premium"' not in page     # no channel: no block, no links to it

    # With a channel: what config.yaml says about it, and nothing more.
    premium = replace(with_premium(config).premium, join_link="https://t.me/+abc", scan_every_minutes=15)
    config = replace(config, premium=premium, quiet_hours=(time(23, 0), time(8, 0)),
                     full_run_every_minutes=180)   # premium comes back together with the 3-hour runs
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert '<section class="premium wrap" id="premium"' in page
    assert '<a class="btn-premium" href="https://t.me/+abc">' in page
    assert "i marrin 6 orë më vonë" in page and "<strong>+6 orë</strong>" in page
    assert "kontrollohen çdo 15 minuta" in page and "nga ora 23:00 deri në 08:00" in page
    assert page.count('href="#premium"') == 3                               # the top bar, the Telegram band, the footer

    # No quick premium scans and no quiet hours: the sentences about them are left out.
    config = replace(config, premium=replace(premium, scan_every_minutes=0), quiet_hours=None)
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert 'id="premium"' in page and "Kontroll çdo" not in page and "postohet menjëherë" not in page
    assert "Natën" not in page
    assert not PLACEHOLDER.search(page)

    # No wait at all: the free channel and the page still get it at the next 3-hour run, never "0 hours".
    config = replace(config, premium=replace(premium, free_delay_hours=0))
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert "i marrin 3 orë më vonë" in page and " 0 orë" not in page


def test_pages_say_how_often_prices_are_checked(config, storage):
    two_routes(storage)
    links = LinkBuilder(config.links, "1")
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert "i kontrolluar çdo 15 minuta." in page and "Kontrollojmë çmimet çdo 15 minuta" in page
    assert "çdo 3 orë" not in page

    config = replace(config, full_run_every_minutes=180)
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert "i kontrolluar çdo 3 orë." in page and "minuta" not in page


def test_pages_say_the_prices_may_have_changed(config, storage):
    two_routes(storage)
    links = LinkBuilder(config.links, "1")
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert ('<p class="price-note">Çmimet janë gjetur nga udhëtarë të tjerë në Aviasales në 2 ditët e fundit '
            'dhe mund të kenë ndryshuar.') in page

    # With no age limit the note leaves out the part about the days.
    config = replace(config, max_price_age_days=None)
    page = render_site(build_offers(config, storage, NOW), config, links, updated_at=NOW, now=NOW)
    assert "të tjerë në Aviasales dhe mund të kenë ndryshuar." in page and "ditët e fundit" not in page


def test_site_templates_lose_their_comments():
    assert "<!--" in load_template("site_row.html")        # the note for editors is in the file...
    for name in ("site.html", "site_row.html", "site_hero.html", "site_card.html", "site_premium.html"):
        assert "<!--" not in load_site_template(name)      # ...but never in what gets rendered


def test_city_label():
    assert city_label(Route(iata="BGY", city="Milano", city_en="Milan", flag="", airport="Bergamo")) == "Milano (Bergamo)"
    assert city_label(Route(iata="VIE", city="Vjenë", city_en="Vienna", flag="")) == "Vjenë"


def test_country_code():
    assert country_code("🇮🇹") == "it"
    assert country_code("🇬🇧") == "gb"
    assert country_code("") is None                        # no flag configured
    assert country_code("✈️") is None                      # not a flag


def test_photo_cards_show_every_route_deals_first(config, storage):
    two_routes(storage)
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    cards = page[page.index('class="strip stories"'):page.index("</ol>", page.index('class="strip stories"'))]
    assert cards.index('data-iata="BGY"') < cards.index('data-iata="VIE"')      # the deal comes first
    assert '<li class="dcard deal" data-iata="BGY">' in cards
    assert '<img src="./img/dest/bgy.webp"' in cards                           # the route's photo
    assert "Milan, Italy" in cards and "Vienna, Austria" in cards              # city and country, in English
    assert "Milano" not in page and "Vjenë" not in page                        # no Albanian city names
    assert cards.count('class="sticker deal"') == 1                            # the sticker is for deals only
    assert '<span class="dair">Bergamo</span>' in cards                        # the airport, for multi-airport cities


def test_route_without_a_photo_gets_a_plain_card(config, storage):
    odd = Route(iata="XYZ", city="Diku", city_en="Somewhere", flag="🇮🇹")
    config = replace(config, routes=[odd])
    latest_scan(storage, "XYZ", {NOV: 30})
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    card = page[page.index('<li class="dcard'):page.index("</li>", page.index('<li class="dcard'))]
    assert 'class="dcard"' in card and "<img" not in card


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
    for img in ("img/services/esim.webp", "img/services/car_rental.webp"):
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


def test_tickets_transfer_and_luggage_cards_open_the_partners_general_pages(config, storage):
    two_routes(storage)
    site = config.website.links
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(site, "12345"), updated_at=NOW, now=NOW)
    for name, title in (("tickets", "Bileta për muze dhe atraksione"), ("transfer", "Transfertë nga aeroporti"),
                        ("luggage", "Ruajtje bagazhesh")):
        assert (ASSETS_DIR / "img" / "services" / f"{name}.webp").exists()
        assert f'<img src="./img/services/{name}.webp"' in page and f"<b>{title}</b>" in page
        # The site's own short link (not the posts'), opening the partner's homepage: the main
        # page is about no one city, whichever route happens to be the cheapest.
        general = site.partners[name].wrapper.format(url=quote(site.partners[name].url, safe=""))
        assert page.count(f'href="{general}"') == 2                     # the card and the footer
        assert site.partners[name].wrapper != config.links.partners[name].wrapper
    assert "attractions-c" not in page and "luggage-storage" not in page


def test_tours_card_opens_getyourguide_with_the_partner_id(config, storage):
    two_routes(storage)
    config = with_tours(config)
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.website.links, "12345"),
                       updated_at=NOW, now=NOW)
    assert (ASSETS_DIR / "img" / "services" / "tours.webp").exists()
    assert '<img src="./img/services/tours.webp"' in page and "<b>Ture dhe aktivitete</b>" in page
    # No redirect link: GetYourGuide's own address with the Partner ID on its end, counted as "website".
    assert page.count('href="https://www.getyourguide.com/?partner_id=ABC123&amp;cmp=website"') == 2   # card and footer
    # GetYourGuide's script is on this page too (it counts the visits), but the widget is for the city pages.
    script = '<script async defer src="https://widget.getyourguide.com/dist/pa.umd.production.min.js" data-gyg-partner-id="ABC123"></script>'
    assert page.count(script) == 1 and page.index(script) < page.index("</head>")
    assert "data-gyg-widget" not in page
    # Without the tours link, nothing is loaded from GetYourGuide.
    site = config.website.links
    off = replace(config, website=replace(config.website, links=replace(site, partners=dict(site.partners, tours=LinkTemplate()))))
    page = render_site(build_offers(off, storage, NOW), off, LinkBuilder(off.website.links, "12345"), updated_at=NOW, now=NOW)
    assert "getyourguide" not in page


def test_every_city_has_its_own_partner_pages():
    # config.yaml: a route whose city is missing here would send its links to the partner's homepage.
    config = load_config()
    cities = {route.city for route in config.routes}
    assert config.website.links.city_pages == config.links.city_pages      # one list, for the posts and the site
    for name in ("tickets", "transfer", "luggage", "tours"):
        assert set(config.links.city_pages[name]) == cities
    # GetYourGuide's widget reads the city's number from the end of its page's address (rome-l33/).
    assert all(re.search(r"-l\d+/$", url) for url in config.links.city_pages["tours"].values())


def test_city_pages_must_name_a_route_and_a_partner(tmp_path):
    def load(city_pages: str):
        path = tmp_path / "config.yaml"
        path.write_text("routes:\n  - { iata: BGY, city: Milan }\nlinks:\n  partners:\n"
                        "    tickets: { url: 'https://t.example/' }\n  city_pages:\n" + city_pages, encoding="utf-8")
        return load_config(path).links.city_pages

    assert load("    tickets: { Milan: 'https://t.example/milan/' }\n") == {"tickets": {"Milan": "https://t.example/milan/"}}
    with pytest.raises(ConfigError, match="'Milano', which is no route's city"):
        load("    tickets: { Milano: 'https://t.example/milan/' }\n")
    with pytest.raises(ConfigError, match="'ticket', which is not under links.partners"):
        load("    ticket: { Milan: 'https://t.example/milan/' }\n")


def test_service_card_without_a_link_is_left_out(config, storage):
    two_routes(storage)
    page = render_site(build_offers(config, storage, NOW), config, LinkBuilder(config.links, "1"),
                       updated_at=NOW, now=NOW)
    assert "img/services/hotel.webp" not in page       # config.yaml has no hotel link yet
    assert "img/services/esim.webp" in page
