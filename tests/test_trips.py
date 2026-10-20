from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.config import Secrets, TripWatch, load_config
from src.main import run as run_scan
from src.scanner import PriceScanner, ReturnFare
from src.storage import Storage
from src.telegram import TelegramClient
from src.trips import find_trips, fits, has_weekend, send_trip_watches, trip_text
from tests.conftest import FakeResponse, FakeSession, api_row, make_quote

# Tuesday 1 December 2026, 10:00 in Tirana: outside quiet hours.
NOW = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
OWNER = "6040871333"
WATCH = TripWatch(name="Memmingen before Christmas", destination="FMM", city="Memmingen",
                  earliest_departure=date(2026, 12, 1), latest_return=date(2026, 12, 24),
                  min_days=3, max_days=4, weekend=True, max_price=100)


def dec(day: int) -> date:
    return date(2026, 12, day)


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", request_delay_seconds=0, trip_watches=(WATCH,))


def world(one_way: list[dict], return_tickets: list[dict] = ()):
    """The price API (one-way rows, or return tickets when one_way=false) and Telegram."""
    def handler(method, url, kwargs):
        if "api.telegram.org" in url:
            return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})
        params = kwargs["params"]
        rows = one_way if params["one_way"] == "true" else list(return_tickets)
        data = [row for row in rows if row["origin_airport"] == params["origin"]
                and row["destination_airport"] == params["destination"]
                and row["departure_at"].startswith(params["departure_at"])]
        return FakeResponse(200, {"success": True, "data": data})
    return FakeSession(handler)


def return_ticket(depart: date, back: date, price: float) -> dict:
    return api_row("TIA", "FMM", depart, price, return_at=f"{back.isoformat()}T07:00:00+01:00", return_transfers=0)


def check(config, session, *, now=NOW, quiet=False, dry_run=False, printed=None) -> int:
    scanner = PriceScanner("tp-token", request_delay_seconds=0, session=session, sleep=lambda _: None)
    with Storage(config.db_path) as storage:
        owner = None if dry_run else TelegramClient("123:bot", OWNER, session)
        return send_trip_watches(config, storage, scanner, owner=owner, now=now, quiet=quiet, dry_run=dry_run,
                                 output=(printed.append if printed is not None else print))


def sent(session) -> list[str]:
    return [call["json"]["text"] for call in session.calls if "api.telegram.org" in call["url"]]


def test_a_trip_fits_with_3_or_4_days_a_weekend_and_back_before_christmas():
    assert fits(WATCH, dec(18), dec(20))        # Fri-Sun: 3 days
    assert fits(WATCH, dec(17), dec(20))        # Thu-Sun: 4 days
    assert fits(WATCH, dec(19), dec(22))        # Sat-Tue: 4 days
    assert not fits(WATCH, dec(19), dec(20))    # Sat-Sun: 2 days
    assert not fits(WATCH, dec(16), dec(20))    # Wed-Sun: 5 days
    assert not fits(WATCH, dec(15), dec(17))    # Tue-Thu: no weekend
    assert not fits(WATCH, dec(20), dec(22))    # Sun-Tue: only the Sunday
    assert not fits(WATCH, dec(22), dec(25))    # back on Christmas Day
    assert has_weekend(dec(5), dec(6)) and not has_weekend(dec(6), dec(7))


def test_one_way_tickets_and_return_tickets_make_trips_cheapest_first():
    outbound = [make_quote(30, dec(18), destination="FMM"), make_quote(45, dec(17), destination="FMM"),
                make_quote(10, dec(15), destination="FMM")]   # Tuesday: no trip from it has a weekend
    inbound = [make_quote(25, dec(20), origin="FMM", destination="TIA", airline="FR")]
    fares = [ReturnFare("TIA", "FMM", dec(18), dec(20), 50, "W4", 0),     # cheaper than the two one-ways
             ReturnFare("TIA", "FMM", dec(19), dec(20), 20, "W4", 0)]     # 2 days: too short
    trips = find_trips(WATCH, outbound, inbound, fares)
    assert [(t.depart, t.back, t.price) for t in trips] == [(dec(18), dec(20), 50), (dec(17), dec(20), 70)]
    assert trips[0].out_price is None and trips[1].out_price == 45


