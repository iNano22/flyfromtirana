"""The bot's private chat: people set price alerts there. It also notes who joins the channels.

    python -m src.bot     # run the listener on its own, for a local test. Never while the server
                          # runs it: Telegram gives each update to whoever asks first.

People talk to the bot (alerts.bot_username, @FlyFromTiranaBot) in a private chat:

    /start               the cities as buttons; a city asks for the price; a price saves
                         the alert ("Milan, up to €30", or "Milan, any deal")
    /start alert_BGY     straight to that city's prices: the "Më njofto" link under a channel post
    /alerts              their alerts, each with a button to delete it
    /stop                deletes all their alerts

The alerts are sent after each scan (src/alerts.py). The same listener gets Telegram's
"chat_member" updates and hands them to src/growth.py, which counts who joined the
channels through which link. It is the bot's only reader of updates, and runs in a
thread of the scheduler (src/scheduler.py) with long polling: one request waits up to
WAIT_SECONDS for something to happen.
"""
from __future__ import annotations

import html
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from typing import Callable

import requests
from dotenv import load_dotenv

from src.config import PROJECT_ROOT, Config, load_config
from src.formatter import WORDS, format_dates, format_price
from src.growth import channel_chats, join_link, record_member_update
from src.storage import Alert, Storage
from src.telegram import Buttons, TelegramClient, TelegramError
from src.website import COUNTRY_NAMES, build_offers, country_code

log = logging.getLogger("flyfromtirana.bot")

WAIT_SECONDS = 50       # how long one request waits for news (Telegram allows up to 50)
RETRY_SECONDS = 30      # after Telegram couldn't be reached
CITIES_PER_ROW = 3
# Telegram allows 100 buttons under a message. With more cities than this, /start shows
# the countries first and a country its cities.
CITIES_PER_MENU = 30

# The bot's words (Albanian, like the posts). {city} etc. are filled in; HTML is allowed.
TEXTS = {
    "welcome": ("👋 Përshëndetje! Jam boti i <b>{brand}</b>.\n\n"
                "Zgjidh një qytet dhe çmimin që të përshtatet: sapo të gjej një biletë nga Tirana me atë "
                "çmim ose më lirë, të shkruaj këtu. Falas."),
    "choose": "Zgjidh qytetin:",
    "pick_price": "✈️ <b>Tirana → {city}</b>\n\nMë njofto kur bileta (vetëm vajtje) kushton deri në:",
    "saved_price": ("✅ U ruajt! Do të të shkruaj kur ka fluturim <b>Tirana → {city}</b> "
                    "deri në <b>€{price}</b>."),
    "saved_deal": ("✅ U ruajt! Do të të shkruaj kur ka <b>ofertë</b> për <b>Tirana → {city}</b>: "
                   "një çmim shumë më i ulët se zakonisht."),
    "now": "\n\nTani më e lira: <b>€{price}</b> ({dates}).",
    "channel": "\n\n📣 Ofertat e ditës: <a href=\"{link}\">kanali ynë në Telegram</a>",
    "list": "🔔 <b>Njoftimet e tua</b>",
    "line_price": "• {city}: deri në €{price}",
    "line_deal": "• {city}: çdo ofertë",
    "none": "Nuk ke ende asnjë njoftim. Zgjidh një qytet:",
    "too_many": "Ke {max} njoftime, sa më shumë. Fshi një për të shtuar një tjetër.",
    "deleted": "U fshi njoftimi për {city}",
    "stopped": "🗑 U fshinë të gjitha njoftimet. Për t'i nisur sërish: /start",
    "help": ("Zgjidh një qytet më poshtë, ose shkruaj /alerts për njoftimet e tua dhe /stop për t'i "
             "fshirë të gjitha."),
}
BUTTONS = {
    "any": "🔥 Çdo ofertë",
    "back": "⬅️ Qytetet",
    "countries": "⬅️ Shtetet",
    "add": "➕ Shto një qytet",
    "mine": "📋 Njoftimet e mia",
    "delete": "❌ {city}",
}
COMMANDS = [("start", "Zgjidh qytetin dhe çmimin"), ("alerts", "Njoftimet e mia"),
            ("stop", "Fshi të gjitha njoftimet")]
DESCRIPTION = "Të njoftoj kur del një biletë e lirë nga Tirana për qytetin që zgjedh. Falas."


def alert_link(config: Config, iata: str) -> str | None:
    """The link that opens the bot on a city's prices ("Më njofto" under a post), or None with alerts off."""
    alerts = config.alerts
    if not (alerts.enabled and alerts.bot_username):
        return None
    return f"https://t.me/{alerts.bot_username}?start=alert_{iata}"


