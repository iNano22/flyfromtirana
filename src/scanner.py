"""Fetches flight prices from the Travelpayouts (Aviasales) Data API.

Endpoint: GET https://api.travelpayouts.com/aviasales/v3/prices_for_dates
Docs:     https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API

The Data API returns *cached* prices that Aviasales users found in recent
searches, not live availability, so some dates or routes can have gaps. That's
fine for spotting deals: the booking link always opens a live search.

One call covers a whole month (departure_at=YYYY-MM), so a 60-day window is
2-3 calls per route and direction.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import requests

from src.http_client import request_with_retry

log = logging.getLogger(__name__)

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


class ScannerError(Exception):
    """Fatal problem (e.g. the token is rejected): stop the whole run."""


class RouteFetchError(Exception):
    """One route failed: log it and carry on with the other routes."""


@dataclass(frozen=True)
class Quote:
    """The cheapest one-way price seen for one route on one departure date."""

    origin: str
    destination: str
    depart_date: date
    price: float                # in the configured currency (EUR)
    airline: str                # IATA airline code, e.g. "W6"
    transfers: int              # 0 = direct
    duration_min: int | None    # flight time in minutes, if the API gave one
    link: str | None            # Aviasales search path from the API, e.g. "/search/TIA1510BGY1?t=..."
    fetched_at: datetime

    @property
    def is_direct(self) -> bool:
        return self.transfers == 0


class PriceScanner:
    def __init__(
        self,
        token: str,
        *,
        currency: str = "eur",
        market: str | None = None,
        lookahead_days: int = 60,
        request_delay_seconds: float = 0.5,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.token = token
        self.currency = currency
        self.market = market
        self.lookahead_days = lookahead_days
        self.request_delay_seconds = request_delay_seconds
        self.session = session or requests.Session()
        self.sleep = sleep  # swapped out in tests so they don't actually wait

    def fetch_route(self, origin: str, destination: str, today: date, fetched_at: datetime) -> list[Quote]:
        """Cheapest one-way quote per departure date, from tomorrow to today + lookahead_days.

        Raises RouteFetchError if every month request failed, ScannerError on a bad token.
        """
        first_day = today + timedelta(days=1)
        last_day = today + timedelta(days=self.lookahead_days)

        cheapest_by_date: dict[date, Quote] = {}
        months = months_between(first_day, last_day)
        failed_months = []
        for month in months:
            try:
                rows = self._fetch_month(origin, destination, month)
            except RouteFetchError as exc:
                log.warning("%s→%s %s: %s", origin, destination, month, exc)
                failed_months.append(month)
                continue
            for row in rows:
                quote = parse_row(row, origin, destination, fetched_at)
                if quote is None or not first_day <= quote.depart_date <= last_day:
                    continue
                current = cheapest_by_date.get(quote.depart_date)
                if current is None or quote.price < current.price:
                    cheapest_by_date[quote.depart_date] = quote

        if len(failed_months) == len(months):
            raise RouteFetchError(f"{origin}→{destination}: all {len(months)} requests failed")

        quotes = sorted(cheapest_by_date.values(), key=lambda q: q.depart_date)
        log.info("%s→%s: prices for %d dates", origin, destination, len(quotes))
        return quotes

    def _fetch_month(self, origin: str, destination: str, month: str) -> list[dict]:
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": month,   # "YYYY-MM" = every day of that month
            "one_way": "true",
            "direct": "false",       # include flights with stops
            "currency": self.currency,
            "sorting": "price",
            "unique": "false",
            "limit": 1000,
            "page": 1,
        }
        if self.market:
            params["market"] = self.market

        try:
            # Token goes in a header, not the URL, so it never shows up in logs.
            response = request_with_retry(
                self.session, "GET", API_URL,
                params=params, headers={"X-Access-Token": self.token}, sleep=self.sleep,
            )
        except requests.RequestException as exc:
            raise RouteFetchError(f"network error ({type(exc).__name__})") from None
        finally:
            self.sleep(self.request_delay_seconds)  # be polite between calls

        if response.status_code in (401, 403):
            raise ScannerError(
                f"Travelpayouts rejected the API token (HTTP {response.status_code}). "
                "Check TRAVELPAYOUTS_TOKEN."
            )
        if response.status_code != 200:
            raise RouteFetchError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            body = response.json()
        except ValueError:
            raise RouteFetchError("response was not JSON") from None
        if not body.get("success", False):
            raise RouteFetchError(f"API error: {body.get('error') or body}")
        return body.get("data") or []


def parse_row(row: dict, origin: str, destination: str, fetched_at: datetime) -> Quote | None:
    """Turn one API row into a Quote, or None if it's malformed or for another airport."""
    # Asked for an airport (e.g. BGY), the API may answer for the whole city
    # (MIL = MXP + BGY + LIN). Keep only rows for the airport we asked about.
    if not _same_place(origin, row.get("origin"), row.get("origin_airport")):
        return None
    if not _same_place(destination, row.get("destination"), row.get("destination_airport")):
        return None
    try:
        depart_date = datetime.fromisoformat(row["departure_at"]).date()
        price = float(row["price"])
    except (KeyError, TypeError, ValueError):
        log.debug("Skipping malformed row: %r", row)
        return None

    duration = row.get("duration_to") or row.get("duration")
    return Quote(
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        price=price,
        airline=str(row.get("airline") or ""),
        transfers=int(row.get("transfers") or 0),
        duration_min=int(duration) if duration else None,
        link=row.get("link") or None,
        fetched_at=fetched_at,
    )


def _same_place(wanted: str, city_code: str | None, airport_code: str | None) -> bool:
    if not city_code and not airport_code:
        return True  # the API didn't say; trust that it answered our query
    return wanted in (city_code, airport_code)


def months_between(start: date, end: date) -> list[str]:
    """['2026-10', '2026-11', ...] for every month touched by start..end."""
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months
