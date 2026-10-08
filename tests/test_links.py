from dataclasses import replace
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from src.config import ConfigError, LinkSettings, LinkTemplate
from src.links import LinkBuilder
from tests.conftest import make_quote


def test_flight_link_keeps_api_params_and_adds_marker():
    links = LinkBuilder(LinkSettings(sub_id="telegram"), marker="12345")
    url = links.flight(make_quote(19, link="/search/TIA2010BGY1?t=abc"))
    assert url == "https://www.aviasales.com/search/TIA2010BGY1?t=abc&currency=eur&marker=12345.telegram"


def test_flight_link_built_when_api_gives_none():
    links = LinkBuilder(LinkSettings(), marker="12345")
    assert links.flight(make_quote(19, date(2026, 3, 5))) == "https://www.aviasales.com/search/TIA0503BGY1?currency=eur&marker=12345"


def test_flight_link_without_marker():
    links = LinkBuilder(LinkSettings(sub_id="telegram"), marker="")
    assert "marker" not in links.flight(make_quote(19))


def test_wrapper_encodes_target_url():
    settings = LinkSettings(flight_wrapper="https://tp.media/r?marker={marker}&p=4114&u={url}")
    url = LinkBuilder(settings, marker="12345").flight(make_quote(19, date(2026, 10, 20)))
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://tp.media/r?marker=12345&p=4114&u=https%3A%2F%2Fwww.aviasales.com")
    assert query["u"] == ["https://www.aviasales.com/search/TIA2010BGY1?currency=eur&marker=12345"]


def test_partner_template_fills_and_encodes(route):
    settings = LinkSettings(partners={"hotel": LinkTemplate(url="https://h.example/?q={city}&in={checkin}")})
    url = LinkBuilder(settings, marker="1").partner("hotel", route, date(2026, 10, 20), date(2026, 10, 23))
    assert url == "https://h.example/?q=Milano&in=2026-10-20"


def test_partner_with_non_ascii_city(route):
    settings = LinkSettings(partners={"hotel": LinkTemplate(url="https://h.example/?q={city}")})
    url = LinkBuilder(settings, marker="1").partner("hotel", replace(route, city="Romë"), date.today(), date.today())
    assert url == "https://h.example/?q=Rom%C3%AB"


def test_empty_partner_is_none(route):
    settings = LinkSettings(partners={"esim": LinkTemplate()})
    assert LinkBuilder(settings, marker="1").partner("esim", route, date.today(), date.today()) is None


def test_validate_catches_typos():
    settings = LinkSettings(partners={"hotel": LinkTemplate(url="https://h.example/?q={citty}")})
    with pytest.raises(ConfigError, match="citty"):
        LinkBuilder(settings, marker="1").validate()
