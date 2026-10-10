"""Deal detection: decide which of this run's prices are worth posting.

For each route:
  1. Work out the "normal" price: the median of every price seen for the
     route in the last 30 days (only once we have enough observations).
  2. A date is a deal if its price is deal_discount_pct below that median,
     or at/below the route's absolute threshold AND at least
     threshold_min_discount_pct below the median (when we know the median).
  3. Skip dates we already posted recently at the same price band (dedupe).
     With a premium channel, the free channel also skips dates premium hasn't
     had for long enough (early_access_from / early_access_until below).
  4. Bundle the cheapest remaining date with other similarly cheap dates
     into one Deal (one post per route), plus the cheapest flight back.
Then rank all routes' deals and keep the best few.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.config import DealRules, Route
from src.scanner import Quote
from src.storage import Storage

log = logging.getLogger(__name__)

# Names used in the posted_deals table. Each channel dedupes independently;
# future ones (Instagram, English) get their own name too.
CHANNEL = "telegram"
PREMIUM_CHANNEL = "telegram_premium"
# A run posts a little after its slot starts (the scan comes first), so "posted
# 6h ago" is checked with this much slack. Otherwise a deal could miss a run by
# 2 minutes.
EARLY_ACCESS_SLACK = timedelta(minutes=30)


@dataclass
class Deal:
    route: Route
    quotes: list[Quote]          # dates shown in the post, cheapest first (quotes[0] = headline)
    median: float | None         # typical price for the route, if we have enough history
    reason: str | None           # why it's a deal: "median" or "threshold" (for logs).
                                 # None = not a deal: the website lists every route, deal or not.
    return_quote: Quote | None = None  # cheapest flight back, if any
    premium_lead_hours: int | None = None  # free channel: how many hours earlier premium got this

    @property
    def best(self) -> Quote:
        return self.quotes[0]

    @property
    def is_deal(self) -> bool:
        return self.reason is not None

    @property
    def score(self) -> float:
        """Price as a fraction of the normal price. Lower = better deal; used for ranking."""
        reference = self.median or self.route.absolute_threshold_eur or self.best.price
        return self.best.price / reference

    def summary(self) -> str:
        """One-line description for logs, e.g. 'TIA→BGY 2026-10-15 €19 (median €45, -58%)'."""
        text = f"{self.best.origin}→{self.best.destination} {self.best.depart_date} €{self.best.price:.0f}"
        if self.median:
            text += f" (median €{self.median:.0f}, -{(1 - self.best.price / self.median) * 100:.0f}%)"
        return f"{text} [{self.reason}]"


def price_band(price: float, band_eur: float) -> int:
    """Bucket a price for dedupe: with €10 bands, €20-29.99 -> 2, €30-39.99 -> 3."""
    return int(price // band_eur)


def deal_reason(price: float, median: float | None, threshold: float | None,
                discount_pct: float, threshold_min_discount_pct: float = 0) -> str | None:
    """Return why this price is a deal ("median" / "threshold"), or None if it isn't one."""
    if median is not None:
        if price <= median * (1 - discount_pct / 100):
            return "median"
        if price > median * (1 - threshold_min_discount_pct / 100):
            # Under the absolute threshold but barely cheaper than usual:
            # "€14 (usually ~€18)" isn't exciting enough to post.
            return None
    if threshold is not None and price <= threshold:
        return "threshold"
    return None


