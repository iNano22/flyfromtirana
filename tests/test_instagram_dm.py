"""The Instagram message bot: the webhook's checks, who gets an answer, what Claude is given, the replies."""
import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src import instagram_dm
from src.config import load_config
from src.instagram_dm import (TEXTS, DMBot, WebhookServer, Writer, look_up_prices, price_table, signature_ok,
                              split_message)
from src.meta import GRAPH_URL, MetaClient, MetaError
from src.storage import Storage
from tests.conftest import NOW, FakeResponse, FakeSession
from tests.test_website import latest_scan

PERSON = "IGSID-1"
OUR_ACCOUNT = "17841417241858036"
SECRET = "app-secret"


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", premium=replace(base.premium, enabled=False),
                   instagram_dm=replace(base.instagram_dm, enabled=True, max_replies_per_day=3,
                                        owner_pause_hours=12))


class FakeMeta:
    """Stands in for MetaClient: remembers what the bot sent."""

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send_instagram_message(self, recipient, text):
        if self.fail:
            raise MetaError("Meta refused the call")
        self.sent.append((recipient, text))
        return f"bot-mid-{len(self.sent)}"


class FakeWriter:
    """Stands in for Writer: answers every chat with the same text (None = Claude failed)."""

    def __init__(self, answer="Milano nga €19 më 20 Nën 👉 https://flyfromtirana.al/milan/"):
        self.answer = answer
        self.chats = []

    def reply(self, rules, prices, chat, lookup):
        self.chats.append({"rules": rules, "prices": prices, "chat": list(chat), "lookup": lookup})
        return self.answer


def text_event(text, mid="m1", person=PERSON):
    return {"sender": {"id": person}, "recipient": {"id": OUR_ACCOUNT}, "timestamp": 1,
            "message": {"mid": mid, "text": text}}


def echo_event(text, mid, person=PERSON):
    """A message our own account sent to `person` (the bot's, or the owner's from the app)."""
    return {"sender": {"id": OUR_ACCOUNT}, "recipient": {"id": person}, "timestamp": 2,
            "message": {"mid": mid, "text": text, "is_echo": True}}


def delivery(*events):
    return {"object": "instagram", "entry": [{"id": OUR_ACCOUNT, "time": 1, "messaging": list(events)}]}


def make_bot(config, meta=None, writer=None, clock=lambda: NOW):
    return DMBot(config, meta or FakeMeta(), writer or FakeWriter(), clock=clock)


# --- who gets an answer ----------------------------------------------------------------

def test_a_question_gets_claudes_reply(config):
    meta, writer = FakeMeta(), FakeWriter()
    make_bot(config, meta, writer).handle_payload(delivery(text_event("Pershendetje per ne Londor sa kushtojn")))

    assert meta.sent == [(PERSON, writer.answer)]
    assert writer.chats[0]["chat"] == [("user", "Pershendetje per ne Londor sa kushtojn")]
    assert "Albanian" in writer.chats[0]["rules"] and "never offer to book" in writer.chats[0]["rules"].lower()


def test_the_sample_from_metas_test_button_is_read_too(config):
    meta = FakeMeta()
    sample = {"object": "instagram", "entry": [{"id": "0", "time": 1, "changes": [{"field": "messages", "value": {
        "sender": {"id": "12334"}, "recipient": {"id": "23245"}, "timestamp": "1527459824",
        "message": {"mid": "random_mid", "text": "random_text"}}}]}]}
    make_bot(config, meta).handle_payload(sample)

    assert meta.sent == [("12334", FakeWriter().answer)]


def test_the_same_message_twice_is_answered_once(config):
    meta = FakeMeta()
    bot = make_bot(config, meta)
    bot.handle_payload(delivery(text_event("Sa kushton Milano?")))
    bot.handle_payload(delivery(text_event("Sa kushton Milano?")))   # Meta retried the delivery

    assert len(meta.sent) == 1


