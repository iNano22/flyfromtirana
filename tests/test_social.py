"""Social media posts: at each posting time the best deal on the website, as a draft to the owner's chat or
posted by the bot."""
import json
import re
import shutil
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from src.config import Secrets, _post_times, load_config
from src.deals import PREMIUM_CHANNEL, price_band
from src.main import run
from src.social import SOCIAL_CHANNEL
from src.storage import Storage
from tests.conftest import (NOW, FakeResponse, FakeSession, api_row, make_quote, telegram_chat, telegram_text,
                            travelpayouts_handler)
from tests.test_main import fake_world, telegram_calls

DRAFTS_CHAT = "777000111"
SECRETS = Secrets("tp-token", "12345", "123:bot", "@flyfromtirana", telegram_drafts_chat_id=DRAFTS_CHAT)


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", request_delay_seconds=0,
                   premium=replace(base.premium, enabled=False),
                   # Pictures here; the Reels have their own tests (the end of this file, and test_reels.py).
                   social=replace(base.social, enabled=True, hashtags="#flyfromtirana #tirana", reels=False),
                   invite_links={"facebook": "https://t.me/+fb"})


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
    # The channel as an address to type: "@flyfromtirana" would be the Instagram account itself.
    assert "Telegram: t.me/flyfromtirana (linku në bio)" in text and "@flyfromtirana" not in text
    assert text.endswith("#flyfromtirana #tirana")
    assert "<a " not in text and "Foto:" not in text   # no link tags; the photo needs no credit

    # The booking link for Facebook has its own SubID.
    assert "marker=12345.facebook" in telegram_text(link)
    assert "Facebook" in telegram_text(link)
    assert social_rows(config) == 2   # both dates of the post are remembered


def test_one_post_per_posting_time_and_never_the_same_city_twice_a_day(config):
    """12:00: one post, the best deal. The quick scans after it: nothing. 19:30: the next deal, another city."""
    seed_current_deals(config, fetched_at=NOW, destinations=("VIE",))   # Vienna €9: the biggest saving
    noon = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=noon, now=NOW) == 0   # 12:00
    assert methods(channel_posts(noon)) == ["sendPhoto"]      # the channel posts as always
    files, caption, link = drafts(noon)                       # one post, although two deals are on the site
    assert "TIRANA → VIENNA nga €9" in telegram_text(caption)

    quarter_past = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=quarter_past, now=NOW + timedelta(minutes=15)) == 0
    assert drafts(quarter_past) == []                         # this time's post is out

    evening = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=evening,
               now=NOW + timedelta(hours=7, minutes=30)) == 0   # 19:30
    files, caption, link = drafts(evening)
    assert "TIRANA → MILAN (Bergamo) nga €19" in telegram_text(caption)   # Vienna is still the best, but had its post

    tomorrow = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=tomorrow, now=NOW + timedelta(days=1)) == 0
    assert drafts(tomorrow) == []                             # both are known for repost_cooldown_days


def test_nothing_goes_out_before_the_first_posting_time(config):
    morning = NOW.replace(hour=7)   # 09:00 in Tirana: quiet hours are over, the channel posts
    session = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=morning) == 0
    assert methods(channel_posts(session)) == ["sendPhoto"]
    assert drafts(session) == []


def test_a_destination_posted_at_this_price_is_skipped_whatever_the_date(config):
    with Storage(config.db_path) as storage:   # Milan at €19 two days ago, for another date
        storage.record_post(SOCIAL_CHANNEL, make_quote(19, date(2026, 12, 5)), price_band(19, 10),
                            NOW - timedelta(days=2))
    session = fake_world()

    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert methods(channel_posts(session)) == ["sendPhoto"]
    assert drafts(session) == []


