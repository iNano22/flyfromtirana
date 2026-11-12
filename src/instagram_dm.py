"""Answers the private messages of the Instagram account, in Albanian (or in the person's own language).

    python -m src.instagram_dm "Sa kushton Londra?"   # print the reply the bot would write now; sends nothing

How a message travels:
  1. Someone writes to @flyfromtirana on Instagram.
  2. Meta sends it to the webhook, https://flyfromtirana.al/meta/webhook. The `web` container (Caddy,
     web/Caddyfile) passes that address on to this module's small web server, which runs in a thread
     of the scanner container (src/scheduler.py).
  3. The server checks Meta's signature (META_APP_SECRET), answers "received" straight away (Meta
     wants that within a few seconds) and puts the message in a queue.
  4. A second thread takes the messages one at a time. People often split one question over several
     messages, so the bot waits instagram_dm.reply_delay_seconds after each message (showing "typing...")
     and answers everything the person wrote in one reply. Claude gets the person's last few messages,
     plus a table of every route's current prices (the website's), and writes the reply. When they
     ask about particular dates, Claude looks those days up in the database (the flight_prices tool);
     a message with a date in it always gets a lookup. A reply that quotes a price Claude was not given
     is written again, and never sent as it is.
  5. The reply goes back through the Graph API with the Page token (src/meta.py).

A message that is only the word "OFERTA" (instagram_dm.deals_keywords) skips Claude: the bot
answers at once with the day's best prices from the website's list (deals_reply). Instagram lets
an account write only to people who wrote to it first, within 24 hours of their last message,
so this is how followers "subscribe": they send the word whenever they want the latest prices.

The same word as a comment under a post or reel gets that reply too, as a private message
(instagram_dm.deals_from_comments; handle_comment). Instagram allows one private message per
comment; once the person writes back in the chat, the bot answers them like anyone else.

The bot never books and never takes anyone's details: it gives the price and the city's page on
the website, where people book themselves. When the owner answers someone by hand in the
Instagram app, the bot keeps quiet with that person for instagram_dm.owner_pause_hours.

Setup (Meta's side, the permissions, App Review): README "Instagram private messages".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import re
import sys
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv

from src.config import PROJECT_ROOT, Config, InstagramDMSettings, load_config
from src.deals import early_access_cutoff, is_premium_only
from src.formatter import WORDS, format_dates, format_price
from src.growth import join_link
from src.meta import MetaClient, MetaError
from src.storage import Storage
from src.website import build_offers, page_url, site_base_url, site_guides

log = logging.getLogger("flyfromtirana.instagram_dm")

WEBHOOK_PATH = "/meta/webhook"   # web/Caddyfile sends this address to the scanner container
PORT = 8080
MAX_BODY_BYTES = 1_000_000       # a webhook delivery is a few kilobytes; anything bigger isn't Meta
# The variables the bot needs (docker-compose.yml). Without all four it stays off.
REQUIRED_ENV = ("META_PAGE_TOKEN", "META_APP_SECRET", "META_VERIFY_TOKEN", "ANTHROPIC_API_KEY")

INSTAGRAM_TEXT_BYTES = 1000      # Instagram's limit for one message; a longer reply goes as several
HISTORY_HOURS = 24               # Claude reads the chat's messages from this far back
KEEP_DAYS = 30                   # messages are forgotten after this long
MAX_TOOL_ROUNDS = 4              # price look-ups Claude may make for one reply
MAX_DRAFTS = 2                   # how many times Claude may write a reply that quotes a price it wasn't given
MAX_LOOKUP_DAYS = 62             # the scan only goes lookahead_days (60) ahead anyway
MAX_ROUND_TRIPS = 5              # the cheapest out-and-back pairs the lookup offers
# Models that can hand a request they decline to another model (Anthropic's "server-side fallback").
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")

# Months in Albanian, English and Italian (also written without ë). A message with one of these, or a day
# number, is about particular dates: Claude must look them up before it answers.
MONTHS = ("(?:janar|shkurt|mars|prill|maj|qershor|korrik|gusht|shtator|tetor|n[eë]ntor|dhjetor)(?:i|it|in)?|"
          "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|"
          "oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
          "gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre")
DATE_PATTERN = re.compile(rf"(?<![\d€])([1-9]|[12]\d|3[01])(?![\d€]|\s€)|\b(?:{MONTHS})\b", re.IGNORECASE)
# A price in a reply or in the data: "€89", "€ 89", "89€", "89 euro", "€89,00".
PRICE_PATTERN = re.compile(r"€\s*(\d+)|(\d+)(?:[.,]\d+)?\s*(?:€|euro|eur\b)", re.IGNORECASE)
PLACEHOLDER = "[a photo, voice message or post]"   # what is kept for a message without text

# What the bot says without asking Claude.
TEXTS = {
    "not_text": ("Përshëndetje! 🙂 Lexoj vetëm mesazhe me shkrim. Më shkruaj qytetin ku do të shkosh "
                 "dhe kur, e të them çmimin më të mirë nga Tirana."),
    "error": "Më fal, s'po arrij të përgjigjem tani. Çmimet e fundit nga Tirana i gjen këtu: {site}",
    # The reply to "OFERTA" (deals_reply): its first line, its last lines, and what it says without prices.
    "deals_top": "✈️ Çmimet më të mira sot nga Tirana (vajtje, për person):",
    "deals_end": ("Të gjitha datat dhe rezervimi: {site}\n"
                  "Çmimet ndryshojnë shpejt. Shkruaj {keyword} kur të duash për më të fundit."),
    "no_deals": "Tani për tani s'kemi çmime të reja. Provo sërish pas pak orësh, ose shiko këtu: {site}",
    # The same two, for the private message a comment gets. No link in these: Instagram refused
    # that message when it had one ("Invalid message id"). The person writes back, and the reply
    # to that (an ordinary message) carries the link.
    "deals_end_comment": ("Çmimet ndryshojnë shpejt. Na shkruaj këtu emrin e qytetit ose {keyword} "
                          "dhe të dërgojmë linkun për rezervim."),
    "no_deals_comment": ("Tani për tani s'kemi çmime të reja. Na shkruaj këtu emrin e qytetit "
                         "dhe të themi çmimin e fundit."),
}

# Claude's instructions. The parts in {braces} are filled in from config.yaml.
INSTRUCTIONS = """\
You answer the private messages of {brand} on Instagram ({handle}). {brand} finds cheap flights \
from Tirana airport (TIA) and shows them on its website {site} and its Telegram channel {channel}.

