"""Fetches flight prices from the Travelpayouts (Aviasales) Data API.

Endpoint: GET https://api.travelpayouts.com/aviasales/v3/prices_for_dates
Docs:     https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API

The Data API returns *cached* prices that Aviasales users found in recent
searches, not live availability, so some dates or routes can have gaps. That's
fine for spotting deals: the booking link always opens a live search.
A price can be up to a week old, though, and cheap seats sell out in days: rows
found more than max_price_age_days ago (the search_date in their link) are left
out, so the price on Aviasales matches the one we post more often.

One call covers a whole month (departure_at=YYYY-MM), so a 60-day window is
2-3 calls per route and direction.
"""
from __future__ import annotations

import logging
import re
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


@dataclass(frozen=True)
class ReturnFare:
    """The cheapest price seen for a return ticket: out and back in one booking."""

    origin: str
    destination: str
    depart_date: date
    return_date: date
    price: float                # both flights together, in the configured currency (EUR)
    airline: str                # IATA airline code of the flight out
    transfers: int              # stops out + stops back; 0 = direct both ways


class PriceScanner:
    def __init__(
        self,
        token: str,
        *,
        currency: str = "eur",
        market: str | None = None,
        lookahead_days: int = 60,
        request_delay_seconds: float = 0.5,
        max_price_age_days: int | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ):
        self.token = token
        self.currency = currency
        self.market = market
        self.lookahead_days = lookahead_days
        self.request_delay_seconds = request_delay_seconds
        self.max_price_age_days = max_price_age_days   # None = keep prices of any age
        self.session = session or requests.Session()
        self.sleep = sleep  # swapped out in tests so they don't actually wait
        self.today = today  # the same, so tests can say which day it is

    def fetch_route(self, origin: str, destination: str, today: date, fetched_at: datetime) -> list[Quote]:
        """Cheapest one-way quote per departure date, from tomorrow to today + lookahead_days.

        Raises RouteFetchError if every month request failed, ScannerError on a bad token.
        """
        return self.fetch_dates(origin, destination, today + timedelta(days=1),
                                today + timedelta(days=self.lookahead_days), fetched_at)

    def fetch_dates(self, origin: str, destination: str, first_day: date, last_day: date,
                    fetched_at: datetime) -> list[Quote]:
        """Cheapest one-way quote per departure date, from first_day to last_day (both included)."""
        cheapest_by_date: dict[date, Quote] = {}
        for row in self._fetch_rows(origin, destination, first_day, last_day, one_way=True):
            quote = parse_row(row, origin, destination, fetched_at)
            if quote is None or not first_day <= quote.depart_date <= last_day:
                continue
            current = cheapest_by_date.get(quote.depart_date)
            if current is None or quote.price < current.price:
                cheapest_by_date[quote.depart_date] = quote

        quotes = sorted(cheapest_by_date.values(), key=lambda q: q.depart_date)
        log.info("%s→%s: prices for %d dates", origin, destination, len(quotes))
        return quotes

    def fetch_return_fares(self, origin: str, destination: str, first_day: date, last_day: date) -> list[ReturnFare]:
        """Cheapest return ticket per pair of dates, leaving from first_day to last_day.

        Few people search return tickets, so there are far fewer of these than one-way prices.
        """
        cheapest: dict[tuple[date, date], ReturnFare] = {}
        for row in self._fetch_rows(origin, destination, first_day, last_day, one_way=False):
            fare = parse_return_row(row, origin, destination)
            if fare is None or not first_day <= fare.depart_date <= last_day:
                continue
            dates = (fare.depart_date, fare.return_date)
            if dates not in cheapest or fare.price < cheapest[dates].price:
                cheapest[dates] = fare
        log.info("%s⇄%s: return tickets for %d pairs of dates", origin, destination, len(cheapest))
        return sorted(cheapest.values(), key=lambda fare: (fare.depart_date, fare.return_date))

    def _fetch_rows(self, origin: str, destination: str, first_day: date, last_day: date, *,
                    one_way: bool) -> list[dict]:
        """Every row the API has for the months that first_day..last_day touch (one call per month).

        Raises RouteFetchError if every month request failed.
        """
        months = months_between(first_day, last_day)
        rows: list[dict] = []
        failed_months = []
        for month in months:
            try:
                rows += self._fetch_month(origin, destination, month, one_way=one_way)
            except RouteFetchError as exc:
                log.warning("%s→%s %s: %s", origin, destination, month, exc)
                failed_months.append(month)
        if len(failed_months) == len(months):
            raise RouteFetchError(f"{origin}→{destination}: all {len(months)} requests failed")
        return self._drop_old(rows, origin, destination)

    def _drop_old(self, rows: list[dict], origin: str, destination: str) -> list[dict]:
        """Leave out the rows found more than max_price_age_days ago. A row with no date is kept."""
        if self.max_price_age_days is None:
            return rows
        oldest = self.today() - timedelta(days=self.max_price_age_days)
        fresh = [row for row in rows if (found_on(row) or oldest) >= oldest]
        if len(fresh) < len(rows):
            log.info("%s→%s: left out %d price(s) found before %s", origin, destination,
                     len(rows) - len(fresh), oldest.isoformat())
        return fresh

    def _fetch_month(self, origin: str, destination: str, month: str, *, one_way: bool = True) -> list[dict]:
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": month,   # "YYYY-MM" = every day of that month
            "one_way": "true" if one_way else "false",   # false = return tickets, back on any date
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


def found_on(row: dict) -> date | None:
    """The day an Aviasales user found this price: the link's search_date=DDMMYYYY, if it has one."""
    match = re.search(r"[?&]search_date=(\d{2})(\d{2})(\d{4})", row.get("link") or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


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


def parse_return_row(row: dict, origin: str, destination: str) -> ReturnFare | None:
    """Turn one return-ticket row into a ReturnFare, or None if it's malformed or for another airport."""
    if not _same_place(origin, row.get("origin"), row.get("origin_airport")):
        return None
    if not _same_place(destination, row.get("destination"), row.get("destination_airport")):
        return None
    try:
        depart_date = datetime.fromisoformat(row["departure_at"]).date()
        return_date = datetime.fromisoformat(row["return_at"]).date()
        price = float(row["price"])
    except (KeyError, TypeError, ValueError):
        log.debug("Skipping malformed return row: %r", row)
        return None
    return ReturnFare(origin=origin, destination=destination, depart_date=depart_date, return_date=return_date,
                      price=price, airline=str(row.get("airline") or ""),
                      transfers=int(row.get("transfers") or 0) + int(row.get("return_transfers") or 0))


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
