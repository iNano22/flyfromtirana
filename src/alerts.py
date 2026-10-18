"""Sends the price alerts people set with the bot ("tell me when Milan is under €30").

After every scan (main.run), each alert is checked against the city's current
prices, the ones the website shows (website.build_offers):

  - an alert with a price matches the city's cheapest flight at that price or less;
  - an "any deal" alert (no price) matches when that flight is a deal by the free
    channel's rules.

Free alerts wait like the website and the free channel: a deal Premium is still
getting first comes once the free channel may have it. Premium members (anyone in
the Premium channel) get theirs straight away, also from the 15-minute premium
scans, whose prices are not saved (they come in as `scanned`).

A person hears about a city once per price band within repost_cooldown_days,
whatever the date or airport, like the social posts: a cheaper band is news
again. That is remembered in posted_deals, as the "channel" alert:<chat id>.
Nothing goes out in quiet hours (the alerts wait for the morning). Someone who
blocked the bot loses their alerts. The message is templates/<language>_alert.txt,
with the destination's photo.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Callable

import requests

from src.config import Config
from src.deals import Deal, price_band
from src.formatter import format_post
from src.growth import join_link
from src.links import LinkBuilder
from src.photos import post_photo
from src.scanner import Quote
from src.storage import Alert, Storage
from src.telegram import TelegramClient, TelegramError, TelegramRefused
from src.website import build_offers

log = logging.getLogger(__name__)

MEMBER = ("member", "administrator", "creator")


def alert_channel(chat_id: int) -> str:
    """The name a person's alerts go under in posted_deals."""
    return f"alert:{chat_id}"


def send_alerts(config: Config, storage: Storage, *, token: str, premium_chat: str, links: LinkBuilder,
                now: datetime, quiet: bool, premium_only: bool = False,
                scanned: tuple[dict[str, list[Quote]], dict[str, list[Quote]]] | None = None,
                dry_run: bool = False, session: requests.Session | None = None,
                output: Callable[[str], None] = print) -> int:
    """Send every alert that has news. Returns 1 if Telegram couldn't be reached, else 0.

    premium_only: the quick premium scan; only Premium members' alerts are checked, against
    `scanned` (its prices). links: the booking links' builder (with the alerts' SubID).
    quiet: it is quiet hours (nothing is sent; a dry run still shows what would go out).
    """
    if not config.alerts.enabled:
        return 0
    alerts = storage.all_alerts()
    if not alerts or (quiet and not dry_run):
        return 0
    session = session or requests.Session()
    links = LinkBuilder(replace(links.settings, sub_id=config.alerts.sub_id), links.marker,
                        origin=config.origin, currency=config.currency)
    premium_on = config.premium.enabled and bool(premium_chat)

    # The prices each kind of person may have now: Premium sees everything at once.
    everything = build_offers(config, storage, now, premium_delay=False, scanned=scanned)
    waiting = [] if premium_only else (build_offers(config, storage, now) if premium_on else everything)
    members: dict[int, bool] = {}

    def is_member(chat_id: int) -> bool:
        """Is this person in the Premium channel? Asked once per run."""
        if not premium_on or dry_run:
            return False
        if chat_id not in members:
            try:
                members[chat_id] = TelegramClient(token, premium_chat, session).member_status(chat_id) in MEMBER
            except TelegramRefused:
                members[chat_id] = False   # never in the channel: Telegram says "user not found"
        return members[chat_id]

    failures = 0
    for alert in alerts:
        premium = is_member(alert.chat_id)
        if premium_only and not premium:
            continue
        offer = matching_offer(alert, everything if premium else waiting)
        if offer is None or already_sent(alert, offer, config, storage, now):
            continue
        text = alert_text(alert, offer, config, links)
        if dry_run:
            output(f"\n----- DRY RUN [alert to {alert.chat_id}] · {offer.summary()} -----\n{text}\n")
            continue
        photo = post_photo(offer.route.iata) if config.post_photos else None
        try:
            TelegramClient(token, str(alert.chat_id), session).send_post(text, photo.path if photo else None)
        except TelegramRefused as exc:
            if "blocked" in str(exc) or "chat not found" in str(exc) or "deactivated" in str(exc):
                gone = storage.delete_alerts(alert.chat_id)
                log.info("[alerts] %s can't be reached any more (%s): deleted their %d alert(s)",
                         alert.chat_id, exc, gone)
            else:
                log.error("[alerts] Telegram refused the alert to %s: %s", alert.chat_id, exc)
            continue
        except TelegramError as exc:
            log.error("[alerts] Could not send the alert to %s: %s", alert.chat_id, exc)
            failures += 1
            continue
        for quote in offer.quotes:
            storage.record_post(alert_channel(alert.chat_id), quote,
                                price_band(quote.price, config.rules.price_band_eur), now)
        log.info("[alerts] Sent %s to %s%s", offer.summary(), alert.chat_id, " (premium)" if premium else "")
    return 1 if failures else 0


def matching_offer(alert: Alert, offers: list[Deal]) -> Deal | None:
    """The city's cheapest flight that the alert asks for, or None."""
    fitting = [offer for offer in offers if offer.route.city == alert.city
               and (offer.is_deal if alert.max_price is None else offer.best.price <= alert.max_price)]
    return min(fitting, key=lambda offer: offer.best.price, default=None)


def already_sent(alert: Alert, offer: Deal, config: Config, storage: Storage, now: datetime) -> bool:
    """Did this person hear about this city at this price band (or cheaper) lately, from any airport?"""
    since = now - timedelta(days=config.rules.repost_cooldown_days)
    band = price_band(offer.best.price, config.rules.price_band_eur)
    airports = [route.iata for route in config.routes if route.city == alert.city]
    return any(storage.was_posted(alert_channel(alert.chat_id), offer.best.origin, code, None, band, since)
               for code in airports)


def alert_text(alert: Alert, offer: Deal, config: Config, links: LinkBuilder) -> str:
    """The alert message (Telegram HTML), from templates/<language>_alert.txt."""
    if alert.max_price is None:
        title = f"Ofertë për {alert.city}!"
    else:
        title = f"{alert.city} deri në €{alert.max_price:g}!"
    return format_post(offer, links, language=config.language, channel_handle=config.channel_handle,
                       airline_names=config.airlines, template=f"{config.language}_alert",
                       extra={"ALERT_TITLE": title, "CHANNEL_LINK": join_link(config, "bot")})