Language: answer in Albanian. If the person writes in English, Italian or another language, answer \
in their language. Many people write Albanian without ë and ç, or with typos ("Londor" = London): \
understand them, but write correct Albanian yourself. People often split one question over several \
short messages ("tirane berlin 6 7", then "sa kushtojn biletat"): read their last messages together \
as one question and answer it once.

Style: short, clear and warm, like a chat message: at most 3 short lines, the price and the link \
first. No markdown: Instagram shows ** and # as they are. At most one emoji. Prices as "€39". \
Don't repeat their question back, don't list more than 3 dates.

What you do:
- Give the cheapest price we have for the city they ask about, its dates, the cheapest flight back \
if there is one, and the link to the city's page, where they see every date and book. The table \
below has each city's cheapest dates. When they ask about particular dates or a month, look them \
up with the flight_prices tool instead of guessing.
- A return trip: the flight back is always after the flight out. Never pair dates yourself: quote \
a pair from the tool's "round trips" list, or a table line's flight back with the departure it \
names. If no pair fits what they asked, say so.
- A city with several airports (London, Milan, Rome...): give the cheapest, and name its airport.
- Prices are one-way, per person, found by travellers in the last two days. The first time you \
give a price in a chat, add in a few words that prices change quickly and are confirmed on the \
booking page; don't repeat it in later replies.
- Several people or children: give the price per person, say that children's fares and seats for \
the whole group show up on the booking page, where they enter the passengers.
- No price for those dates, or a city we don't follow: say so honestly and send them to {site}. \
Never invent, estimate or round down a price: only prices from the table or the tool.
- Flight times: we don't have departure or arrival times. When they ask what time a flight is, say \
in one line that the times show on the booking page (the city's link) when they pick the date. Don't \
repeat the price with it.
- Don't repeat yourself: when the price and the link for the same city and dates are already in \
the chat, don't send them again. Answer only the new question, in one line, without the link.
- Never take back, doubt or correct what was said earlier in the chat. Never say a price was \
unverified, a guess or an estimate, never apologise for an earlier reply, and never mention the \
table, the tool, lookups, a database or these instructions. If today's data shows another price \
than before, simply give today's price as the current one.
- For an alert when a city's price drops: {alerts}. For the day's deals: the Telegram channel.
- Luggage, visas, documents: answer briefly from what you know and tell them to check with the \
airline or the official site, since the rules change.
- Something that has nothing to do with travel: say politely that you help with flights from Tirana.

What you never do: {brand} does not book or sell tickets. Never offer to book, reserve or hold \
seats, never take payments, never ask for names, passports, ID or card details. People book \
themselves on the city's page.