def test_a_cheaper_price_is_news_again(config):
    seed_social_post(config, "BGY", NOW - timedelta(days=2))   # Milan at €30 the other day
    session = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert len(drafts(session)) == 3                          # €19 now: posted


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
    folder = Path(re.search(r"pictures in (\S+) -----", draft[0]).group(1))
    try:
        assert sorted(path.name for path in folder.iterdir()) == ["bgy_feed.jpg", "bgy_story.jpg"]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_config_yaml_settings():
    social = load_config().social
    assert social.post_times == (time(12, 0), time(19, 30))
    assert social.sub_id == "facebook"
    assert social.hashtags.startswith("#")


@pytest.mark.parametrize("raw, complaint", [
    ([], "list of times"),
    ([720], "quotes"),                                  # YAML's reading of an unquoted 12:00
    (["noon"], "HH:MM"),
    (["12:00", "12:30"], "an hour apart"),
    (["19:30", "12:00"], "in order"),
    ([f"{hour:02d}:00" for hour in range(7)], "more than 6"),
])
def test_post_times_are_checked(raw, complaint):
    with pytest.raises(ValueError, match=complaint):
        _post_times(raw)


# --- posting by the bot (META_PAGE_TOKEN set) ----------------------------------------

from src.meta import GRAPH_URL  # noqa: E402
from tests.test_meta import ME, graph_handler  # noqa: E402

META_SECRETS = replace(SECRETS, meta_page_token="page-token")


@pytest.fixture
def posting(config, tmp_path):
    """The config with the bot posting on both platforms, the site built into a temp folder."""
    site = replace(config.website, output_dir=tmp_path / "docs", url="https://flyfromtirana.al/")
    return replace(config, website=site, social=replace(config.social, publish=("instagram", "facebook")))


def with_graph(session, **graph_options) -> FakeSession:
    """The fake world plus a fake graph.facebook.com."""
    telegram_and_api = session.handler
    graph = graph_handler(**graph_options)

    def handler(method, url, kwargs):
        if "graph.facebook.com" in url:
            return graph(method, url, kwargs)
        return telegram_and_api(method, url, kwargs)
    session.handler = handler
    return session


def graph_calls(session) -> list[str]:
    return [f'{c["method"]} {c["url"].replace(GRAPH_URL.format(path=""), "")}'
            for c in session.calls if "graph.facebook.com" in c["url"]]


def test_bot_posts_on_instagram_and_facebook_and_reports(posting):
    session = with_graph(fake_world())
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0

    # The pictures are on the site, named after the route and the moment of posting.
    folder = posting.website.output_dir / "social"
    assert sorted(p.name for p in folder.iterdir()) == ["bgy_feed_20261009-1000.jpg", "bgy_story_20261009-1000.jpg"]

    # Instagram: a feed post and a story, Facebook: one Page post, all from those addresses.
    calls = graph_calls(session)
    assert calls.count("POST 17841417241858036/media") == 2
    assert calls.count("POST 17841417241858036/media_publish") == 2
    assert calls.count("POST 1348603305007068/photos") == 1
    graph = [c for c in session.calls if "graph.facebook.com" in c["url"]]
    feed, story = [c["data"] for c in graph if c["url"].endswith("/media")]
    assert feed["image_url"] == "https://flyfromtirana.al/social/bgy_feed_20261009-1000.jpg"
    assert feed["caption"].startswith("✈️ TIRANA → MILAN (Bergamo) nga €19 one way 🔥 -69%")
    assert "linku në bio" in feed["caption"] and "&amp;" not in feed["caption"]
    assert story == {"image_url": "https://flyfromtirana.al/social/bgy_story_20261009-1000.jpg", "media_type": "STORIES"}
    photo, = [c["data"] for c in graph if c["url"].endswith("/photos")]
    assert photo["url"] == feed["image_url"]
    assert "👉 Rezervo këtu: https://" in photo["message"] and "marker=12345.facebook" in photo["message"]
    assert "&amp;" not in photo["message"] and "linku në bio" not in photo["message"]
    assert "Ofertat dalin të parat në Telegram: https://t.me/+fb\n" in photo["message"]   # Facebook's own invite link

    # The owner gets one report with the links, no draft; the deal is remembered.
    report, = drafts(session)
    text = telegram_text(report)
    assert text.startswith("✅ <b>U postua</b> · TIRANA → MILAN (Bergamo) nga €19")
    assert "https://www.instagram.com/p/abc123/" in text
    assert "https://www.facebook.com/1348603305007068_777" in text
    assert "📱 Story: ✅" in text
    assert social_rows(posting) == 2