def test_the_chat_so_far_goes_to_claude(config):
    writer = FakeWriter()
    bot = make_bot(config, writer=writer)
    bot.handle_payload(delivery(text_event("Sa kushton Athina?", mid="m1")))
    bot.handle_payload(delivery(text_event("Po me 2 nëntor?", mid="m2")))

    assert writer.chats[1]["chat"] == [("user", "Sa kushton Athina?"), ("assistant", writer.answer),
                                       ("user", "Po me 2 nëntor?")]


def test_the_bots_own_echo_changes_nothing(config):
    meta = FakeMeta()
    bot = make_bot(config, meta)
    bot.handle_payload(delivery(text_event("Sa kushton Milano?", mid="m1")))
    bot.handle_payload(delivery(echo_event(FakeWriter().answer, mid="bot-mid-1")))   # the reply's echo
    bot.handle_payload(delivery(text_event("Faleminderit!", mid="m2")))

    assert len(meta.sent) == 2


def test_after_the_owner_answers_by_hand_the_bot_keeps_quiet(config):
    meta = FakeMeta()
    clock = {"now": NOW}
    bot = make_bot(config, meta, clock=lambda: clock["now"])
    bot.handle_payload(delivery(echo_event("Përshëndetje, jam Donaldi", mid="owner-1")))
    bot.handle_payload(delivery(text_event("Sa kushton Milano?", mid="m1")))
    assert meta.sent == []

    clock["now"] = NOW + timedelta(hours=13)   # past owner_pause_hours
    bot.handle_payload(delivery(text_event("Sa kushton Milano?", mid="m2")))
    assert len(meta.sent) == 1


def test_a_person_gets_at_most_max_replies_per_day(config):
    meta = FakeMeta()
    bot = make_bot(config, meta)
    for number in range(5):
        bot.handle_payload(delivery(text_event(f"Pyetja {number}", mid=f"m{number}")))

    assert len(meta.sent) == 3   # max_replies_per_day in the fixture


def test_a_photo_gets_the_short_text_and_claude_is_not_asked(config):
    meta, writer = FakeMeta(), FakeWriter()
    event = {"sender": {"id": PERSON}, "message": {"mid": "m1", "attachments": [{"type": "image"}]}}
    make_bot(config, meta, writer).handle_payload(delivery(event))

    assert meta.sent == [(PERSON, TEXTS["not_text"])]
    assert writer.chats == []


def test_story_mentions_reactions_and_other_objects_get_nothing(config):
    meta = FakeMeta()
    mention = {"sender": {"id": PERSON}, "message": {"mid": "m1", "attachments": [{"type": "story_mention"}]}}
    reaction = {"sender": {"id": PERSON}, "reaction": {"mid": "m1", "action": "react", "emoji": "❤️"}}
    bot = make_bot(config, meta)
    bot.handle_payload(delivery(mention, reaction))
    bot.handle_payload({"object": "page", "entry": [{"messaging": [text_event("hello", mid="m9")]}]})

    assert meta.sent == []


def test_when_claude_fails_the_person_gets_the_website(config):
    meta = FakeMeta()
    make_bot(config, meta, FakeWriter(answer=None)).handle_payload(delivery(text_event("Sa kushton Milano?")))

    assert meta.sent == [(PERSON, TEXTS["error"].format(site="https://flyfromtirana.al/"))]


def test_a_reply_meta_refuses_is_logged_not_raised(config):
    make_bot(config, FakeMeta(fail=True)).handle_payload(delivery(text_event("Sa kushton Milano?")))


def test_old_messages_are_forgotten(config):
    clock = {"now": NOW}
    bot = make_bot(config, clock=lambda: clock["now"])
    bot.handle_payload(delivery(text_event("Sa kushton Milano?", mid="m1")))
    clock["now"] = NOW + timedelta(days=31)
    bot.handle_payload(delivery(text_event("Po Roma?", mid="m2")))

    with Storage(config.db_path) as storage:
        assert storage.dm_history(PERSON, NOW - timedelta(days=60), 10) == [
            ("user", "Po Roma?"), ("assistant", FakeWriter().answer)]


# --- what Claude is given ---------------------------------------------------------------

