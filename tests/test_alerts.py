"""Sending the price alerts: who gets which price, the Premium head start, once per band, quiet hours."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.alerts import send_alerts
from src.config import load_config
from src.links import LinkBuilder
from src.main import run as run_scan
from src.config import Secrets
from src.storage import Storage
from tests.conftest import NOW, FakeResponse, FakeSession
from tests.test_main import fake_world, telegram_calls
from tests.test_website import two_routes

FREE_PERSON, PREMIUM_PERSON = 111, 222
PREMIUM_CHAT = "-1009876"
NOV = date(2026, 11, 1)


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", request_delay_seconds=0,
                   premium=replace(base.premium, enabled=False, free_delay_hours=6),
                   social=replace(base.social, enabled=False), invite_links={"bot": "https://t.me/+botlink"},
                   alerts=replace(base.alerts, enabled=True, bot_username="FlyFromTiranaBot"))


def with_premium(config):
    return replace(config, premium=replace(config.premium, enabled=True))


def telegram(blocked=()):
    """Fake Telegram: 222 is in the Premium channel, people in `blocked` blocked the bot."""
    def handler(method, url, kwargs):
        body = kwargs.get("json") or kwargs.get("data") or {}
        if url.endswith("/getChatMember"):
            status = "member" if body["user_id"] == PREMIUM_PERSON else "left"
            return FakeResponse(200, {"ok": True, "result": {"status": status}})
        if str(body.get("chat_id")) in {str(chat) for chat in blocked}:
            return FakeResponse(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"})
        return FakeResponse(200, {"ok": True, "result": {"message_id": 7}})
    return FakeSession(handler)


def sent_to(session) -> dict[str, str]:
    """{chat: text} of the alerts sent (each goes as a photo with the text under it)."""
    return {c["data"]["chat_id"]: c["data"]["caption"] for c in session.calls if c["url"].endswith("/sendPhoto")}


def alert(config, session=None, *, now=NOW, quiet=False, **options):
    session = session or telegram()
    with Storage(config.db_path) as storage:
        send_alerts(config, storage, token="123:bot", premium_chat=PREMIUM_CHAT, links=LinkBuilder(config.links, "12345"),
                    now=now, quiet=quiet, session=session, **options)
    return session


def set_alerts(config, *alerts):
    with Storage(config.db_path) as storage:
        for chat_id, city, price in alerts:
            storage.set_alert(chat_id, city, price, NOW)


@pytest.fixture
def prices(config):
    """Milan (BGY): usually €60, now €19 (a deal). Vienna: usually €50, now €45 (not a deal)."""
    with Storage(config.db_path) as storage:
        two_routes(storage)


def test_an_alert_with_a_price_gets_the_citys_flight_at_that_price_or_less(config, prices):
    set_alerts(config, (FREE_PERSON, "Milan", 30), (PREMIUM_PERSON, "Vienna", 40), (333, "Vienna", 50))
    sent = sent_to(alert(config))
    assert set(sent) == {str(FREE_PERSON), "333"}                    # Vienna's €45 is over 40
    text = sent[str(FREE_PERSON)]
    assert text.startswith("🔔 <b>Milan deri në €30!</b>")
    assert "💰 <b>nga €19</b> one way · zakonisht ~€60" in text
    assert "marker=12345.alerts" in text                             # the alerts' own SubID
    assert '<a href="https://t.me/+botlink">' in text


def test_any_deal_means_a_deal_by_the_free_channels_rules(config, prices):
    set_alerts(config, (FREE_PERSON, "Milan", None), (333, "Vienna", None))
    sent = sent_to(alert(config))
    assert list(sent) == [str(FREE_PERSON)]                          # Vienna's €45 is no deal
    assert sent[str(FREE_PERSON)].startswith("🔔 <b>Ofertë për Milan!</b>")


def test_once_per_price_band_until_it_gets_cheaper(config, prices):
    set_alerts(config, (FREE_PERSON, "Milan", 30))
    assert sent_to(alert(config))
    assert sent_to(alert(config, now=NOW + timedelta(hours=3))) == {}      # the same band: not again
    with Storage(config.db_path) as storage:                               # then €9: news again
        from tests.test_website import latest_scan
        latest_scan(storage, "BGY", {NOV + timedelta(days=20): 9}, fetched_at=NOW + timedelta(hours=6))
    assert sent_to(alert(config, now=NOW + timedelta(hours=6)))


def test_free_alerts_wait_for_premiums_head_start_and_premium_members_dont(config, prices):
    config = with_premium(config)
    set_alerts(config, (FREE_PERSON, "Milan", 30), (PREMIUM_PERSON, "Milan", 30))
    sent = sent_to(alert(config))
    assert list(sent) == [str(PREMIUM_PERSON)]       # €19 is a deal Premium hasn't had for 6 hours yet
    assert "nga €19" in sent[str(PREMIUM_PERSON)]


def test_quick_premium_scan_only_alerts_premium_members_from_its_own_prices(config, prices):
    config = with_premium(config)
    set_alerts(config, (FREE_PERSON, "Vienna", 50), (PREMIUM_PERSON, "Vienna", 50))
    from tests.conftest import make_quote
    fresh = [make_quote(29, NOV + timedelta(days=3), destination="VIE", fetched_at=NOW)]   # not saved anywhere
    sent = sent_to(alert(config, premium_only=True, scanned=({"VIE": fresh}, {"VIE": []})))
    assert list(sent) == [str(PREMIUM_PERSON)] and "nga €29" in sent[str(PREMIUM_PERSON)]


def test_nothing_in_quiet_hours_and_a_dry_run_only_shows(config, prices):
    set_alerts(config, (FREE_PERSON, "Milan", 30))
    assert sent_to(alert(config, quiet=True)) == {}
    shown = []
    session = alert(config, quiet=True, dry_run=True, output=shown.append)
    assert session.calls == [] and "DRY RUN [alert to 111]" in shown[0]
    assert sent_to(alert(config))                    # nothing was remembered: it goes out later


def test_someone_who_blocked_the_bot_loses_their_alerts(config, prices):
    set_alerts(config, (FREE_PERSON, "Milan", 30), (FREE_PERSON, "Vienna", 50), (333, "Milan", 30))
    alert(config, telegram(blocked=[FREE_PERSON]))
    with Storage(config.db_path) as storage:
        assert [a.chat_id for a in storage.all_alerts()] == [333]


def test_the_scan_sends_alerts_and_its_posts_link_to_the_bot(config):
    set_alerts(config, (FREE_PERSON, "Milan", 30))
    session = fake_world()
    secrets = Secrets("tp-token", "12345", "123:bot", "@flyfromtirana")
    assert run_scan(config, secrets, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    texts = {(c.get("data") or c.get("json"))["chat_id"]: (c.get("data") or c.get("json")).get("caption")
             for c in telegram_calls(session) if c["url"].endswith("/sendPhoto")}
    assert '🎯 <a href="https://t.me/FlyFromTiranaBot?start=alert_BGY">Më njofto kur bie çmimi për Milan</a>' \
        in texts["@flyfromtirana"]
    assert texts[str(FREE_PERSON)].startswith("🔔 <b>Milan deri në €30!</b>")


def test_alerts_off_sends_nothing(config, prices):
    set_alerts(config, (FREE_PERSON, "Milan", 30))
    off = replace(config, alerts=replace(config.alerts, enabled=False))
    assert alert(off).calls == []