def test_failed_platform_is_reported_and_sent_as_a_draft(posting):
    session = with_graph(fake_world(), facebook_ok=False)
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0

    report, files, caption, link = drafts(session)
    text = telegram_text(report)
    assert text.startswith("⚠️ <b>Postim i pjesshëm</b>")
    assert "https://www.instagram.com/p/abc123/" in text
    assert "❌ Facebook: Meta refused the call (HTTP 400, code 100): Invalid parameter" in text
    assert "Postoje me dorë" in text
    assert methods([files, caption, link]) == ["sendMediaGroup", "sendMessage", "sendMessage"]
    assert social_rows(posting) == 2   # handled: posted on Instagram, handed over for Facebook


def test_bad_token_means_a_draft_for_everything(posting):
    session = fake_world()
    good = session.handler

    def handler(method, url, kwargs):
        if "graph.facebook.com" in url:
            return FakeResponse(400, {"error": {"message": "Error validating access token", "code": 190}})
        return good(method, url, kwargs)
    session.handler = handler

    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    report, files, caption, link = drafts(session)
    text = telegram_text(report)
    assert text.startswith("❌ <b>Postimi dështoi</b>")
    assert "META_PAGE_TOKEN is no longer valid" in text
    assert methods([files, caption, link]) == ["sendMediaGroup", "sendMessage", "sendMessage"]
    assert social_rows(posting) == 2


def test_without_the_token_the_posts_are_drafts(posting):
    session = with_graph(fake_world())
    assert run(posting, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert graph_calls(session) == []
    assert methods(drafts(session)) == ["sendMediaGroup", "sendMessage", "sendMessage"]
    assert not (posting.website.output_dir / "social").exists()


def test_old_pictures_are_removed_from_the_site(posting):
    import os
    folder = posting.website.output_dir / "social"
    folder.mkdir(parents=True)
    old = folder / "vie_20260901-0817_feed.jpg"
    old.write_bytes(b"old")
    stamp = (NOW - timedelta(days=10)).timestamp()
    os.utime(old, (stamp, stamp))
    fresh = folder / "fco_20261008-0817_feed.jpg"
    fresh.write_bytes(b"fresh")

    session = with_graph(fake_world())
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    names = sorted(p.name for p in folder.iterdir())
    assert "vie_20260901-0817_feed.jpg" not in names
    assert "fco_20261008-0817_feed.jpg" in names


# --- the week's top deals and the city guides ---------------------------------------------

from src.social import GUIDE_CHANNEL, WEEKLY_CHANNEL  # noqa: E402

SUNDAY = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)   # 12:00 in Tirana


def seed_current_deals(config, *, fetched_at, destinations=("VIE", "FCO")) -> None:
    """Cheap current prices for other routes, so the website lists them as deals (under their thresholds)."""
    with Storage(config.db_path) as storage:
        storage.save_quotes([make_quote(9, date(2026, 11, 20), destination=code, fetched_at=fetched_at)
                            for code in destinations])


def rows(config, channel) -> list[tuple]:
    with Storage(config.db_path) as storage:
        return storage.conn.execute("SELECT destination, depart_date FROM posted_deals WHERE channel = ? "
                                    "ORDER BY destination", (channel,)).fetchall()


