from datetime import date

import pytest

from src.config import DEFAULT_PARTNERS, ConfigError, LinkSettings, LinkTemplate, Route
from src.deals import Deal
from src.formatter import (WORDS, format_dates, format_duration, format_post, format_price, render,
                           saving_percent, tidy)
from src.links import LinkBuilder
from src import photos
from src.photos import PostPhoto, post_photo
from tests.conftest import make_quote

AIRLINES = {"W6": "Wizz Air"}


def make_links(**partner_urls) -> LinkBuilder:
    partners = {name: LinkTemplate() for name in DEFAULT_PARTNERS}
    partners.update({name: LinkTemplate(url=url) for name, url in partner_urls.items()})
    return LinkBuilder(LinkSettings(sub_id="telegram", partners=partners), marker="12345")


def post(deal, links=None, **kwargs):
    return format_post(deal, links or make_links(), language="sq",
                       channel_handle="@flyfromtirana", airline_names=AIRLINES, **kwargs)


def test_full_post_matches_template(route):
    deal = Deal(
        route=route,
        quotes=[make_quote(19.4, date(2026, 10, 20)), make_quote(21, date(2026, 10, 15))],
        median=62,
        reason="median",
        return_quote=make_quote(24, date(2026, 10, 27), origin="BGY", destination="TIA"),
    )
    links = make_links(
        hotel="https://hotels.example/?city={city_en}&in={checkin}&out={checkout}",
        esim="https://esim.example/{iata}",
        insurance="https://insurance.example/?m={marker}",
    )
    assert post(deal, links, website_url="https://site.example/") == "\n".join([
        "✈️ <b>TIRANA → MILAN</b> (Bergamo) 🇮🇹",
        "",
        "💰 <b>nga €20</b> one way",
        "🔥 <b>-69%</b> · zakonisht ~€62",
        "<blockquote>📅 <b>15 Tet, 20 Tet</b>",
        "🛫 Wizz Air · direkt · 1 orë 35 min",
        "🔁 Kthimi nga €24</blockquote>",
        '👉 <b><a href="https://www.aviasales.com/search/TIA2010BGY1'
        '?currency=eur&amp;marker=12345.telegram">Rezervo tani</a></b>',
        "⏳ <i>Çmimet ndryshojnë shpejt!</i>",
        "",
        '🏨 <a href="https://hotels.example/?city=Milan&amp;in=2026-10-20&amp;out=2026-10-27">Hotele në Milan</a>',
        '📱 <a href="https://esim.example/BGY">eSIM</a>   🛡️ <a href="https://insurance.example/?m=12345">Sigurim</a>',
        "",
        '🌐 <a href="https://site.example/">Të gjitha ofertat në faqen tonë</a>',
        "🔔 Ndiq @flyfromtirana për oferta çdo ditë",
    ])


def test_optional_lines_are_left_out():
    plain = Route(iata="VIE", city="Vienna", city_en="Vienna", flag="🇦🇹")
    deal = Deal(route=plain, quotes=[make_quote(19, destination="VIE", transfers=1, duration_min=None)],
                median=None, reason="threshold")
    assert post(deal) == "\n".join([
        "✈️ <b>TIRANA → VIENNA</b> 🇦🇹",     # no (airport)
        "",
        "💰 <b>nga €19</b> one way",          # no median yet, so no "zakonisht" line either
        "<blockquote>📅 <b>20 Tet</b>",
        "🛫 Wizz Air · me ndalesë</blockquote>",   # no duration, no return line
        '👉 <b><a href="https://www.aviasales.com/search/TIA2010VIE1'
        '?currency=eur&amp;marker=12345.telegram">Rezervo tani</a></b>',
        "⏳ <i>Çmimet ndryshojnë shpejt!</i>",
        "",                                    # no hotel/eSIM/insurance lines, and only one blank line for them
        "🔔 Ndiq @flyfromtirana për oferta çdo ditë",   # no website line without website_url
    ])


def test_quote_survives_without_airline_and_return(route):
    # Whatever is missing inside the quote, its tags must still open and close.
    deal = Deal(route=route, quotes=[make_quote(19, airline="")], median=None, reason="threshold")
    assert "<blockquote>📅 <b>20 Tet</b></blockquote>\n👉" in post(deal)


