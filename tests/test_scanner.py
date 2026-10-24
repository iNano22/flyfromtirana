"""Scanner tests with a fake HTTP session: no network, no real token."""
from datetime import date

import pytest
import requests

from src.config import ConfigError, load_config
from src.scanner import PriceScanner, RouteFetchError, ScannerError, found_on, months_between
from tests.conftest import NOW, FakeResponse, FakeSession, api_row, travelpayouts_handler

TODAY = date(2026, 10, 9)


def scanner_for(session) -> tuple[PriceScanner, list]:
    waits = []
    scanner = PriceScanner("secret-token", lookahead_days=60, request_delay_seconds=0.5,
                           session=session, sleep=waits.append)
    return scanner, waits


def test_months_between():
    assert months_between(date(2026, 10, 10), date(2026, 12, 8)) == ["2026-10", "2026-11", "2026-12"]
    assert months_between(date(2026, 12, 20), date(2027, 1, 5)) == ["2026-12", "2027-01"]


def test_fetch_route_parses_and_keeps_cheapest_per_day():
    rows = [
        api_row("TIA", "BGY", date(2026, 10, 20), 35),
        api_row("TIA", "BGY", date(2026, 10, 20), 19, airline="FR", transfers=1, duration_to=200),
        api_row("TIA", "BGY", date(2026, 11, 3), 42),
        api_row("TIA", "BGY", date(2026, 10, 9), 5),    # today: outside the window (starts tomorrow)
        api_row("TIA", "BGY", date(2026, 12, 20), 5),   # beyond 60 days
    ]
    session = FakeSession(travelpayouts_handler(rows))
    scanner, _ = scanner_for(session)

    quotes = scanner.fetch_route("TIA", "BGY", TODAY, NOW)

    assert [(q.depart_date, q.price) for q in quotes] == [(date(2026, 10, 20), 19), (date(2026, 11, 3), 42)]
    first = quotes[0]
    assert (first.airline, first.transfers, first.duration_min) == ("FR", 1, 200)
    assert first.link.startswith("/search/TIA2010BGY1")
    assert first.fetched_at == NOW


def found(day: date) -> str:
    """A link like the API's, with the day an Aviasales user found the price."""
    return f"/search/TIA2010BGY1?t=x&search_date={day:%d%m%Y}&expected_price=17"


def test_found_on_reads_the_search_date_in_the_link():
    assert found_on({"link": found(date(2026, 10, 7))}) == date(2026, 10, 7)
    assert found_on({"link": "/search/TIA2010BGY1?t=x"}) is None          # no date: age unknown
    assert found_on({"link": "/search/TIA2010BGY1?search_date=31022026"}) is None   # no such day
    assert found_on({}) is None


def test_prices_found_too_long_ago_are_left_out():
    rows = [
        api_row("TIA", "BGY", date(2026, 10, 20), 9, link=found(date(2026, 10, 6))),    # 3 days old: out
        api_row("TIA", "BGY", date(2026, 10, 20), 19, link=found(date(2026, 10, 7))),   # 2 days old: kept
        api_row("TIA", "BGY", date(2026, 10, 21), 8, link=found(date(2026, 10, 1))),    # out, and the date with it
        api_row("TIA", "BGY", date(2026, 10, 22), 25),                                  # no date: kept
    ]
    scanner = PriceScanner("secret-token", request_delay_seconds=0, max_price_age_days=2,
                           session=FakeSession(travelpayouts_handler(rows)), sleep=lambda _: None,
                           today=lambda: TODAY)
    quotes = scanner.fetch_route("TIA", "BGY", TODAY, NOW)
    assert [(q.depart_date, q.price) for q in quotes] == [(date(2026, 10, 20), 19), (date(2026, 10, 22), 25)]

    # The same rule for return tickets (the trip watches).
    rows = [api_row("TIA", "BGY", date(2026, 10, 20), 30, return_at="2026-10-23T09:00:00+02:00",
                    link=found(date(2026, 10, 5)))]
    scanner.session = FakeSession(travelpayouts_handler(rows))
    assert scanner.fetch_return_fares("TIA", "BGY", date(2026, 10, 10), date(2026, 10, 31)) == []

    # No limit: every price counts, however old.
    scanner.max_price_age_days = None
    scanner.session = FakeSession(travelpayouts_handler(rows))
    assert len(scanner.fetch_return_fares("TIA", "BGY", date(2026, 10, 10), date(2026, 10, 31))) == 1