def test_the_quick_premium_scan_keeps_the_posting_time(config):
    """Between full runs the 15-minute premium scan makes the post whose time has come."""
    config = replace(config, premium=replace(load_config().premium, enabled=True))
    secrets = replace(SECRETS, telegram_premium_channel_id="-1001234567890")
    seed_current_deals(config, fetched_at=NOW - timedelta(hours=1), destinations=("VIE",))
    with Storage(config.db_path) as storage:   # premium had Vienna 7 hours ago, so the website shows it
        storage.record_post(PREMIUM_CHANNEL, make_quote(9, date(2026, 11, 20), destination="VIE"), price_band(9, 10),
                            NOW - timedelta(hours=7))
    session = fake_world()
    assert run(config, secrets, dry_run=False, only_routes={"BGY"}, premium_only=True, session=session, now=NOW) == 0

    assert channel_posts(session) == []                       # the free channel waits for the full run
    files, caption, link = drafts(session)
    assert methods([files, caption, link]) == ["sendMediaGroup", "sendMessage", "sendMessage"]
    assert "TIRANA → VIENNA nga €9" in telegram_text(caption)


def test_sunday_starts_with_the_weekly_carousel_and_a_deal_follows_in_the_evening(posting):
    posting = replace(posting, social=replace(posting.social, weekly_day=6, weekly_count=5))
    seed_current_deals(posting, fetched_at=SUNDAY)
    session = with_graph(fake_world())
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=SUNDAY) == 0

    weekly_report, = drafts(session)   # the day's first post is the week's top, not a deal
    text = telegram_text(weekly_report)
    assert text.startswith("✅ <b>U postua</b> · TOP 3 ofertat e javës")
    assert "📱 Story" not in text   # a carousel has no story

    graph = [c for c in session.calls if "graph.facebook.com" in c["url"]]
    items = [c["data"] for c in graph if c["url"].endswith("/media") and c["data"].get("is_carousel_item")]
    assert [item["image_url"].rsplit("/", 1)[1] for item in items] == [
        "weekly_cover_20261011-1000.jpg", "weekly_1_vie_20261011-1000.jpg", "weekly_2_bgy_20261011-1000.jpg",
        "weekly_3_fco_20261011-1000.jpg"]   # best saving first: VIE €9 of a €35 threshold, BGY €19 of ~€62
    carousel, = [c["data"] for c in graph if c.get("data", {}).get("media_type") == "CAROUSEL"]
    assert carousel["caption"].startswith("🏆 TOP 3 OFERTAT E JAVËS nga Tirana (11.10)\n\n1️⃣ Vienna nga €9")
    assert "linku në bio" in carousel["caption"]
    album, = [c["data"] for c in graph if c["url"].endswith("/feed")]
    assert "https://flyfromtirana.al/" in album["message"] and "attached_media[3]" in album
    assert len([c for c in graph if c["url"].endswith("/photos")]) == 4   # the album's unpublished pictures
    assert rows(posting, WEEKLY_CHANNEL) == [("BGY", "2026-11-20"), ("FCO", "2026-11-20"), ("VIE", "2026-11-20")]

    # Later the same day: the weekly post is not made again. At the evening's time, the best deal.
    session2 = with_graph(fake_world())
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session2,
               now=SUNDAY + timedelta(hours=3)) == 0
    assert drafts(session2) == []
    session3 = with_graph(fake_world())
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session3,
               now=SUNDAY + timedelta(hours=7, minutes=30)) == 0
    deal_report, = drafts(session3)
    assert "TIRANA → VIENNA nga €9" in telegram_text(deal_report)


def test_weekly_needs_three_deals(posting):
    posting = replace(posting, social=replace(posting.social, weekly_day=6))
    session = with_graph(fake_world())   # only BGY is a deal
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=SUNDAY) == 0
    assert len(drafts(session)) == 1   # the deal's report: the day's first post is a deal instead
    assert rows(posting, WEEKLY_CHANNEL) == []


def test_no_weekly_on_other_days(posting):
    posting = replace(posting, social=replace(posting.social, weekly_day=6))
    seed_current_deals(posting, fetched_at=NOW)
    session = with_graph(fake_world())
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0   # a Friday
    assert rows(posting, WEEKLY_CHANNEL) == []