def test_the_price_table_has_the_city_its_price_and_its_page(config):
    with Storage(config.db_path) as storage:
        latest_scan(storage, "BGY", {date(2026, 11, 20): 19, date(2026, 11, 22): 20},
                    back={date(2026, 11, 27): 24})
        table = price_table(config, storage, NOW)

    line = next(line for line in table.splitlines() if line.startswith("BGY |"))
    assert "Milan" in line and "Bergamo airport" in line
    assert "€19 on 20 Nov, 22 Nov" in line and "from €24 on 27 Nov, after the 20 Nov flight out" in line
    assert line.endswith("https://flyfromtirana.al/milan/")
    assert "Today is Friday 9 Oct 2026." in table
    assert "no fresh price" in table and "Athens" in table   # a city without prices is still named


def test_the_lookup_gives_every_day_both_ways(config):
    with Storage(config.db_path) as storage:
        latest_scan(storage, "ATH", {date(2026, 11, 2): 35, date(2026, 11, 3): 30, date(2026, 11, 20): 15},
                    back={date(2026, 11, 6): 57})
        answer = look_up_prices(config, storage, NOW,
                                {"airport": "ath", "first_date": "2026-11-02", "last_date": "2026-11-06"})

    out, back, trips = answer.splitlines()
    assert out.startswith("TIA → ATH (Athens): 2 Nov 2026: €35") and "3 Nov 2026: €30" in out
    assert "20 Nov" not in out   # outside the dates asked about
    assert back.startswith("ATH → TIA (Athens): 6 Nov 2026: €57")
    assert trips == ("Round trips, cheapest first: out 3 Nov 2026 €30 + back 6 Nov 2026 €57 = €87; "
                     "out 2 Nov 2026 €35 + back 6 Nov 2026 €57 = €92")


def test_a_round_trip_never_comes_back_before_it_leaves(config):
    """The Frankfurt reply that started this: out on 18 Dec, "back" on 14 Dec."""
    with Storage(config.db_path) as storage:
        latest_scan(storage, "HHN", {date(2026, 12, 18): 39}, back={date(2026, 12, 14): 39})
        answer = look_up_prices(config, storage, NOW,
                                {"airport": "HHN", "first_date": "2026-12-01", "last_date": "2026-12-26"})

    assert answer.splitlines()[-1] == "Round trips, cheapest first: none in these days"


def test_the_lookup_says_when_an_airport_isnt_scanned_or_the_dates_are_wrong(config):
    with Storage(config.db_path) as storage:
        assert "We don't scan XXX" in look_up_prices(config, storage, NOW, {"airport": "XXX", "first_date": "",
                                                                              "last_date": ""})
        assert "YYYY-MM-DD" in look_up_prices(config, storage, NOW, {"airport": "ATH", "first_date": "2 Nov",
                                                                      "last_date": "6 Nov"})


# --- asking Claude ------------------------------------------------------------------------

