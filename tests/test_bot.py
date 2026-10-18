"""The bot's private chat: picking a city and a price, the list of alerts, /stop, and the joins it notes."""
import threading
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.bot import BUTTONS, Bot, alert_link
from src.config import load_config
from src.growth import FREE
from src.storage import Alert, Storage
from tests.conftest import NOW, FakeResponse, FakeSession
from tests.test_website import latest_scan, seed_history

PERSON = 111
CHATS = {FREE: "@flyfromtirana"}


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", premium=replace(base.premium, enabled=False),
                   invite_links={"bot": "https://t.me/+botlink"},
                   alerts=replace(base.alerts, enabled=True, bot_username="FlyFromTiranaBot", max_per_person=2))


def telegram():
    """A fake Telegram that says yes to everything and remembers what it was asked."""
    return FakeSession(lambda method, url, kwargs: FakeResponse(200, {"ok": True, "result": {"message_id": 7}}))


def calls(session, method):
    return [c["json"] for c in session.calls if c["url"].endswith("/" + method)]


def message(text, *, chat_type="private", update_id=1):
    return {"update_id": update_id, "message": {"message_id": 5, "text": text,
                                                "chat": {"id": PERSON, "type": chat_type}}}


def button(data, update_id=2):
    return {"update_id": update_id, "callback_query": {"id": "q1", "data": data,
                                                       "message": {"message_id": 7, "chat": {"id": PERSON}}}}


def labels(payload):
    """The buttons under a message, as rows of labels."""
    return [[b["text"] for b in row] for row in payload["reply_markup"]["inline_keyboard"]]


def actions(payload):
    return [b.get("callback_data") or b.get("url") for row in payload["reply_markup"]["inline_keyboard"] for b in row]


def run_bot(config, *updates):
    session = telegram()
    Bot(config, "123:bot", CHATS, session=session, clock=lambda: NOW).handle_all(list(updates))
    return session


def test_start_shows_a_button_per_city(config):
    session = run_bot(config, message("/start"))
    sent, = calls(session, "sendMessage")
    assert sent["chat_id"] == str(PERSON) and "Përshëndetje" in sent["text"]
    cities = list(dict.fromkeys(route.city for route in config.routes))
    assert actions(sent)[:len(cities)] == [f"c|{city}" for city in cities]
    assert labels(sent)[-1] == [BUTTONS["mine"]]
    assert all(len(row) <= 3 for row in labels(sent))


def test_link_under_a_post_opens_that_citys_prices(config):
    assert alert_link(config, "BGY") == "https://t.me/FlyFromTiranaBot?start=alert_BGY"
    sent, = calls(run_bot(config, message("/start alert_BGY")), "sendMessage")
    assert "Tirana → Milan" in sent["text"]
    assert actions(sent) == ["p|Milan|20", "p|Milan|30", "p|Milan|40", "p|Milan|50", "p|Milan|70", "p|Milan|100",
                             "p|Milan|0", "s"]


def test_a_price_saves_the_alert_and_shows_the_cheapest_flight_now(config):
    with Storage(config.db_path) as storage:
        seed_history(storage, [60] * 12, destination="BGY", days_ago=2)
        latest_scan(storage, "BGY", {date(2026, 11, 20): 19})
    session = run_bot(config, button("c|Milan"), button("p|Milan|30", update_id=3))
    menu, saved = calls(session, "editMessageText")
    assert "Më njofto kur bileta" in menu["text"]
    assert "deri në <b>€30</b>" in saved["text"]
    assert "Tani më e lira: <b>€19</b> (20 Nën)" in saved["text"]
    assert '<a href="https://t.me/+botlink">' in saved["text"]                 # the channel, through the bot's link
    assert [c["text"] for c in calls(session, "answerCallbackQuery") if "text" in c] == ["✅"]
    with Storage(config.db_path) as storage:
        assert storage.alerts_of(PERSON) == [Alert(PERSON, "Milan", 30.0)]