def quiet_world() -> FakeSession:
    """BGY at ~€60 every day: no deal anywhere."""
    api_rows = [api_row("TIA", "BGY", date(2026, 10, 10) + timedelta(days=i), 60 + i % 5) for i in range(30)]
    session = fake_world()
    api = travelpayouts_handler(api_rows)
    telegram = session.handler

    def handler(method, url, kwargs):
        if "api.travelpayouts.com" in url:
            return api(method, url, kwargs)
        return telegram(method, url, kwargs)
    session.handler = handler
    return with_graph(session)


def test_a_day_without_deals_gets_a_city_guide_at_the_last_posting_time(posting):
    evening = NOW.replace(hour=17, minute=30)   # 19:30 in Tirana: the day's last posting time
    session = quiet_world()
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=evening) == 0

    assert channel_posts(session) == []
    report, = drafts(session)
    text = telegram_text(report)
    assert text.startswith("✅ <b>U postua</b> · Udhëzues: Milan")   # the first city with a guide, in config order
    assert "📱 Story: ✅" in text
    graph = [c for c in session.calls if "graph.facebook.com" in c["url"]]
    creates = [c["data"] for c in graph if c["url"].endswith("/media")]
    items = [d for d in creates if d.get("is_carousel_item")]
    assert items[0]["image_url"].endswith("/social/guide_milan_feed_20261009-1730.jpg")   # the cover first
    assert items[1]["image_url"].endswith("/social/guide_milan_2_sights_20261009-1730.jpg")
    assert 5 <= len(items) <= 10
    carousel, = [d for d in creates if d.get("media_type") == "CAROUSEL"]
    assert "📍 MILAN:" in carousel["caption"] and "flyfromtirana.al/milan/ — linku në bio" in carousel["caption"]
    assert "👀 Mos humb: " in carousel["caption"] and "🍴 Provo: " in carousel["caption"]
    assert "Fluturime nga Tirana nga €60" in carousel["caption"]   # BGY's cheapest current price
    story, = [d for d in creates if d.get("media_type") == "STORIES"]
    assert story["image_url"].endswith("/social/guide_milan_story_20261009-1730.jpg")
    album, = [c["data"] for c in graph if c["url"].endswith("/feed")]
    assert "https://flyfromtirana.al/milan/" in album["message"] and "attached_media[4]" in album
    assert rows(posting, GUIDE_CHANNEL) == [("MXP", "2026-10-09")]

    # Later the same evening: nothing more. The next day it is another city's turn.
    session2 = quiet_world()
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session2,
               now=evening + timedelta(hours=3)) == 0
    assert drafts(session2) == []
    session3 = quiet_world()
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session3,
               now=evening + timedelta(days=1)) == 0
    assert "Udhëzues: Rome" in telegram_text(drafts(session3)[0])


def test_no_guide_before_the_last_posting_time_or_when_a_deal_was_posted(posting):
    session = quiet_world()
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0   # 12:00
    assert drafts(session) == []   # no deal, but the evening may still bring one

    evening = NOW.replace(hour=17, minute=30)   # 19:30
    session = with_graph(fake_world())   # BGY is a deal: it is posted, so no guide
    assert run(posting, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=evening) == 0
    report, = drafts(session)
    assert "TIRANA → MILAN" in telegram_text(report)
    assert rows(posting, GUIDE_CHANNEL) == []


def test_the_weekly_as_a_draft_without_the_token(posting):
    posting = replace(posting, social=replace(posting.social, weekly_day=6))
    seed_current_deals(posting, fetched_at=SUNDAY)
    session = with_graph(fake_world())
    assert run(posting, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=SUNDAY) == 0
    files = [c for c in drafts(session) if c["url"].endswith("/sendMediaGroup")]
    assert len(files) == 1   # the weekly's draft; a deal's follows in the evening
    weekly = json.loads(files[0]["data"]["media"])
    assert len(weekly) == 4 and "carousel" in weekly[0]["caption"]


