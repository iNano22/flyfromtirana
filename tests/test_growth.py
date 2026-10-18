"""Growing the free channel: the join links, the WhatsApp share link, and the weekly report."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from src import growth
from src.config import ConfigError, GrowthSettings, Route, Secrets, _build_growth, _invite_links, load_config
from src.deals import Deal
from src.growth import (FREE, PREMIUM, join_link, link_source, membership_change, public_url,
                        record_member_update, report_due, report_text, run, short_address, whatsapp_link)
from src.links import LinkBuilder
from src.main import run as run_scan
from src.storage import Storage
from src.website import build_offers, render_site
from tests.conftest import NOW, FakeResponse, FakeSession, make_quote, telegram_chat, telegram_text
from tests.test_main import fake_world, telegram_calls
from tests.test_website import two_routes

LINKS = {"instagram": "https://t.me/+insta", "facebook": "https://t.me/+fb", "website": "https://t.me/+web",
         "whatsapp": "https://t.me/+wa"}
OWNER = "6040871333"
ENV = {"TELEGRAM_BOT_TOKEN": "123:bot", "TELEGRAM_CHANNEL_ID": "@flyfromtirana",
       "TELEGRAM_PREMIUM_CHANNEL_ID": "-1009876", "TELEGRAM_ALERT_CHAT_ID": OWNER}
# NOW is a Friday; this is the Monday after it, 09:17 UTC = 11:17 in Tirana, a full run's time.
MONDAY = datetime(2026, 10, 12, 9, 17, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", request_delay_seconds=0, invite_links=dict(LINKS),
                   premium=replace(base.premium, enabled=True), growth=GrowthSettings(),
                   website=replace(base.website, output_dir=tmp_path / "site"),
                   social=replace(base.social, enabled=False))


def deal() -> Deal:
    """Milan at €19 on two dates, as the free channel would post it."""
    route = Route(iata="BGY", city="Milan", city_en="Milan", flag="🇮🇹", airport="Bergamo")
    quotes = [make_quote(19, date(2026, 11, 20)), make_quote(20, date(2026, 11, 22))]
    return Deal(route=route, quotes=quotes, median=62, reason="median")


# --- the links --------------------------------------------------------------------------

def test_public_url():
    assert public_url("@flyfromtirana") == "https://t.me/flyfromtirana"
    assert public_url("-1001234567890") is None          # private channel id: no public link
    assert public_url("") is None


def test_each_place_has_its_own_link_or_else_the_public_address(config):
    assert join_link(config, "website") == "https://t.me/+web"
    assert join_link(replace(config, invite_links={}), "website") == "https://t.me/flyfromtirana"
    assert short_address(config) == "t.me/flyfromtirana"


def test_whatsapp_link_opens_a_message_about_the_deal_with_the_channels_link(config):
    link = whatsapp_link(deal(), config, "sq")
    assert link.startswith("https://wa.me/?text=")
    text = parse_qs(urlsplit(link).query)["text"][0]
    assert text == ("✈️ Tirana → Milan nga €19 one way · 20 Nën, 22 Nën\n"
                    "Oferta si kjo çdo ditë, falas, në Telegram: https://t.me/+wa")
    # In another language, with the city's name in it.
    text = parse_qs(urlsplit(whatsapp_link(deal(), config, "it", city="Milano")).query)["text"][0]
    assert text.startswith("✈️ Tirana → Milano da €19 solo andata · 20 nov, 22 nov\nOfferte come questa")


def test_invite_links_are_checked():
    assert _invite_links({"website": " https://t.me/+web ", "facebook": ""}) == {"website": "https://t.me/+web"}
    with pytest.raises(ValueError, match="only knows"):
        _invite_links({"tiktok": "https://t.me/+x"})
    with pytest.raises(ValueError, match="must be a Telegram link"):
        _invite_links({"website": "https://example.com/"})


def test_growth_settings():
    assert _build_growth({}) == GrowthSettings(report_day=0)
    assert _build_growth({"report_day": "", "report_time": "18:30"}).report_day is None
    with pytest.raises(ValueError, match="quotes"):
        _build_growth({"report_time": 930})


# --- where the links go -----------------------------------------------------------------

def test_free_channel_posts_can_be_sent_on_whatsapp_and_premium_posts_cannot(config):
    session = fake_world()
    secrets = Secrets("tp-token", "12345", "123:bot", "@flyfromtirana", telegram_premium_channel_id="-1009876")
    config = replace(config, premium=replace(config.premium, free_delay_hours=0))   # both channels post now
    assert run_scan(config, secrets, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    posts = {telegram_chat(call): telegram_text(call) for call in telegram_calls(session)}
    assert '💬 <a href="https://wa.me/?text=' in posts["@flyfromtirana"]
    assert "t.me%2F%2Bwa" in posts["@flyfromtirana"]          # the WhatsApp invite link, inside the message
    assert ">Dërgoja një shoku në WhatsApp</a>" in posts["@flyfromtirana"]
    assert "wa.me" not in posts["-1009876"]


def test_website_buttons_use_the_website_link_and_deals_can_be_sent(config, storage):
    two_routes(storage)   # BGY is a deal, VIE isn't
    page = render_site(build_offers(replace(config, premium=replace(config.premium, enabled=False)), storage, NOW),
                       config, LinkBuilder(config.links, "1"), updated_at=NOW, now=NOW)
    assert 'class="nav-tg" href="https://t.me/+web"' in page
    assert "https://t.me/flyfromtirana" not in page
    assert page.count('class="dshare" href="https://wa.me/?text=') == 1    # the deal's photo card only
    assert page.count('class="share" href="https://wa.me/?text=') == 1     # and its row in the table
    assert "Tirana%20%E2%86%92%20Milan%20nga%20%E2%82%AC19" in page        # "Tirana → Milan nga €19"


def test_city_page_deal_can_be_sent_on_whatsapp(config, storage):
    from tests.test_guides import milan_page
    two_routes(storage)
    page = milan_page(replace(config, premium=replace(config.premium, enabled=False)), storage)
    assert page.count('class="fare-share" href="https://wa.me/?text=') == 1   # Bergamo's €19
    assert 'class="nav-tg" href="https://t.me/+web"' in page


def test_instagram_says_where_to_find_the_channel_and_facebook_links_to_it(config):
    from src.social import facebook_channel, instagram_channel
    assert instagram_channel(config) == "t.me/flyfromtirana (linku në bio)"   # "@flyfromtirana" is the Instagram account
    assert facebook_channel(config) == "https://t.me/+fb"
    assert facebook_channel(replace(config, invite_links={})) == "https://t.me/flyfromtirana"


# --- noting who joins ---------------------------------------------------------------------

def update(update_id, *, chat=None, old="left", new="member", invite=None, at=NOW):
    change = {"chat": chat or {"id": -100111, "username": "flyfromtirana", "type": "channel"},
              "date": int(at.timestamp()), "old_chat_member": {"status": old}, "new_chat_member": {"status": new}}
    if invite:
        change["invite_link"] = invite
    return {"update_id": update_id, "chat_member": change}


def test_joins_and_leaves():
    assert membership_change({"old_chat_member": {"status": "left"}, "new_chat_member": {"status": "member"}}) is True
    assert membership_change({"old_chat_member": {"status": "member"}, "new_chat_member": {"status": "kicked"}}) is False
    # An admin's rights changed: no join, no leave.
    assert membership_change({"old_chat_member": {"status": "member"},
                              "new_chat_member": {"status": "administrator"}}) is None


def test_link_source(config):
    assert link_source(None, config) == ""
    assert link_source({"invite_link": "https://t.me/+fb", "name": "facebook"}, config) == "facebook"
    assert link_source({"invite_link": "https://t.me/+web"}, config) == "website"   # no name: by the link
    assert link_source({"invite_link": "https://t.me/+Ab…", "name": "posters"}, config) == "posters"


def telegram_world(updates, counts=None):
    """Fake Telegram: the updates (then nothing), member counts per chat, and any message."""
    pending = list(updates)
    counts = counts or {"@flyfromtirana": 42, "-1009876": 3}

    def handler(method, url, kwargs):
        body = kwargs.get("json") or {}
        if url.endswith("/getUpdates"):
            offset = body.get("offset")
            batch = [u for u in pending if offset is None or u["update_id"] >= offset]
            return FakeResponse(200, {"ok": True, "result": batch})
        if url.endswith("/getChatMemberCount"):
            return FakeResponse(200, {"ok": True, "result": counts[body["chat_id"]]})
        return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})
    return FakeSession(handler)


def calls_to(session, method):
    return [c for c in session.calls if c["url"].endswith("/" + method)]


def test_joins_are_noted_per_link_and_other_chats_ignored(config, storage):
    chats = {FREE: "@flyfromtirana", PREMIUM: "-1009876"}
    updates = [
        update(5, invite={"invite_link": "https://t.me/+insta", "name": "instagram"}),
        update(6),                                                     # the public address
        update(7, old="member", new="left"),
        update(8, chat={"id": -100999, "username": "someone_else"}),   # another chat
        update(9, old="member", new="administrator"),                  # an admin's rights: not a join
        update(10, chat={"id": -1009876, "type": "channel"}),          # premium
    ]
    noted = [record_member_update(u, storage, config, chats) for u in updates]
    assert noted == [True, True, True, False, False, True]
    assert storage.joins_by_source(FREE, NOW - timedelta(days=1)) == {"instagram": 1, "": 1}
    assert storage.count_leaves(FREE, NOW - timedelta(days=1)) == 1
    assert storage.joins_by_source(PREMIUM, NOW - timedelta(days=1)) == {"": 1}


def test_run_counts_the_members_and_leaves_the_updates_to_the_bot(config):
    session = telegram_world([update(5)])
    assert run(config, ENV, session=session, now=NOW) == 0          # a Friday: no report
    with Storage(config.db_path) as storage:
        assert storage.member_count_since(FREE, NOW - timedelta(days=1)) == (42, NOW)
        assert storage.member_count_since(PREMIUM, NOW - timedelta(days=1)) == (3, NOW)
    assert calls_to(session, "getUpdates") == [] and calls_to(session, "sendMessage") == []


def test_an_update_seen_twice_is_counted_once(config):
    with Storage(config.db_path) as storage:
        for _ in range(2):
            storage.save_join(5, FREE, "website", True, NOW)
        assert storage.joins_by_source(FREE, NOW - timedelta(days=1)) == {"website": 1}


# --- the weekly report --------------------------------------------------------------------

def test_report_is_due_once_on_the_report_day_after_its_time(config, storage):
    tirana = MONDAY.astimezone(growth.ZoneInfo(config.timezone))
    assert report_due(config, storage, tirana) is True
    assert report_due(config, storage, tirana.replace(hour=8)) is False          # before 09:00
    assert report_due(config, storage, tirana - timedelta(days=1)) is False      # Sunday
    storage.record_report(growth.REPORT, MONDAY)
    assert report_due(config, storage, tirana + timedelta(hours=3)) is False     # sent already
    off = replace(config, growth=GrowthSettings(report_day=None))
    assert report_due(off, storage, tirana) is False


def test_report_text(config, storage):
    week_ago = MONDAY - timedelta(days=7)
    storage.save_member_count(FREE, 30, week_ago - timedelta(hours=3))   # before the week: not the start
    storage.save_member_count(FREE, 31, week_ago + timedelta(hours=1))
    storage.save_member_count(PREMIUM, 3, week_ago + timedelta(hours=1))
    for update_id, source in enumerate(["whatsapp", "instagram", "instagram", "", "posters"]):
        storage.save_join(update_id, FREE, source, True, MONDAY - timedelta(days=2))
    storage.save_join(99, FREE, "", False, MONDAY - timedelta(days=1))
    text = report_text(config, storage, MONDAY, {FREE: 42, PREMIUM: 3})
    assert text.startswith("📈 <b>FlyFromTirana: Telegram, java deri më 12 Tet</b>")
    assert "👥 Kanali falas: <b>42</b> anëtarë (+11 që nga 5 Tet)" in text
    assert "💎 Premium: <b>3</b> anëtarë (+0 që nga 5 Tet)" in text
    assert "Të rinj në kanalin falas: <b>5</b>. Nga erdhën:" in text
    lines = text.splitlines()
    sources = lines[lines.index("Të rinj në kanalin falas: <b>5</b>. Nga erdhën:") + 1:]
    assert sources == ["📸 Instagram (linku në bio): 2", "💬 WhatsApp: 1", "🔗 Linku «posters»: 1",
                       "🔗 Pa linkun tonë (adresa publike, kërkimi, postime të përcjella): 1",
                       "🚪 Dolën nga kanali falas: 1"]


def test_a_quiet_week_says_so(config, storage):
    storage.save_member_count(FREE, 2, MONDAY)
    text = report_text(config, storage, MONDAY, {FREE: 2})
    assert "Asnjë anëtar i ri në kanalin falas këtë javë." in text and "Dolën" not in text


def test_monday_run_sends_the_report_to_the_owner_once(config):
    session = telegram_world([])
    assert run(config, ENV, session=session, now=MONDAY) == 0
    report, = calls_to(session, "sendMessage")
    assert report["json"]["chat_id"] == OWNER
    assert "👥 Kanali falas: <b>42</b> anëtarë" in report["json"]["text"]

    later = telegram_world([])
    assert run(config, ENV, session=later, now=MONDAY + timedelta(hours=3)) == 0
    assert calls_to(later, "sendMessage") == []


def test_dry_run_prints_the_report_and_leaves_telegrams_updates_alone(config):
    session, printed = telegram_world([update(5)]), []
    assert run(config, ENV, dry_run=True, session=session, now=NOW, output=printed.append) == 0
    assert calls_to(session, "getUpdates") == [] and calls_to(session, "sendMessage") == []
    assert "👥 Kanali falas: <b>42</b> anëtarë" in printed[0]
    with Storage(config.db_path) as storage:
        assert storage.member_count_since(FREE, NOW - timedelta(days=1)) is None   # nothing saved


def test_without_premium_only_the_free_channel_is_counted(config):
    session = telegram_world([])
    off = replace(config, premium=replace(config.premium, enabled=False))
    assert run(off, ENV, report_now=True, session=session, now=NOW) == 0
    assert [c["json"]["chat_id"] for c in calls_to(session, "getChatMemberCount")] == ["@flyfromtirana"]
    assert "Premium" not in calls_to(session, "sendMessage")[0]["json"]["text"]


def test_run_needs_the_bot_and_the_channel(config):
    with pytest.raises(ConfigError):
        run(config, {"TELEGRAM_BOT_TOKEN": "123:bot"}, session=telegram_world([]), now=NOW)
