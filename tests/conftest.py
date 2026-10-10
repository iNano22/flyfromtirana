"""Shared test helpers: a fake HTTP session (no real network) and sample-data builders."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.config import DealRules, Route
from src.scanner import Quote
from src.storage import Storage

# 10:00 UTC = 12:00 in Tirana, outside quiet hours.
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body=None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}
        self.text = json.dumps(json_body) if json_body is not None else ""

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class FakeSession:
    """Stands in for requests.Session.

    `handler(method, url, kwargs)` returns a FakeResponse (or raises).
    Every call is recorded in `.calls` so tests can check what was sent.
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


def telegram_text(call: dict) -> str:
    """The text of one recorded Telegram call: a plain message's text, or a photo's caption."""
    return call["json"]["text"] if "json" in call else call["data"]["caption"]


def telegram_chat(call: dict) -> str:
    return (call.get("json") or call["data"])["chat_id"]


def api_row(origin: str, destination: str, day: date, price: float, /, **extra) -> dict:
    """One row shaped like the prices_for_dates response. **extra overrides any field."""
    row = {
        "origin": origin, "destination": destination,
        "origin_airport": origin, "destination_airport": destination,
        "price": price, "airline": "W6", "flight_number": "4123",
        "departure_at": f"{day.isoformat()}T07:00:00+02:00",
        "transfers": 0, "duration": 95, "duration_to": 95,
        "link": f"/search/{origin}{day:%d%m}{destination}1?t=test",
    }
    row.update(extra)
    return row


def travelpayouts_handler(rows: list[dict]):
    """Fake API: answers each request with the rows matching its origin/destination/month."""
    def handler(method, url, kwargs):
        params = kwargs["params"]
        matching = [
            r for r in rows
            if r["origin_airport"] == params["origin"]
            and r["destination_airport"] == params["destination"]
            and r["departure_at"].startswith(params["departure_at"])
        ]
        return FakeResponse(200, {"success": True, "data": matching, "currency": "eur"})
    return handler


def make_quote(price: float, depart: date = date(2026, 10, 20), *, origin: str = "TIA",
               destination: str = "BGY", airline: str = "W6", transfers: int = 0,
               duration_min: int | None = 95, link: str | None = None,
               fetched_at: datetime = NOW) -> Quote:
    return Quote(origin=origin, destination=destination, depart_date=depart, price=price,
                 airline=airline, transfers=transfers, duration_min=duration_min, link=link,
                 fetched_at=fetched_at)


def seed_history(storage: Storage, prices: list[float], *, destination: str = "BGY",
                 days_ago: int = 1) -> None:
    """Pretend earlier runs saw these prices (spread over different dates)."""
    fetched = NOW - timedelta(days=days_ago)
    quotes = [make_quote(p, date(2026, 11, 1) + timedelta(days=i), destination=destination, fetched_at=fetched)
              for i, p in enumerate(prices)]
    storage.save_quotes(quotes)


@pytest.fixture
def storage():
    with Storage(":memory:") as s:
        yield s


@pytest.fixture
def route() -> Route:
    return Route(iata="BGY", city="Milan", city_en="Milan", flag="🇮🇹", airport="Bergamo",
                 absolute_threshold_eur=25)


@pytest.fixture
def rules() -> DealRules:
    return DealRules(min_samples_for_median=5)