class Bot:
    def __init__(self, config: Config, token: str, chats: dict[str, str], *,
                 session: requests.Session | None = None, clock: Callable[[], datetime] | None = None):
        """chats: the channels, for counting joins (growth.channel_chats). clock: tests set the time."""
        self.config = config
        self.token = token
        self.chats = chats
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cities = list(dict.fromkeys(route.city for route in config.routes))   # each once, in order
        # The cities of each country, countries in the order of their first route: {"it": ["Milan", ...]}
        self.countries: dict[str, list[str]] = {}
        self.flags: dict[str, str] = {}
        for route in config.routes:
            code = country_code(route.flag) or ""
            self.flags.setdefault(code, route.flag)
            if route.city not in self.countries.setdefault(code, []):
                self.countries[code].append(route.city)
        self.country_of = {city: code for code, cities in self.countries.items() for city in cities}
        self.city_of = {route.iata: route.city for route in config.routes}

    def client(self, chat_id: int | str = "") -> TelegramClient:
        return TelegramClient(self.token, str(chat_id), self.session)

    # --- listening ---------------------------------------------------------------------

    def listen(self, stop: threading.Event) -> None:
        """Read and handle updates until `stop` is set. An error never stops it: it waits and tries again."""
        if self.config.alerts.enabled:
            try:
                self.client().set_commands(COMMANDS, DESCRIPTION)
            except TelegramError as exc:
                log.warning("Could not set the bot's commands: %s", exc)
        log.info("The bot is listening%s", "" if self.config.alerts.enabled else " (alerts are off: joins only)")
        offset = None
        while not stop.is_set():
            try:
                updates = self.client().get_updates(offset, wait=WAIT_SECONDS)
            except TelegramError as exc:
                log.warning("Could not read the bot's updates: %s (trying again in %ds)", exc, RETRY_SECONDS)
                stop.wait(RETRY_SECONDS)
                continue
            if updates:
                self.handle_all(updates)
                offset = updates[-1]["update_id"] + 1
        if offset is not None:   # tell Telegram the last ones were handled, so a restart doesn't repeat them
            try:
                self.client().get_updates(offset, limit=1)
            except TelegramError:
                pass

    def handle_all(self, updates: list[dict]) -> None:
        with Storage(self.config.db_path) as storage:
            for update in updates:
                try:
                    self.handle(update, storage)
                except Exception:   # one bad update must not stop the others
                    log.exception("Could not handle update %s", update.get("update_id"))

    def handle(self, update: dict, storage: Storage) -> None:
        if "chat_member" in update:
            record_member_update(update, storage, self.config, self.chats)
        elif not self.config.alerts.enabled:
            return
        elif "callback_query" in update:
            self.on_button(update["callback_query"], storage)
        elif "message" in update:
            self.on_message(update["message"], storage)

    # --- what people send ----------------------------------------------------------------

    def on_message(self, message: dict, storage: Storage) -> None:
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return   # the bot only talks in private chats
        chat_id = chat["id"]
        command, _, argument = (message.get("text") or "").strip().partition(" ")
        command = command.split("@")[0].lower()
        client = self.client(chat_id)
        if command == "/start":
            city = self.city_of.get(argument.strip().removeprefix("alert_").upper())
            if city:
                client.send_message(*self.price_menu(city))
            else:
                client.send_message(*self.city_menu(TEXTS["welcome"].format(brand=html.escape(self.config.brand))))
        elif command == "/alerts":
            client.send_message(*self.alert_list(chat_id, storage))
        elif command == "/stop":
            storage.delete_alerts(chat_id)
            client.send_message(TEXTS["stopped"])
        else:
            client.send_message(*self.city_menu(TEXTS["help"]))

    def on_button(self, query: dict, storage: Storage) -> None:
        """A button under one of the bot's messages: the message changes in place."""
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        action, *parts = (query.get("data") or "").split("|")
        client = self.client(chat_id)
        note = None
        if action == "s":                                            # the cities (or the countries)
            client.edit_message(message_id, *self.city_menu(TEXTS["choose"]))
        elif action == "k" and parts and parts[0] in self.countries:  # a country: its cities
            cities = self.countries[parts[0]]
            menu = self.price_menu(cities[0]) if len(cities) == 1 else self.city_menu(TEXTS["choose"], parts[0])
            client.edit_message(message_id, *menu)
        elif action == "c" and parts and parts[0] in self.cities:    # a city: its prices
            client.edit_message(message_id, *self.price_menu(parts[0]))
        elif action == "p" and len(parts) == 2 and parts[0] in self.cities and parts[1].isdigit():
            city, price = parts[0], int(parts[1])                    # a price: save the alert
            mine = storage.alerts_of(chat_id)
            if len(mine) >= self.config.alerts.max_per_person and city not in [alert.city for alert in mine]:
                note = TEXTS["too_many"].format(max=self.config.alerts.max_per_person)
                client.edit_message(message_id, *self.alert_list(chat_id, storage))
            else:
                storage.set_alert(chat_id, city, float(price) if price else None, self.clock())
                client.edit_message(message_id, self.saved_text(city, price or None, storage),
                                    [[(BUTTONS["add"], "s"), (BUTTONS["mine"], "l")]])
                note = "✅"
        elif action == "l":                                          # their alerts
            client.edit_message(message_id, *self.alert_list(chat_id, storage))
        elif action == "d" and parts:                                # delete one
            storage.delete_alert(chat_id, parts[0])
            note = TEXTS["deleted"].format(city=parts[0])
            client.edit_message(message_id, *self.alert_list(chat_id, storage))
        client.answer_button(query["id"], note)

    # --- the bot's messages ----------------------------------------------------------------

    def grouped(self) -> bool:
        """Too many cities for one message: pick a country first."""
        return len(self.cities) > CITIES_PER_MENU

    def city_menu(self, intro: str, country: str | None = None) -> tuple[str, Buttons]:
        """A message with a button per city, and one for the person's alerts.

        With many cities: a button per country ("🇮🇹 Italy"), or, for `country`, its cities.
        """
        if self.grouped() and country is None:
            buttons = [(f"{self.flags[code]} {COUNTRY_NAMES.get(code, code.upper())}", f"k|{code}")
                       for code in self.countries]
        else:
            cities = self.countries[country] if country else self.cities
            buttons = [(city, f"c|{city}") for city in cities]
        rows = [buttons[i:i + CITIES_PER_ROW] for i in range(0, len(buttons), CITIES_PER_ROW)]
        if country:
            rows.append([(BUTTONS["countries"], "s")])
        rows.append([(BUTTONS["mine"], "l")])
        return intro, rows

    def price_menu(self, city: str) -> tuple[str, Buttons]:
        """The price buttons for a city ("€30" = up to €30), "any deal", and back to the cities."""
        prices = [(f"€{price}", f"p|{city}|{price}") for price in self.config.alerts.prices]
        rows = [prices[i:i + 3] for i in range(0, len(prices), 3)]
        rows.append([(BUTTONS["any"], f"p|{city}|0")])
        country = self.country_of[city]
        if self.grouped() and len(self.countries[country]) > 1:
            rows.append([(BUTTONS["back"], f"k|{country}")])   # back to that country's cities
        else:
            rows.append([(BUTTONS["countries"] if self.grouped() else BUTTONS["back"], "s")])
        return TEXTS["pick_price"].format(city=html.escape(city)), rows

    def saved_text(self, city: str, price: int | None, storage: Storage) -> str:
        """The confirmation: what was saved, the city's cheapest flight now, the channel."""
        if price is None:
            text = TEXTS["saved_deal"].format(city=html.escape(city))
        else:
            text = TEXTS["saved_price"].format(city=html.escape(city), price=price)
        offers = [offer for offer in build_offers(self.config, storage, self.clock()) if offer.route.city == city]
        if offers:
            best = min(offers, key=lambda offer: offer.best.price)
            dates = format_dates([quote.depart_date for quote in best.quotes], WORDS[self.config.language]["months"])
            text += TEXTS["now"].format(price=format_price(best.best.price), dates=dates)
        link = join_link(self.config, "bot")
        if link:
            text += TEXTS["channel"].format(link=html.escape(link, quote=True))
        return text

    def alert_list(self, chat_id: int, storage: Storage) -> tuple[str, Buttons]:
        """The person's alerts, a delete button for each; the cities when there are none."""
        alerts = storage.alerts_of(chat_id)
        if not alerts:
            return self.city_menu(TEXTS["none"])
        lines = [TEXTS["list"], ""] + [describe(alert) for alert in alerts]
        deletes = [(BUTTONS["delete"].format(city=alert.city), f"d|{alert.city}") for alert in alerts]
        rows = [deletes[i:i + 2] for i in range(0, len(deletes), 2)]
        rows.append([(BUTTONS["add"], "s")])
        return "\n".join(lines), rows


def describe(alert: Alert) -> str:
    city = html.escape(alert.city)
    if alert.max_price is None:
        return TEXTS["line_deal"].format(city=city)
    return TEXTS["line_price"].format(city=city, price=f"{alert.max_price:g}")


def main() -> int:
    """Run the listener in the foreground (a local test). Stop it with Ctrl+C."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)   # its debug lines would show the bot token
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    Bot(config, token, channel_chats(config, os.environ)).listen(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