def test_any_deal_and_a_new_price_for_the_same_city_replaces_the_old(config):
    run_bot(config, button("p|Milan|30"), button("p|Milan|0", update_id=3))
    with Storage(config.db_path) as storage:
        assert storage.alerts_of(PERSON) == [Alert(PERSON, "Milan", None)]


def test_the_list_deletes_one_and_stop_deletes_all(config):
    run_bot(config, button("p|Milan|30"), button("p|Rome|0", update_id=3))
    session = run_bot(config, message("/alerts"))
    listed, = calls(session, "sendMessage")
    assert "• Milan: deri në €30" in listed["text"] and "• Rome: çdo ofertë" in listed["text"]
    assert "d|Milan" in actions(listed)

    run_bot(config, button("d|Milan"))
    with Storage(config.db_path) as storage:
        assert [alert.city for alert in storage.alerts_of(PERSON)] == ["Rome"]
    session = run_bot(config, message("/stop"))
    assert "U fshinë" in calls(session, "sendMessage")[0]["text"]
    with Storage(config.db_path) as storage:
        assert storage.alerts_of(PERSON) == []


def test_a_person_has_at_most_max_per_person_alerts(config):
    session = run_bot(config, button("p|Milan|30"), button("p|Rome|30", update_id=3),
                      button("p|Vienna|30", update_id=4), button("p|Rome|50", update_id=5))
    with Storage(config.db_path) as storage:
        assert [(a.city, a.max_price) for a in storage.alerts_of(PERSON)] == [("Milan", 30.0), ("Rome", 50.0)]
    notes = [c.get("text") for c in calls(session, "answerCallbackQuery")]
    assert notes[2].startswith("Ke 2 njoftime")                        # Vienna: one too many
    assert notes[3] == "✅"                                             # changing Rome is fine


def test_groups_and_unknown_buttons_are_ignored(config):
    session = run_bot(config, message("/start", chat_type="group"), button("p|Atlantis|30"))
    assert calls(session, "sendMessage") == [] and calls(session, "editMessageText") == []
    with Storage(config.db_path) as storage:
        assert storage.alerts_of(PERSON) == []


def test_joins_are_noted_even_with_alerts_off(config):
    off = replace(config, alerts=replace(config.alerts, enabled=False))
    join = {"update_id": 9, "chat_member": {"chat": {"id": -1001, "username": "flyfromtirana"}, "date": int(NOW.timestamp()),
                                            "old_chat_member": {"status": "left"}, "new_chat_member": {"status": "member"},
                                            "invite_link": {"invite_link": "https://t.me/+botlink", "name": "bot"}}}
    session = run_bot(off, message("/start", update_id=8), join)
    assert calls(session, "sendMessage") == []
    with Storage(config.db_path) as storage:
        assert storage.joins_by_source(FREE, NOW - timedelta(days=1)) == {"bot": 1}


def test_listen_reads_until_stopped_and_confirms_the_last_updates(config):
    stop = threading.Event()
    batches = [[message("/start", update_id=41)], []]

    def handler(method, url, kwargs):
        if url.endswith("/getUpdates"):
            if not batches:
                return FakeResponse(200, {"ok": True, "result": []})
            batch = batches.pop(0)
            if not batch:
                stop.set()
            return FakeResponse(200, {"ok": True, "result": batch})
        return FakeResponse(200, {"ok": True, "result": {"message_id": 7}})
    session = FakeSession(handler)
    Bot(config, "123:bot", CHATS, session=session, clock=lambda: NOW).listen(stop)

    polls = calls(session, "getUpdates")
    assert polls[0]["allowed_updates"] == ["message", "callback_query", "chat_member"] and polls[0]["timeout"] == 50
    assert [p.get("offset") for p in polls] == [None, 42, 42]      # the last call confirms update 41
    assert calls(session, "setMyCommands")[0]["commands"][0]["command"] == "start"
    assert len(calls(session, "sendMessage")) == 1