# --- Reels (social.reels on) --------------------------------------------------------------

@pytest.fixture
def reels(posting, monkeypatch):
    """Reels on, with a stand-in for the video: making a real one takes seconds (test_reels.py does)."""
    made = []

    def fake_reel(deal, out_dir, **options):
        made.append(options)
        video, cover = out_dir / f"{deal.route.iata.lower()}_reel.mp4", out_dir / f"{deal.route.iata.lower()}_reel.jpg"
        video.write_bytes(b"mp4 bytes")
        cover.write_bytes(b"jpeg bytes")
        return video, cover
    monkeypatch.setattr("src.social.make_reel", fake_reel)
    config = replace(posting, social=replace(posting.social, reels=True))
    return config, made


def test_deal_goes_to_instagram_as_a_reel_with_its_cover(reels):
    config, made = reels
    session = with_graph(fake_world())
    assert run(config, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0

    assert made[0]["telegram"] == "t.me/flyfromtirana"
    track, start = made[0]["music"]                       # the first Reel: the first track, from its start
    assert track.name == "upbeat-v4-bombinsound.m4a" and start == 0
    folder = config.website.output_dir / "social"
    assert {"bgy_reel_20261009-1000.mp4", "bgy_reel_20261009-1000.jpg"} <= {p.name for p in folder.iterdir()}
    graph = [c for c in session.calls if "graph.facebook.com" in c["url"]]
    created = [c["data"] for c in graph if c["url"].endswith("/media")]
    reel, story = created
    assert reel["media_type"] == "REELS" and reel["share_to_feed"] == "true"
    assert reel["video_url"] == "https://flyfromtirana.al/social/bgy_reel_20261009-1000.mp4"
    assert reel["cover_url"] == "https://flyfromtirana.al/social/bgy_reel_20261009-1000.jpg"
    assert reel["caption"].startswith("✈️ TIRANA → MILAN (Bergamo) nga €19")
    assert story["media_type"] == "STORIES"
    photo, = [c["data"] for c in graph if c["url"].endswith("/photos")]
    assert photo["url"].endswith("/bgy_feed_20261009-1000.jpg")   # Facebook keeps the picture

    report, = drafts(session)
    assert '🎬 Reel: <a href="https://www.instagram.com/p/abc123/">' in telegram_text(report)


def test_refused_reel_falls_back_to_the_picture(reels):
    config, _ = reels
    session = with_graph(fake_world())
    graph = session.handler

    def handler(method, url, kwargs):   # Instagram refuses videos, takes pictures
        if url.endswith("/media") and kwargs.get("data", {}).get("media_type") == "REELS":
            return FakeResponse(400, {"error": {"message": "The video file is not supported", "code": 352}})
        return graph(method, url, kwargs)
    session.handler = handler

    assert run(config, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    report, files, caption, link = drafts(session)
    text = telegram_text(report)
    assert "❌ Reel: Meta refused the call (HTTP 400, code 352)" in text
    assert '📸 Instagram: <a href="https://www.instagram.com/p/abc123/">' in text   # the picture went out instead
    sent = json.loads(files["data"]["media"])
    assert files["files"]["file0"][0] == "bgy_reel.mp4" and files["files"]["file0"][2] == "video/mp4"
    assert len(sent) == 3                                                  # the video, the feed card, the story
    assert "postoje si Reel" in sent[0]["caption"]


def test_no_reel_without_ffmpeg_means_a_picture(posting, monkeypatch):
    monkeypatch.setattr("src.reels.shutil.which", lambda name: None)
    config = replace(posting, social=replace(posting.social, reels=True))
    session = with_graph(fake_world())
    assert run(config, META_SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    graph = [c for c in session.calls if "graph.facebook.com" in c["url"]]
    assert all("media_type" not in c["data"] or c["data"]["media_type"] == "STORIES"
               for c in graph if c["url"].endswith("/media"))
    assert "📸 Instagram:" in telegram_text(drafts(session)[0])