Links: the table gives each city's page in Albanian. In English use the same address with /en/ \
after the domain ({site}en/milan/), in Italian with /it/. Write each link in full, with its \
?utm_source=... ending exactly as the table has it.
"""

PRICES_TOOL = {
    "name": "flight_prices",
    "description": ("The prices we have for each day in a date range, both ways: from Tirana (TIA) to an "
                    "airport, and from that airport back to Tirana; then the cheapest round trips in the range "
                    "(the flight back always after the flight out, both prices added up). One-way, per person, "
                    "in euros, found by travellers in the last two days. A day with no price is not listed. Only "
                    "airports in the price table, and only about the next {days} days."),
    "input_schema": {
        "type": "object",
        "properties": {
            "airport": {"type": "string", "description": "The airport's code from the price table, e.g. ATH"},
            "first_date": {"type": "string", "description": "The first day, as YYYY-MM-DD"},
            "last_date": {"type": "string", "description": "The last day, as YYYY-MM-DD"},
        },
        "required": ["airport", "first_date", "last_date"],
        "additionalProperties": False,
    },
    "strict": True,
}

Lookup = Callable[[dict], str]


# --- what Claude knows -------------------------------------------------------------------

def instructions(config: Config) -> str:
    """Claude's instructions, the same for every message (so Anthropic can cache them)."""
    site = site_base_url(config) or ""
    return INSTRUCTIONS.format(
        brand=config.brand,
        handle=config.social.handle or config.channel_handle,
        site=site,
        channel=join_link(config, "instagram") or config.channel_handle,
        alerts=alerts_bot(config) or "the Telegram channel",
    )


DM_UTM_SOURCE = "igdm"   # utm_source on the site links the bot sends

def alerts_bot(config: Config) -> str | None:
    """Where people set price alerts (src/bot.py): the bot's chat, or None with alerts off."""
    alerts = config.alerts
    if not (alerts.enabled and alerts.bot_username):
        return None
    return f"our Telegram bot https://t.me/{alerts.bot_username} (they pick the city and the price)"


def price_table(config: Config, storage: Storage, now: datetime) -> str:
    """Today's date and one line per city: its cheapest dates, the flight back, its page. The website's prices."""
    months = WORDS["en"]["months"]
    guides = site_guides(config)   # the Albanian guides: their page addresses and the cities' Albanian names
    today = now.astimezone(ZoneInfo(config.timezone)).date()
    lines = [f"Today is {today:%A} {today.day} {months[today.month - 1]} {today.year}.",
             "",
             "Current prices: one-way from Tirana, per person, euros.",
             "Airport code | city | cheapest dates | flight back | page"]
    priced = set()
    for offer in build_offers(config, storage, now):
        route, best = offer.route, offer.best
        priced.add(route.city)
        guide = guides.get(route.city)
        city = route.city
        if guide and guide.name and guide.name != route.city:
            city += f" ({guide.name})"
        if route.airport:
            city += f", {route.airport} airport"
        stops = "direct" if best.is_direct else "with stops"
        airline = config.airlines.get(best.airline, best.airline)
        cheapest = (f"€{format_price(best.price)} on {format_dates([q.depart_date for q in offer.quotes], months)}"
                    f" ({airline}, {stops})")
        if offer.is_deal and offer.median:
            cheapest += f", a deal: usually about €{format_price(offer.median)}"
        back = offer.return_quote
        back_text = (f"from €{format_price(back.price)} on {format_dates([back.depart_date], months)}, "
                     f"after the {format_dates([best.depart_date], months)} flight out" if back else "-")
        page = page_url(config, config.website.languages[0], guide.slug if guide else None) or ""
        if page:
            # Marks the visit as coming from the DM bot: Google Analytics shows it as source
            # "igdm", and the site adds it to the booking links' SubID (templates/site_source.html).
            page += f"?utm_source={DM_UTM_SOURCE}&utm_medium=dm"
        lines.append(f"{route.iata} | {city} {route.flag} | {cheapest} | {back_text} | {page}")
    waiting = [route.city for route in config.routes if route.city not in priced]
    if waiting:
        lines += ["", "Cities we follow but have no fresh price for right now: "
                  + ", ".join(dict.fromkeys(waiting))]
    return "\n".join(lines)