def test_request_sends_token_in_header_not_url():
    session = FakeSession(travelpayouts_handler([]))
    scanner, _ = scanner_for(session)
    scanner.fetch_route("TIA", "BGY", TODAY, NOW)

    assert len(session.calls) == 3  # Oct, Nov, Dec
    call = session.calls[0]
    assert call["headers"] == {"X-Access-Token": "secret-token"}
    assert "secret-token" not in str(call["params"]) and "secret-token" not in call["url"]
    assert call["params"]["departure_at"] == "2026-10"
    assert call["params"]["one_way"] == "true"
    assert call["params"]["currency"] == "eur"


def test_rows_for_other_airports_in_same_city_are_dropped():
    # Asked for BGY, the API answers for all of Milan (city code MIL)
    rows = [
        api_row("TIA", "BGY", date(2026, 10, 20), 30, destination="MIL", destination_airport="BGY"),
        api_row("TIA", "BGY", date(2026, 10, 21), 10, destination="MIL", destination_airport="MXP"),
    ]
    session = FakeSession(lambda m, u, kw: FakeResponse(200, {"success": True, "data": rows}))
    scanner, _ = scanner_for(session)
    quotes = scanner.fetch_route("TIA", "BGY", TODAY, NOW)
    assert {q.price for q in quotes} == {30}


def test_bad_token_is_fatal():
    session = FakeSession(lambda m, u, kw: FakeResponse(401, {"error": "Unauthorized"}))
    scanner, _ = scanner_for(session)
    with pytest.raises(ScannerError, match="TRAVELPAYOUTS_TOKEN"):
        scanner.fetch_route("TIA", "BGY", TODAY, NOW)


def test_rate_limit_is_retried_with_backoff():
    responses = iter([FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(503)]
                     + [FakeResponse(200, {"success": True, "data": []})] * 3)
    session = FakeSession(lambda m, u, kw: next(responses))
    scanner, waits = scanner_for(session)

    scanner.fetch_route("TIA", "BGY", TODAY, NOW)

    assert len(session.calls) == 5  # 2 retries + 3 months
    assert waits[:2] == [3.0, 4.0]  # server asked for 3s; then backoff 2s * 2^1
    assert waits.count(0.5) == 3   # polite pause after each month


def test_one_failed_month_still_returns_the_others():
    def handler(method, url, kwargs):
        if kwargs["params"]["departure_at"] == "2026-11":
            return FakeResponse(400, {"success": False, "error": "bad request"})
        return FakeResponse(200, {"success": True, "data": [api_row("TIA", "BGY", date(2026, 10, 20), 19)]})

    scanner, _ = scanner_for(FakeSession(handler))
    assert len(scanner.fetch_route("TIA", "BGY", TODAY, NOW)) == 1


def test_route_fails_when_every_month_fails():
    session = FakeSession(lambda m, u, kw: FakeResponse(200, {"success": False, "error": "oops"}))
    scanner, _ = scanner_for(session)
    with pytest.raises(RouteFetchError):
        scanner.fetch_route("TIA", "BGY", TODAY, NOW)


def test_network_errors_become_route_errors():
    def handler(method, url, kwargs):
        raise requests.ConnectionError("boom")

    scanner, _ = scanner_for(FakeSession(handler))
    with pytest.raises(RouteFetchError):
        scanner.fetch_route("TIA", "BGY", TODAY, NOW)


def test_max_price_age_days_is_read_and_checked(tmp_path):
    assert load_config().max_price_age_days == 2

    def load(extra_yaml):
        path = tmp_path / "config.yaml"
        path.write_text(f"routes:\n  - {{ iata: BGY, city: Milan }}\n{extra_yaml}\n", encoding="utf-8")
        return load_config(path)

    assert load("").max_price_age_days == 2                              # not set = 2 days
    assert load("max_price_age_days: 0").max_price_age_days == 0         # today's prices only
    assert load("max_price_age_days: null").max_price_age_days is None   # any age
    with pytest.raises(ConfigError, match="max_price_age_days"):
        load("max_price_age_days: -1")
