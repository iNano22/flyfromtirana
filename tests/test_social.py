"""Social media drafts: the cards and the caption go to the owner's chat for the free channel's deals."""
import json
import re
import shutil
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.config import Secrets, load_config
from src.deals import price_band
from src.main import run
from src.social import SOCIAL_CHANNEL
from src.storage import Storage
from tests.conftest import NOW, FakeResponse, FakeSession, make_quote, telegram_chat, telegram_text
from tests.test_main import fake_world, telegram_calls

DRAFTS_CHAT = "777000111"
SECRETS = Secrets("tp-token", "12345", "123:bot", "@flyfromtirana", telegram_drafts_chat_id=DRAFTS_CHAT)


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", request_delay_seconds=0,
                   premium=replace(base.premium, enabled=False),
                   social=replace(base.social, enabled=True, max_posts_per_day=2, hashtags="#flyfromtirana #tirana"))


def drafts(session) -> list[dict]:
    """The calls that went to the drafts chat, in order."""
    return [call for call in telegram_calls(session) if telegram_chat(call) == DRAFTS_CHAT]


def channel_posts(session) -> list[dict]:
    return [call for call in telegram_calls(session) if telegram_chat(call) == "@flyfromtirana"]


def methods(calls) -> list[str]:
    return [call["url"].rsplit("/", 1)[1] for call in calls]


def social_rows(config) -> int:
    with Storage(config.db_path) as storage:
        return storage.conn.execute("SELECT COUNT(*) FROM posted_deals WHERE channel = ?",
                                    (SOCIAL_CHANNEL,)).fetchone()[0]


def seed_social_post(config, destination: str, posted_at) -> None:
    """Pretend a draft for this route went out at `posted_at`."""
    with Storage(config.db_path) as storage:
        quote = make_quote(30, date(2026, 12, 1), destination=destination)
        storage.record_post(SOCIAL_CHANNEL, quote, price_band(30, 10), posted_at)


def test_draft_follows_the_channel_post(config):
    session = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0

    assert methods(channel_posts(session)) == ["sendPhoto"]
    files, caption, link = drafts(session)
    assert methods([files, caption, link]) == ["sendMediaGroup", "sendMessage", "sendMessage"]

    # The two cards go as files (full size), the feed card first, with the instructions under them.
    media = json.loads(files["data"]["media"])
    assert [item["type"] for item in media] == ["document", "document"]
    assert [item["media"] for item in media] == ["attach://file0", "attach://file1"]
    assert "Draft për Instagram dhe Facebook" in media[0]["caption"]
    assert "TIRANA → MILAN (Bergamo) nga €19" in media[0]["caption"]
    assert files["files"]["file0"][0] == "bgy_feed.jpg"
    assert files["files"]["file1"][0] == "bgy_story.jpg"
    assert files["files"]["file0"][1][:2] == b"\xff\xd8"    # JPEG bytes

    # The caption: plain text with the deal, the bio link, the channel and the hashtags.
    text = telegram_text(caption)
    assert "✈️ TIRANA → MILAN (Bergamo) nga €19 one way 🔥 -69%" in text
    assert "💡 Zakonisht ~€62" in text
    assert "📅 20 Nën, 22 Nën" in text
    assert "🔁 Kthimi nga €24" in text
    assert "linku në bio: flyfromtirana.al" in text
    assert "@flyfromtirana" in text
    assert text.endswith("#flyfromtirana #tirana")
    assert "<a " not in text and "Foto:" not in text   # no link tags; the photo needs no credit

    # The booking link for Facebook has its own SubID.
    assert "marker=12345.facebook" in telegram_text(link)
    assert "Facebook" in telegram_text(link)
    assert social_rows(config) == 2   # both dates of the post are remembered


def test_only_the_first_drafts_of_the_day_go_out(config):
    two_hours_ago = NOW - timedelta(hours=2)   # 10:00 in Tirana: the same day
    seed_social_post(config, "VIE", two_hours_ago)
    seed_social_post(config, "FCO", two_hours_ago)
    session = fake_world()

    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert methods(channel_posts(session)) == ["sendPhoto"]   # the channel is not held back
    assert drafts(session) == []


def test_yesterdays_drafts_do_not_count(config):
    seed_social_post(config, "VIE", NOW - timedelta(hours=20))   # 16:00 in Tirana, the day before
    seed_social_post(config, "FCO", NOW - timedelta(hours=20))
    session = fake_world()

    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert len(drafts(session)) == 3


def test_a_deal_the_drafts_chat_already_had_is_skipped(config):
    with Storage(config.db_path) as storage:
        storage.record_post(SOCIAL_CHANNEL, make_quote(19, date(2026, 11, 20)), price_band(19, 10),
                            NOW - timedelta(days=2))
    session = fake_world()

    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert methods(channel_posts(session)) == ["sendPhoto"]
    assert drafts(session) == []


def test_failed_draft_flags_the_run_but_the_channel_post_stands(config):
    session = fake_world()
    good_handler = session.handler

    def handler(method, url, kwargs):
        if url.endswith("/sendMediaGroup"):
            return FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})
        return good_handler(method, url, kwargs)
    session.handler = handler

    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 1
    assert methods(channel_posts(session)) == ["sendPhoto"]
    assert methods(drafts(session)) == ["sendMediaGroup"]   # stopped at the first failure
    assert social_rows(config) == 0                           # so the next run tries again
    with Storage(config.db_path) as storage:
        assert storage.conn.execute("SELECT COUNT(*) FROM posted_deals WHERE channel = 'telegram'").fetchone()[0] == 2


def test_without_a_drafts_chat_the_run_warns_and_goes_on(config, caplog):
    session = fake_world()
    no_chat = replace(SECRETS, telegram_drafts_chat_id="")

    assert run(config, no_chat, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert methods(channel_posts(session)) == ["sendPhoto"]
    assert drafts(session) == []
    assert "TELEGRAM_DRAFTS_CHAT_ID is not set" in caplog.text


def test_social_can_be_switched_off(config):
    session = fake_world()
    config = replace(config, social=replace(config.social, enabled=False))
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert methods(channel_posts(session)) == ["sendPhoto"]
    assert drafts(session) == []


def test_quiet_hours_hold_the_drafts_too(config):
    session = fake_world()
    night = NOW.replace(hour=22)   # 00:00 in Tirana
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=night) == 0
    assert telegram_calls(session) == []
    assert social_rows(config) == 0


def test_dry_run_prints_the_caption_and_keeps_the_cards(config):
    session, printed = fake_world(), []
    assert run(config, SECRETS, dry_run=True, only_routes={"BGY"}, session=session, now=NOW,
               output=printed.append) == 0

    assert telegram_calls(session) == []
    assert social_rows(config) == 0
    draft = [text for text in printed if "DRY RUN [social]" in text]
    assert len(draft) == 1
    assert "✈️ TIRANA → MILAN (Bergamo) nga €19 one way" in draft[0]
    folder = Path(re.search(r"cards in (\S+) -----", draft[0]).group(1))
    try:
        assert sorted(path.name for path in folder.iterdir()) == ["bgy_feed.jpg", "bgy_story.jpg"]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_config_yaml_settings():
    social = load_config().social
    assert social.max_posts_per_day == 2
    assert social.sub_id == "facebook"
    assert social.hashtags.startswith("#")