def look_up_prices(config: Config, storage: Storage, now: datetime, request: dict) -> str:
    """The flight_prices tool: every day's price in a range, both ways, as short lines for Claude."""
    routes = {route.iata: route for route in config.routes}
    airport = str(request.get("airport", "")).strip().upper()
    route = routes.get(airport)
    if route is None:
        return f"We don't scan {airport or 'that airport'}. Airports we scan: {', '.join(routes)}."
    try:
        first = date.fromisoformat(str(request.get("first_date", "")))
        last = date.fromisoformat(str(request.get("last_date", "")))
    except ValueError:
        return "The dates must be written as YYYY-MM-DD."
    if last < first:
        first, last = last, first
    last = min(last, first + timedelta(days=MAX_LOOKUP_DAYS))
    months = WORDS["en"]["months"]

    def day(d: date) -> str:
        return f"{d.day} {months[d.month - 1]} {d.year}"

    lines, found = [], []
    for origin, destination in ((config.origin, airport), (airport, config.origin)):
        quotes = [q for q in storage.latest_quotes(origin, destination) if first <= q.depart_date <= last]
        if origin == config.origin and config.premium.enabled:
            # The same rule as the website: a deal the paid channel has first stays hidden until it's public.
            median, samples = storage.route_median(origin, destination,
                                                   since=now - timedelta(days=config.rules.median_window_days))
            median = median if samples >= config.rules.min_samples_for_median else None
            cutoff = early_access_cutoff(now, config.premium.free_delay_hours)
            quotes = [q for q in quotes
                      if not is_premium_only(q, route, median, storage, config.premium.rules, now, cutoff)]
        found.append(quotes)
        days = [f"{day(q.depart_date)}: €{format_price(q.price)} ({config.airlines.get(q.airline, q.airline)}, "
                f"{'direct' if q.is_direct else 'with stops'})"
                for q in sorted(quotes, key=lambda q: q.depart_date)]
        lines.append(f"{origin} → {destination} ({route.city}): " + ("; ".join(days) or "no prices for these days"))
    # The pairs are made here, not left to Claude, which once paired a flight back with a later flight out.
    outbound, inbound = found
    trips = sorted(((out, back) for out in outbound for back in inbound if back.depart_date > out.depart_date),
                   key=lambda trip: trip[0].price + trip[1].price)[:MAX_ROUND_TRIPS]
    pairs = []
    for out, back in trips:
        there, home = format_price(out.price), format_price(back.price)   # whole euros, as quoted
        pairs.append(f"out {day(out.depart_date)} €{there} + back {day(back.depart_date)} €{home} "
                     f"= €{int(there) + int(home)}")
    lines.append("Round trips, cheapest first: " + ("; ".join(pairs) or "none in these days"))
    return "\n".join(lines)


# --- asking Claude -------------------------------------------------------------------------

class Writer:
    """Asks Claude for the reply to a chat."""

    def __init__(self, settings: InstagramDMSettings, client: anthropic.Anthropic):
        self.settings = settings
        self.client = client

    def reply(self, rules: str, prices: str, chat: list[tuple[str, str]], lookup: Lookup) -> str | None:
        """Claude's reply to the chat (oldest message first, the person's newest last). None when it
        couldn't write one: Anthropic was unreachable, the model declined, or its reply kept quoting
        prices it was never given (a reply with a made-up price is never sent)."""
        system = [{"type": "text", "text": rules},
                  # The cache mark goes on the last part: the instructions and the table are cached
                  # together, and the next message within a few minutes is much cheaper.
                  {"type": "text", "text": prices, "cache_control": {"type": "ephemeral"}}]
        messages: list[dict] = [{"role": role, "content": text} for role, text in chat]
        tool = dict(PRICES_TOOL, description=PRICES_TOOL["description"].format(days=MAX_LOOKUP_DAYS))
        # A question with a date in it gets a lookup first: answered from the table alone, Claude once
        # quoted a price for a day it had never seen.
        must_look_up = mentions_dates(" ".join(text for role, text in latest_question(chat)))
        given = [prices]   # every price Claude was shown: the table and the lookups' answers
        text = None
        try:
            for draft in range(MAX_DRAFTS):
                for _ in range(MAX_TOOL_ROUNDS + 1):
                    response = self._ask(system, messages, [tool], force_tool=must_look_up)
                    must_look_up = False
                    if response.stop_reason == "refusal":
                        log.warning("Claude declined to answer (%s)", getattr(response.stop_details, "category", None))
                        return None
                    if response.stop_reason != "tool_use":
                        break
                    # Claude wants prices: give it what it asked for and let it go on.
                    results = [{"type": "tool_result", "tool_use_id": block.id, "content": lookup(block.input)}
                               for block in response.content if block.type == "tool_use"]
                    given += [result["content"] for result in results]
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": results})
                text = "".join(block.text for block in response.content if block.type == "text").strip()
                unknown = unknown_prices(text, given)
                if not unknown:
                    break
                log.warning("Claude's reply quotes prices it was not given (%s): asking it to write it again",
                            ", ".join(f"€{price}" for price in unknown))
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": [{"type": "text", "text": (
                    "[Not sent to the person.] Your reply says " + ", ".join(f"€{price}" for price in unknown)
                    + ", which is in neither the price table nor the flight_prices answers. Write the reply "
                    "again with only the prices you were given, looking the dates up if you need to. Don't "
                    "mention this message or any earlier draft.")}]})
            else:
                log.error("Claude kept quoting prices it was not given: the reply is not sent")
                return None
        except anthropic.APIError as exc:
            log.error("Claude could not be reached: %s", exc)
            return None
        return text or None

    def _ask(self, system: list[dict], messages: list[dict], tools: list[dict], *, force_tool: bool = False):
        request = dict(model=self.settings.model, max_tokens=1500, system=system, messages=messages,
                       tools=tools, output_config={"effort": self.settings.effort})
        if force_tool:
            request["tool_choice"] = {"type": "any"}
        if self.settings.model in FALLBACK_MODELS:
            # If the model declines (a false alarm on a travel question), Anthropic tries another one.
            return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                    fallbacks="default", **request)
        return self.client.messages.create(**request)