def test_the_owner_gets_the_cheapest_trips_with_their_searches(config):
    session = world([api_row("TIA", "FMM", dec(18), 30), api_row("FMM", "TIA", dec(20), 25, airline="FR"),
                     api_row("TIA", "FMM", dec(17), 45)])
    assert check(config, session) == 0
    [text] = sent(session)
    assert text.startswith("🧳 <b>Memmingen before Christmas</b>\nTirana ⇄ Memmingen from <b>€55</b>, both flights")
    assert ('• <a href="https://www.aviasales.com/search/TIA1812FMM20121?currency=eur">Fri 18 Dec → Sun 20 Dec</a>'
            ' (3 days): <b>€55</b>, €30 out + €25 back · Wizz Air out, Ryanair back') in text
    assert "Thu 17 Dec → Sun 20 Dec</a> (4 days): <b>€70</b>" in text
    assert all(call["json"]["chat_id"] == OWNER for call in session.calls if "api.telegram.org" in call["url"])


def test_once_per_price_band_until_it_gets_cheaper(config):
    trip = [api_row("FMM", "TIA", dec(20), 25)]
    check(config, world(trip + [api_row("TIA", "FMM", dec(18), 30)]))
    again = world(trip + [api_row("TIA", "FMM", dec(18), 33)])       # €58: the same €50s band
    check(config, again)
    assert sent(again) == []
    cheaper = world(trip + [api_row("TIA", "FMM", dec(18), 20)])     # €45: news
    check(config, cheaper)
    assert len(sent(cheaper)) == 1


def test_nothing_over_max_price_nor_in_quiet_hours_nor_once_the_dates_have_passed(config):
    expensive = world([api_row("TIA", "FMM", dec(18), 70), api_row("FMM", "TIA", dec(20), 40)])
    check(config, expensive)
    assert sent(expensive) == []
    cheap = [api_row("TIA", "FMM", dec(18), 30), api_row("FMM", "TIA", dec(20), 25)]
    night = world(cheap)
    check(config, night, quiet=True)
    assert night.calls == []                                          # not even the price API
    late = world(cheap)
    check(config, late, now=datetime(2026, 12, 23, 9, 0, tzinfo=timezone.utc))
    assert sent(late) == []


def test_return_tickets_count_and_a_dry_run_only_shows(config):
    session, printed = world([], [return_ticket(dec(4), dec(6), 48)]), []
    assert check(config, session, dry_run=True, printed=printed) == 0
    assert "Fri 4 Dec → Sun 6 Dec</a> (3 days): <b>€48</b>, return ticket · Wizz Air" in printed[0]
    assert sent(session) == []


def test_the_full_scan_checks_the_watches_and_writes_to_the_owner(config):
    session = world([api_row("TIA", "FMM", dec(18), 30), api_row("FMM", "TIA", dec(20), 25)])
    secrets = Secrets("tp-token", "12345", "123:bot", "@flyfromtirana", telegram_drafts_chat_id=OWNER)
    run_scan(config, secrets, dry_run=False, only_routes={"BGY"}, session=session, now=NOW)
    owner = [call["json"]["text"] for call in session.calls
             if "api.telegram.org" in call["url"] and call.get("json", {}).get("chat_id") == OWNER]
    assert any(text.startswith("🧳 <b>Memmingen before Christmas</b>") for text in owner)


def test_trip_text_lists_at_most_three_trips():
    config = load_config()
    outbound = [make_quote(20 + day, dec(day), destination="FMM") for day in (3, 4, 5, 11, 12)]
    inbound = [make_quote(20, dec(day), origin="FMM", destination="TIA") for day in (6, 7, 13, 14)]
    text = trip_text(WATCH, find_trips(WATCH, outbound, inbound, []), config)
    assert text.count("• ") == 3