def find_route_deal(
    route: Route,
    origin: str,
    outbound: list[Quote],
    inbound: list[Quote],
    storage: Storage,
    rules: DealRules,
    now: datetime,
    channel: str,
    early_access_from: str | None = None,
    early_access_until: datetime | None = None,
) -> Deal | None:
    """Best not-yet-posted deal for one route, or None.

    early_access_from / early_access_until: only allow dates that were already
    posted on that channel (premium) at or before that time.
    """
    median, samples = storage.route_median(origin, route.iata, since=now - timedelta(days=rules.median_window_days))
    if samples < rules.min_samples_for_median:
        log.debug("%s: only %d prices in history, median not used yet", route.iata, samples)
        median = None
    cheapest = min((q.price for q in outbound), default=None)
    log.debug("%s: median %s from %d prices, cheapest now %s, threshold %s", route.iata,
              f"€{median:.0f}" if median else "n/a", samples,
              f"€{cheapest:.0f}" if cheapest else "n/a", route.absolute_threshold_eur)

    cooldown_start = now - timedelta(days=rules.repost_cooldown_days)
    candidates: list[tuple[Quote, str]] = []
    early_posted_at: dict[Quote, datetime] = {}
    for quote in sorted(outbound, key=lambda q: q.price):
        reason = deal_reason(quote.price, median, route.absolute_threshold_eur,
                             rules.discount_pct, rules.threshold_min_discount_pct)
        if reason is None:
            continue
        band = price_band(quote.price, rules.price_band_eur)
        if storage.was_posted(channel, origin, route.iata, quote.depart_date, band, since=cooldown_start):
            log.info("Skipping %s→%s %s €%.0f: already posted", origin, route.iata, quote.depart_date, quote.price)
            continue
        if early_access_from:
            first = storage.first_posted_at(early_access_from, origin, route.iata, quote.depart_date,
                                            since=cooldown_start, until=early_access_until)
            if first is None:
                continue  # premium hasn't had it long enough yet
            early_posted_at[quote] = first
        candidates.append((quote, reason))

    if not candidates:
        return None

    best, reason = candidates[0]
    # Other dates shown in the same post must be about as cheap as the headline price.
    max_price = best.price * (1 + rules.date_price_tolerance_pct / 100)
    shown = [q for q, _ in candidates if q.price <= max_price][: rules.max_dates_per_post]
    lead = None
    if best in early_posted_at:
        lead = round((now - early_posted_at[best]).total_seconds() / 3600)
    return Deal(route=route, quotes=shown, median=median, reason=reason,
                return_quote=cheapest_return(best, inbound, rules), premium_lead_hours=lead)


def early_access_cutoff(now: datetime, free_delay_hours: float) -> datetime:
    """A deal premium got at or before this moment is old enough for the free channel and the website."""
    return now - timedelta(hours=free_delay_hours) + EARLY_ACCESS_SLACK


def is_premium_only(quote: Quote, route: Route, median: float | None, storage: Storage,
                    premium_rules: DealRules, now: datetime, cutoff: datetime) -> bool:
    """Is this price still for premium members only?

    True for a price that is a deal by premium's rules until premium has had
    it since `cutoff` (see early_access_cutoff). The website leaves such
    prices out, the same way the free channel waits for them.
    """
    reason = deal_reason(quote.price, median, route.absolute_threshold_eur,
                         premium_rules.discount_pct, premium_rules.threshold_min_discount_pct)
    if reason is None:
        return False  # an ordinary price: nothing to hold back
    cooldown_start = now - timedelta(days=premium_rules.repost_cooldown_days)
    first = storage.first_posted_at(PREMIUM_CHANNEL, quote.origin, route.iata, quote.depart_date,
                                    since=cooldown_start, until=cutoff)
    return first is None


def cheapest_return(outbound: Quote, inbound: list[Quote], rules: DealRules) -> Quote | None:
    """Cheapest flight back that leaves return_min_days..return_max_days after the outbound one."""
    earliest = outbound.depart_date + timedelta(days=rules.return_min_days)
    latest = outbound.depart_date + timedelta(days=rules.return_max_days)
    options = [q for q in inbound if earliest <= q.depart_date <= latest]
    return min(options, key=lambda q: q.price, default=None)


def rank_deals(deals: list[Deal], limit: int) -> list[Deal]:
    """Best deals first (biggest saving vs. normal price), at most `limit`."""
    return sorted(deals, key=lambda d: d.score)[:limit]