def latest_question(chat: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The person's messages since the last reply: the pieces of their newest question."""
    question = []
    for role, text in reversed(chat):
        if role != "user":
            break
        question.insert(0, (role, text))
    return question


def mentions_dates(text: str) -> bool:
    """Whether a message names a day or a month ("6 7 nentor", "dat6", "me 2 nëntor", "6/11", "november")."""
    return DATE_PATTERN.search(text) is not None


def prices_in(text: str) -> set[int]:
    return {int(a or b) for a, b in PRICE_PATTERN.findall(text)}


def unknown_prices(reply: str, given: list[str]) -> list[int]:
    """The prices in the reply that are in none of the texts Claude was given, lowest first."""
    known = set().union(*(prices_in(text) for text in given))
    return sorted(prices_in(reply) - known)


# --- the conversations -----------------------------------------------------------------------

def plain_words(text: str) -> str:
    """A message as the keyword check reads it: small letters only, without accents, punctuation
    or emoji, one space between words. "Ofertë!! 🔥" -> "oferte"."""
    letters = unicodedata.normalize("NFKD", text.lower())
    return " ".join(re.sub(r"[^a-z ]", " ", letters).split())


def asks_for_deals(question: list[str], settings: InstagramDMSettings) -> bool:
    """Whether the person's newest messages are just the keyword ("OFERTA"), and nothing else.

    Anything longer ("oferta per ne Rome ne dhjetor") is a real question and goes to Claude."""
    keywords = {plain_words(word) for word in settings.deals_keywords}
    return plain_words(" ".join(question)) in keywords


def deals_reply(config: Config, storage: Storage, now: datetime, *, link: bool = True) -> str:
    """The reply to "OFERTA": the first routes of the website's list (deals first), one per line,
    then the site's link. Written here, not by Claude: the prices are exactly the website's.

    link=False is for the private message a comment gets: no address in it, and a last line
    asking the person to write back instead."""
    settings = config.instagram_dm
    site = site_base_url(config) or config.channel_handle
    if site.startswith("http"):
        site += f"?utm_source={DM_UTM_SOURCE}&utm_medium=dm"   # counted as the DM bot's, like the city links
    offers = build_offers(config, storage, now)[:settings.deals_count]
    if not offers:
        return TEXTS["no_deals"].format(site=site) if link else TEXTS["no_deals_comment"]
    months = WORDS["sq"]["months"]
    guides = site_guides(config)   # the cities' Albanian names
    lines = [TEXTS["deals_top"]]
    for offer in offers:
        route, best = offer.route, offer.best
        guide = guides.get(route.city)
        city = guide.name if guide and guide.name else route.city
        if route.airport:
            city += f" ({route.airport})"
        line = f"{city} €{format_price(best.price)} · {format_dates([best.depart_date], months)}"
        if offer.is_deal and offer.median:
            line += f" (zakonisht €{format_price(offer.median)})"
        lines.append(line)
    end = TEXTS["deals_end"] if link else TEXTS["deals_end_comment"]
    lines.append(end.format(site=site, keyword=settings.deals_keywords[0].upper()))
    return "\n".join(lines)


@dataclass
class Pending:
    """A person whose messages are waiting for their reply."""

    due: datetime        # when the reply is written, if they write nothing more before then
    has_text: bool       # whether any of the waiting messages has text (else they sent a photo or a voice)


class DMBot:
    """Everything here runs in the one replying thread (`work`): the pending dict needs no lock."""

    def __init__(self, config: Config, meta: MetaClient, writer: Writer, *,
                 clock: Callable[[], datetime] | None = None):
        self.config = config
        self.settings = config.instagram_dm
        self.meta = meta
        self.writer = writer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.rules = instructions(config)
        self.pending: dict[str, Pending] = {}
        # Reads the deliveries that arrived while a reply was being written (set by start()), so a
        # reply to a question the person has since added to is thrown away and written again.
        self.drain: Callable[[], None] | None = None
        self.answering = False

    def handle_payload(self, payload: dict) -> None:
        """One webhook delivery from Meta. It can carry several messages, from several people."""
        if payload.get("object") != "instagram":
            return
        with Storage(self.config.db_path) as storage:
            storage.prune_dms(self.clock() - timedelta(days=KEEP_DAYS))
            for entry in payload.get("entry") or []:
                # Messages come under "messaging". The Test button in Meta's webhook settings sends
                # its sample under "changes" instead, as {"field": "messages", "value": <the message>}.
                events = list(entry.get("messaging") or [])
                events += [change.get("value") or {} for change in entry.get("changes") or []
                           if change.get("field") == "messages"]
                for event in events:
                    try:
                        self.handle_event(event, storage)
                    except Exception:   # one bad message must not stop the others
                        log.exception("Could not handle an Instagram message")
                # Comments under our posts and reels come under "changes" too, as field "comments".
                for change in entry.get("changes") or []:
                    if change.get("field") != "comments":
                        continue
                    try:
                        self.handle_comment(change.get("value") or {}, str(entry.get("id") or ""), storage)
                    except Exception:
                        log.exception("Could not handle an Instagram comment")
        self.answer_due()

    def handle_comment(self, comment: dict, account: str, storage: Storage) -> None:
        """A comment under one of our posts. If it is just the keyword ("OFERTA"), send its writer
        the day's prices as a private message. Any other comment is left alone.

        `comment` is {"id": ..., "text": ..., "from": {"id": ..., "username": ...}, "media": {...}}
        and `account` is our own Instagram id."""
        comment_id = str(comment.get("id") or "")
        writer = comment.get("from") or {}
        person = str(writer.get("id") or "")
        text = (comment.get("text") or "").strip()
        if not self.settings.deals_from_comments or not comment_id or not person or person == account:
            return   # switched off, an incomplete notification, or our own comment
        if not asks_for_deals([text], self.settings):
            return
        now = self.clock()
        # Kept like a message, so the same comment is never answered twice (Meta sends a comment
        # on a boosted post two times) and Claude sees it if the person writes back in the chat.
        if not storage.save_dm(f"comment-{comment_id}", person, "user", text, now):
            return
        if storage.count_dm_replies(person, now - timedelta(days=1)) >= self.settings.max_replies_per_day:
            log.warning("Not answering %s's comment: %d replies in a day already", person,
                        self.settings.max_replies_per_day)
            return
        # One message only: Instagram allows no second one until the person writes back.
        reply = split_message(deals_reply(self.config, storage, now, link=False))[0]
        try:
            recipient, mid = self.meta.send_instagram_private_reply(comment_id, reply)
        except MetaError as exc:
            log.error("Could not answer the comment of %s: %s", writer.get("username") or person, exc)
            return
        # Saved under the person's chat, so the message's echo is known as the bot's, not the owner's.
        storage.save_dm(mid or f"bot-{uuid.uuid4()}", recipient or person, "assistant", reply, self.clock())
        log.info("Sent the prices to %s, who commented %r", writer.get("username") or person, text)

    def handle_event(self, event: dict, storage: Storage) -> None:
        """Keep the message; the reply is written once they've stopped writing (answer_due)."""
        message = event.get("message")
        # Reactions, "seen" receipts and deleted messages come as events too: nothing to answer.
        if not message or not message.get("mid") or message.get("is_deleted") or message.get("is_unsupported"):
            return
        now = self.clock()
        mid = message["mid"]
        text = (message.get("text") or "").strip()
        if message.get("is_echo"):
            # A message our account sent: one of the bot's (already saved), or the owner's from the app.
            person = (event.get("recipient") or {}).get("id")
            if person and storage.save_dm(mid, person, "assistant", text or "[a picture]", now):
                storage.pause_dm(person, now + timedelta(hours=self.settings.owner_pause_hours))
                log.info("The owner answered %s by hand: the bot keeps quiet with them for %g hours",
                         person, self.settings.owner_pause_hours)
            return
        person = (event.get("sender") or {}).get("id")
        if not person:
            return
        attachments = [a.get("type") for a in message.get("attachments") or []]
        if not text and "story_mention" in attachments:
            return   # they mentioned the account in their story: nothing to reply to
        if not storage.save_dm(mid, person, "user", text or PLACEHOLDER, now):
            return   # Meta sent this one before: it has its answer
        log.info("A message from %s (%d characters)", person, len(text))
        waiting = self.pending.get(person)
        if waiting is None:
            self.show_typing(person)   # they see the dots while the bot waits for the rest of their question
        self.pending[person] = Pending(due=now + timedelta(seconds=self.settings.reply_delay_seconds),
                                       has_text=bool(text) or (waiting is not None and waiting.has_text))

    def answer_due(self) -> None:
        """Reply to everyone who has written nothing more for reply_delay_seconds."""
        if self.answering:   # a drain while a reply is written: the new messages only wait their turn
            return
        now = self.clock()
        due = [person for person, waiting in self.pending.items() if waiting.due <= now]
        if not due:
            return
        self.answering = True
        try:
            with Storage(self.config.db_path) as storage:
                for person in due:
                    waiting = self.pending.pop(person)
                    try:
                        self.answer(person, waiting, storage, now)
                    except Exception:
                        log.exception("Could not answer %s", person)
        finally:
            self.answering = False

    def answer(self, person: str, waiting: Pending, storage: Storage, now: datetime) -> None:
        paused = storage.dm_paused_until(person)
        if paused and paused > now:
            log.info("Not answering %s: the owner is talking to them", person)
            return
        if storage.count_dm_replies(person, now - timedelta(days=1)) >= self.settings.max_replies_per_day:
            log.warning("Not answering %s: %d replies in a day already", person, self.settings.max_replies_per_day)
            return
        written = storage.count_dms_from(person)
        if waiting.has_text and asks_for_deals(self.question(person, storage, now), self.settings):
            reply = deals_reply(self.config, storage, now)   # "OFERTA": the day's prices, without Claude
        elif waiting.has_text:
            self.show_typing(person)
            reply = self.write_reply(person, storage, now)
        else:
            reply = TEXTS["not_text"]
        if self.drain:
            self.drain()   # whatever arrived while Claude was writing
        if storage.count_dms_from(person) != written:
            # They wrote more meanwhile: this reply doesn't cover it. handle_event has put them back in
            # the queue, and the next reply answers everything.
            log.info("%s wrote more while the reply was written: writing it again", person)
            return
        self.send(person, reply, storage)

    def question(self, person: str, storage: Storage, now: datetime) -> list[str]:
        """The texts the person sent since the bot's last reply."""
        chat = storage.dm_history(person, now - timedelta(hours=HISTORY_HOURS), self.settings.history_messages + 1)
        return [text for _, text in latest_question(chat)]

    def write_reply(self, person: str, storage: Storage, now: datetime) -> str:
        chat = storage.dm_history(person, now - timedelta(hours=HISTORY_HOURS), self.settings.history_messages + 1)
        while chat and chat[0][0] != "user":   # Claude's side of a chat must start with the person
            chat.pop(0)
        reply = self.writer.reply(self.rules, price_table(self.config, storage, now), chat,
                                  lambda request: look_up_prices(self.config, storage, now, request))
        return reply or TEXTS["error"].format(site=site_base_url(self.config) or self.config.channel_handle)

    def show_typing(self, person: str) -> None:
        try:
            self.meta.send_instagram_typing(person)
        except MetaError as exc:   # only the dots: the reply still goes
            log.debug("Could not show %s the typing dots: %s", person, exc)

    def send(self, person: str, text: str, storage: Storage) -> None:
        parts = split_message(text)
        for part in parts:
            try:
                mid = self.meta.send_instagram_message(person, part)
            except MetaError as exc:
                log.error("Could not send the reply to %s: %s", person, exc)
                return
            # Saved before the next webhook is read, so its echo is known as the bot's, not the owner's.
            storage.save_dm(mid or f"bot-{uuid.uuid4()}", person, "assistant", part, self.clock())
        log.info("Answered %s (%d characters)", person, len(text))


def split_message(text: str, limit: int = INSTAGRAM_TEXT_BYTES) -> list[str]:
    """Cut a reply into pieces Instagram takes (at most `limit` bytes each), between lines where it can."""
    parts, current = [], ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode()) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(line.encode()) > limit:   # one line longer than the limit: cut it between words
            cut = line.encode()[:limit].decode(errors="ignore")
            cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
            parts.append(cut)
            line = line[len(cut):].lstrip()
        current = line
    if current.strip():
        parts.append(current)
    return parts