def test_photo_credit_is_the_last_line(route, tmp_path):
    deal = Deal(route=route, quotes=[make_quote(19)], median=None, reason="threshold")
    photo = PostPhoto(path=tmp_path / "bgy.jpg", credit="D-Stanley, CC BY 2.0",
                      source_url="https://photos.example/1?a=1&b=2")
    assert post(deal, photo=photo).endswith(
        "🔔 Ndiq @flyfromtirana për oferta çdo ditë\n"
        '📷 <i>Foto: <a href="https://photos.example/1?a=1&amp;b=2">D-Stanley, CC BY 2.0</a></i>')
    # A photo whose licence asks for no credit, and a post without a photo: no such line.
    no_credit = PostPhoto(path=tmp_path / "bgy.jpg", credit=None, source_url=None)
    assert "Foto:" not in post(deal, photo=no_credit)
    assert "Foto:" not in post(deal)


def test_post_photo_needs_no_credit_today():
    vienna = post_photo("VIE")
    assert vienna.path.name == "vie.jpg" and vienna.path.exists()
    assert vienna.credit is None                           # Unsplash License: no credit needed
    assert post_photo("bgy").credit is None                # CC0: none either
    assert post_photo("XXX") is None                       # no assets/telegram/xxx.jpg


def test_cc_by_photo_names_its_author(tmp_path, monkeypatch):
    # Should a CC BY photo come back one day, its post must credit the author again.
    (tmp_path / "vie.jpg").write_bytes(b"jpeg")
    credits = tmp_path / "credits.json"
    credits.write_text('[{"slot": "VIE", "creator": "Jane Doe", "license": "CC BY 2.0",'
                       ' "source_url": "https://photos.example/1"}]', encoding="utf-8")
    monkeypatch.setattr(photos, "POST_PHOTOS_DIR", tmp_path)
    monkeypatch.setattr(photos, "CREDITS_FILE", credits)
    vienna = post_photo("VIE")
    assert vienna.credit == "Jane Doe, CC BY 2.0"
    assert vienna.source_url == "https://photos.example/1"


def test_post_photos_are_small_jpegs():
    photos = sorted(post_photo("VIE").path.parent.glob("*"))
    assert len(photos) >= 24
    for path in photos:
        assert path.suffix == ".jpg", path.name
        assert path.read_bytes()[:2] == b"\xff\xd8", f"{path.name} is not a JPEG"
        assert path.stat().st_size < 1_000_000, f"{path.name} is larger than a post photo needs to be"


def test_tidy():
    assert tidy("A\n\n\n\nB") == "A\n\nB"
    assert tidy("<blockquote>A\nB\n</blockquote>\nC") == "<blockquote>A\nB</blockquote>\nC"


def test_only_one_of_esim_and_insurance(route):
    deal = Deal(route=route, quotes=[make_quote(19)], median=None, reason="threshold")
    text = post(deal, make_links(insurance="https://insurance.example/"))
    assert '\n🛡️ <a href="https://insurance.example/">Sigurim</a>\n' in text
    assert "eSIM" not in text


def test_values_are_html_escaped():
    odd = Route(iata="XXX", city="A&B <x>", city_en="AB", flag="")
    deal = Deal(route=odd, quotes=[make_quote(19, destination="XXX")], median=None, reason="threshold")
    text = post(deal)
    assert "A&amp;B &lt;X&gt;" in text
    assert "<x>" not in text


def test_unknown_placeholder_is_an_error():
    with pytest.raises(ConfigError, match="NOPE"):
        render("Hello {NOPE}", {})


def test_blank_template_lines_are_kept():
    assert render("A {X}\n\nB", {"X": "1"}) == "A 1\n\nB"


def test_saving_percent(route):
    def saving(price, median):
        return saving_percent(Deal(route=route, quotes=[make_quote(price)], median=median, reason="median"))

    assert saving(20, 62) == "68"
    assert saving(20, None) is None    # no usual price yet
    assert saving(62, 62) is None      # no saving to show


def test_helpers():
    assert format_price(19.0) == "19"
    assert format_price(19.01) == "20"  # rounded up, never under-advertised
    assert format_dates([date(2026, 11, 2), date(2026, 10, 30)], WORDS["sq"]["months"]) == "30 Tet, 2 Nën"
    assert format_duration(135, WORDS["sq"]) == "2 orë 15 min"
    assert format_duration(120, WORDS["sq"]) == "2 orë"
    assert format_duration(55, WORDS["sq"]) == "55 min"
    assert format_duration(None, WORDS["sq"]) is None