class FakeAnthropic:
    """Stands in for anthropic.Anthropic: gives the answers in `responses` in order, remembers the requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=lambda block: block.__dict__)))
        return self.responses.pop(0)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def test_the_writer_looks_prices_up_when_claude_asks(config):
    tool_use = SimpleNamespace(type="tool_use", id="tu1", name="flight_prices",
                               input={"airport": "ATH", "first_date": "2026-11-02", "last_date": "2026-11-06"})
    client = FakeAnthropic(SimpleNamespace(stop_reason="tool_use", content=[tool_use]),
                           SimpleNamespace(stop_reason="end_turn", content=[text_block("Athina më 2 Nën: €35")]))
    asked = []
    reply = Writer(config.instagram_dm, client).reply("rules", "prices", [("user", "Athinë 2-6 nëntor?")],
                                                      lambda request: asked.append(request) or "TIA → ATH: €35")

    assert reply == "Athina më 2 Nën: €35"
    assert asked == [tool_use.input]
    second = client.requests[1]
    assert second["messages"][-1]["content"] == [{"type": "tool_result", "tool_use_id": "tu1",
                                                  "content": "TIA → ATH: €35"}]
    first = client.requests[0]
    assert first["model"] == "claude-haiku-5-5" and first["output_config"] == {"effort": "low"}
    assert "fallbacks" not in first   # Haiku has no server-side fallback
    assert first["system"][1]["cache_control"] == {"type": "ephemeral"}


def test_the_writer_gives_up_when_claude_declines(config):
    client = FakeAnthropic(SimpleNamespace(stop_reason="refusal", content=[], stop_details=None))
    assert Writer(config.instagram_dm, client).reply("r", "p", [("user", "hi")], lambda _: "") is None


def test_a_bigger_model_gets_the_server_side_fallback(config):
    settings = replace(config.instagram_dm, model="claude-sonnet-5-5")
    client = FakeAnthropic(SimpleNamespace(stop_reason="end_turn", content=[text_block("ok")]))
    Writer(settings, client).reply("r", "p", [("user", "hi")], lambda _: "")

    assert client.requests[0]["fallbacks"] == "default"


def test_a_long_reply_is_cut_into_messages_instagram_takes():
    lines = ["Milano nga €19 — çmimet ndryshojnë shpejt ë" * 3] * 20
    parts = split_message("\n".join(lines))

    assert len(parts) > 1
    assert all(len(part.encode()) <= 1000 for part in parts)
    assert "\n".join(parts).replace("\n", "") == "".join(lines)
    assert split_message("Përshëndetje!") == ["Përshëndetje!"]


def test_meta_sends_the_message_to_the_page_messages_endpoint():
    def handler(method, url, kwargs):
        if url.endswith("/me"):
            return FakeResponse(200, {"id": "PAGE1", "name": "x", "instagram_business_account": {"id": OUR_ACCOUNT}})
        return FakeResponse(200, {"recipient_id": PERSON, "message_id": "mid-77"})
    session = FakeSession(handler)

    assert MetaClient("token", session).send_instagram_message(PERSON, "Përshëndetje") == "mid-77"
    call = session.calls[-1]
    assert call["url"] == GRAPH_URL.format(path="PAGE1/messages")
    assert json.loads(call["data"]["recipient"]) == {"id": PERSON}
    assert json.loads(call["data"]["message"]) == {"text": "Përshëndetje"}


# --- the webhook ----------------------------------------------------------------------------

def signed(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_signature_ok():
    body = b'{"object": "instagram"}'
    assert signature_ok(body, signed(body), SECRET)
    assert not signature_ok(body, signed(b"something else"), SECRET)
    assert not signature_ok(body, "", SECRET)


@pytest.fixture
def server():
    server = WebhookServer(("127.0.0.1", 0), "verify-me", SECRET)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, path, body: bytes | None = None, signature: str = ""):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    request = urllib.request.Request(url, data=body, headers={"X-Hub-Signature-256": signature})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""


def test_meta_checks_the_address_with_the_verify_token(server):
    good = "/meta/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=12345"
    assert call(server, good) == (200, "12345")
    assert call(server, good.replace("verify-me", "wrong"))[0] == 403
    assert call(server, "/somewhere-else")[0] == 404


def test_a_signed_delivery_is_queued_and_an_unsigned_one_refused(server):
    body = json.dumps(delivery(text_event("Sa kushton Milano?"))).encode()

    assert call(server, "/meta/webhook", body, signed(body)) == (200, "EVENT_RECEIVED")
    assert server.inbox.get(timeout=1)["object"] == "instagram"
    assert call(server, "/meta/webhook", body, "sha256=forged")[0] == 403
    assert server.inbox.empty()


def test_the_bot_stays_off_without_its_variables(config):
    assert instagram_dm.start(config, threading.Event(), env={"META_PAGE_TOKEN": "x"}) is None
    off = replace(config, instagram_dm=replace(config.instagram_dm, enabled=False))
    assert instagram_dm.start(off, threading.Event(), env=dict.fromkeys(instagram_dm.REQUIRED_ENV, "x")) is None