# --- the webhook --------------------------------------------------------------------------

def signature_ok(body: bytes, header: str, app_secret: str) -> bool:
    """Meta signs every delivery with the app's secret: X-Hub-Signature-256: sha256=<hex>."""
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.strip().encode())


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], verify_token: str, app_secret: str):
        super().__init__(address, WebhookHandler)
        self.verify_token = verify_token
        self.app_secret = app_secret
        self.inbox: queue.Queue[dict] = queue.Queue()


class WebhookHandler(BaseHTTPRequestHandler):
    server: WebhookServer

    def do_GET(self) -> None:
        """Meta checks the address once, when it is saved in the app's settings: echo its challenge."""
        url = urlsplit(self.path)
        if url.path != WEBHOOK_PATH:
            return self.answer(404)
        query = parse_qs(url.query)
        mode, token, challenge = ((query.get(name) or [""])[0] for name in ("hub.mode", "hub.verify_token",
                                                                             "hub.challenge"))
        if mode == "subscribe" and hmac.compare_digest(token.encode(), self.server.verify_token.encode()):
            return self.answer(200, challenge)
        self.answer(403)

    def do_POST(self) -> None:
        """A delivery of messages: check it comes from Meta, queue it, say "received"."""
        if urlsplit(self.path).path != WEBHOOK_PATH:
            return self.answer(404)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self.answer(413)
        body = self.rfile.read(length)
        if not signature_ok(body, self.headers.get("X-Hub-Signature-256", ""), self.server.app_secret):
            log.warning("A webhook call without Meta's signature was refused. If it was Meta's, META_APP_SECRET "
                        "is the wrong secret: it must be the Instagram app secret (README: Instagram private messages)")
            return self.answer(403)
        try:
            payload = json.loads(body)
        except ValueError:
            return self.answer(400)
        self.server.inbox.put(payload)
        self.answer(200, "EVENT_RECEIVED")

    def answer(self, status: int, text: str = "") -> None:
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:   # every request in the log would be noise
        log.debug("%s %s", self.address_string(), format % args)


