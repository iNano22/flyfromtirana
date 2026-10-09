from datetime import date

import pytest

from src.config import DEFAULT_PARTNERS, ConfigError, LinkSettings, LinkTemplate, Route
from src.deals import Deal
from src.formatter import WORDS, format_dates, format_duration, format_post, format_price, render
from src.links import LinkBuilder
from tests.conftest import make_quote

AIRLINES = {"W6": "Wizz Air"}


def make_links(**partner_urls) -> LinkBuilder:
    partners = {name: LinkTemplate() for name in DEFAULT_PARTNERS}
    partners.update({name: LinkTemplate(url=url) for name, url in partner_urls.items()})
    return LinkBuilder(LinkSettings(sub_id="telegram", partners=partners), marker="12345")


def post(deal, links=None):
    return format_post(deal, links or make_links(), language="sq",
                       channel_handle="@flyfromtirana", airline_names=AIRLINES)


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
    assert post(deal, links) == "\n".join([
        "✈️ TIRANA → MILAN (Bergamo) 🇮🇹",
        "💰 nga €20 one way (zakonisht ~€62)",
        "📅 Data: 15 Tet, 20 Tet",
        "🛫 Wizz Air · direkt · 1 orë 35 min",
        "🔁 Kthimi nga €24",
        '👉 <a href="https://www.aviasales.com/search/TIA2010BGY1'
        '?currency=eur&amp;marker=12345.telegram">Rezervo tani</a>',
        '🏨 <a href="https://hotels.example/?city=Milan&amp;in=2026-10-20&amp;out=2026-10-27">Hotele në Milan</a>',
        '📱 <a href="https://esim.example/BGY">eSIM</a>   🛡️ <a href="https://insurance.example/?m=12345">Sigurim</a>',
        "⏳ Çmimet ndryshojnë shpejt!",
        "🔔 Ndiq @flyfromtirana për oferta çdo ditë",
    ])


def test_optional_lines_are_left_out():
    plain = Route(iata="VIE", city="Vienna", city_en="Vienna", flag="🇦🇹")
    deal = Deal(route=plain, quotes=[make_quote(19, destination="VIE", transfers=1, duration_min=None)],
                median=None, reason="threshold")
    assert post(deal) == "\n".join([
        "✈️ TIRANA → VIENNA 🇦🇹",            # no (airport)
        "💰 nga €19 one way",                    # no median yet
        "📅 Data: 20 Tet",
        "🛫 Wizz Air · me ndalesë",          # no duration
        # no return line, no hotel line, no eSIM/insurance line
        '👉 <a href="https://www.aviasales.com/search/TIA2010VIE1'
        '?currency=eur&amp;marker=12345.telegram">Rezervo tani</a>',
        "⏳ Çmimet ndryshojnë shpejt!",
        "🔔 Ndiq @flyfromtirana për oferta çdo ditë",
    ])


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


def test_helpers():
    assert format_price(19.0) == "19"
    assert format_price(19.01) == "20"  # rounded up, never under-advertised
    assert format_dates([date(2026, 11, 2), date(2026, 10, 30)], WORDS["sq"]["months"]) == "30 Tet, 2 Nën"
    assert format_duration(135, WORDS["sq"]) == "2 orë 15 min"
    assert format_duration(120, WORDS["sq"]) == "2 orë"
    assert format_duration(55, WORDS["sq"]) == "55 min"
    assert format_duration(None, WORDS["sq"]) is None
