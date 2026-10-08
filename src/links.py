"""Builds every affiliate link in one place, so switching programs is a config change.

- Flights: Aviasales search page tagged with your Travelpayouts marker,
  optionally wrapped in the tp.media tracking redirect (config: links.flight).
- Everything else (hotel, eSIM, insurance, ...): URL templates from
  config.yaml (links.partners). An empty url means "leave it out of the post".
"""
from __future__ import annotations

from datetime import date
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from src.config import ConfigError, LinkSettings, LinkTemplate, Route
from src.scanner import Quote


class LinkBuilder:
    def __init__(self, settings: LinkSettings, marker: str, origin: str = "TIA"):
        self.settings = settings
        self.marker = marker
        self.origin = origin

    @property
    def partner_names(self) -> list[str]:
        return list(self.settings.partners)

    def tracking_marker(self) -> str:
        """Marker plus SubID, e.g. "123456.telegram", so stats show which channel earned the click."""
        if self.marker and self.settings.sub_id:
            return f"{self.marker}.{self.settings.sub_id}"
        return self.marker

    def flight(self, quote: Quote) -> str:
        """Aviasales search for this flight, tagged with our marker."""
        path = quote.link or aviasales_search_path(quote.origin, quote.destination, quote.depart_date)
        url = f"{self.settings.flight_base_url}/{path.lstrip('/')}"
        if self.marker:
            url = add_query_params(url, marker=self.tracking_marker())
        return self._wrap(url, self.settings.flight_wrapper, "links.flight")

    def partner(self, name: str, route: Route, checkin: date, checkout: date) -> str | None:
        """Link for a partner program (hotel, esim, ...), or None if its url isn't configured."""
        template = self.settings.partners.get(name) or LinkTemplate()
        if not template.url:
            return None
        values = {
            "city": route.city,
            "city_en": route.city_en,
            "iata": route.iata,
            "origin": self.origin,
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "marker": self.marker,
            "sub_id": self.settings.sub_id,
        }
        encoded = {key: quote(str(value), safe="") for key, value in values.items()}
        url = _fill(template.url, encoded, f"links.partners.{name}.url")
        return self._wrap(url, template.wrapper, f"links.partners.{name}")

    def validate(self) -> None:
        """Build every link once with sample data, so a typo in config.yaml fails at startup."""
        sample_route = Route(iata="XXX", city="Qytet", city_en="City", flag="")
        sample_day = date(2000, 1, 1)
        for name in self.settings.partners:
            self.partner(name, sample_route, sample_day, sample_day)
        self._wrap("https://example.com", self.settings.flight_wrapper, "links.flight")

    def _wrap(self, url: str, wrapper: str, where: str) -> str:
        """Put `url` inside a tracking redirect like tp.media/r?...&u={url}, if one is configured."""
        if not wrapper:
            return url
        values = {
            "url": quote(url, safe=""),
            "marker": quote(self.marker, safe=""),
            "sub_id": quote(self.settings.sub_id, safe=""),
        }
        return _fill(wrapper, values, f"{where}.wrapper")


def aviasales_search_path(origin: str, destination: str, depart: date, passengers: int = 1) -> str:
    """Aviasales search path: /search/TIA1510BGY1 = TIA -> BGY on 15 Oct, 1 adult."""
    return f"/search/{origin}{depart:%d%m}{destination}{passengers}"


def add_query_params(url: str, **params: str) -> str:
    """Add or replace query parameters, keeping the ones already in the URL."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(parts._replace(query=urlencode(query)))


def _fill(template: str, values: dict[str, str], where: str) -> str:
    try:
        return template.format_map(values)
    except KeyError as exc:
        allowed = " ".join("{" + key + "}" for key in values)
        raise ConfigError(f"{where} uses unknown placeholder {{{exc.args[0]}}} (allowed: {allowed})") from None
    except (ValueError, IndexError) as exc:  # e.g. a stray "{"
        raise ConfigError(f"{where} is not a valid template: {exc}") from None