def work(bot: DMBot, inbox: queue.Queue, stop: threading.Event) -> None:
    """Read the queued deliveries one at a time, so a person's messages are answered in order, and
    every second answer the people whose waiting time is over."""
    bot.drain = lambda: drain(bot, inbox)
    while not stop.is_set():
        try:
            handle(bot, inbox.get(timeout=1))
        except queue.Empty:
            bot.answer_due()


def drain(bot: DMBot, inbox: queue.Queue) -> None:
    """Read every delivery that is already in the queue, without waiting for more."""
    while True:
        try:
            handle(bot, inbox.get_nowait())
        except queue.Empty:
            return


def handle(bot: DMBot, payload: dict) -> None:
    try:
        bot.handle_payload(payload)
    except Exception:
        log.exception("Could not handle a webhook delivery")


def start(config: Config, stop: threading.Event, env: Mapping[str, str] = os.environ) -> WebhookServer | None:
    """Start the webhook server and the replying thread (src/scheduler.py). None when the bot is off."""
    if not config.instagram_dm.enabled:
        log.info("instagram_dm.enabled is false: the Instagram messages are not answered")
        return None
    missing = [name for name in REQUIRED_ENV if not env.get(name, "").strip()]
    if missing:
        log.warning("The Instagram message bot is off: %s not set", ", ".join(missing))
        return None
    meta = MetaClient(env["META_PAGE_TOKEN"].strip())
    writer = Writer(config.instagram_dm, anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"].strip()))
    bot = DMBot(config, meta, writer)
    try:
        server = WebhookServer(("0.0.0.0", PORT), env["META_VERIFY_TOKEN"].strip(), env["META_APP_SECRET"].strip())
    except OSError as exc:
        log.error("The Instagram webhook could not open port %d: %s", PORT, exc)
        return None
    # daemon: a redeploy doesn't wait for them; Meta sends again what wasn't answered "received".
    threading.Thread(target=server.serve_forever, name="dm-webhook", daemon=True).start()
    threading.Thread(target=work, args=(bot, server.inbox, stop), name="dm-replies", daemon=True).start()
    log.info("Answering Instagram messages (%s): webhook on port %d at %s", config.instagram_dm.model, PORT,
             WEBHOOK_PATH)
    return server


def main(argv: list[str] | None = None) -> int:
    """Print the reply the bot would write to a message now, from the local database. Sends nothing."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    load_dotenv(PROJECT_ROOT / ".env")
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('Usage: python -m src.instagram_dm "Sa kushton Londra?"')
        return 2
    config = load_config()
    now = datetime.now(timezone.utc)
    if asks_for_deals(args, config.instagram_dm):   # "OFERTA": answered without Claude
        with Storage(config.db_path) as storage:
            print(deals_reply(config, storage, now))
        return 0
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log.error("ANTHROPIC_API_KEY is not set (put it in .env)")
        return 2
    writer = Writer(config.instagram_dm, anthropic.Anthropic(api_key=key))
    with Storage(config.db_path) as storage:
        reply = writer.reply(instructions(config), price_table(config, storage, now), [("user", " ".join(args))],
                             lambda request: look_up_prices(config, storage, now, request))
    print(reply or "(no reply: see the log above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
