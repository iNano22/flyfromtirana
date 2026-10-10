"""Premium channel: instant posts there, delayed posts on the free channel."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.config import ConfigError, Secrets, load_config
from src.deals import Deal, find_route_deal, price_band
from src.formatter import format_post
from src.main import run
from tests.conftest import NOW, make_quote, telegram_chat, telegram_text
from tests.test_formatter import AIRLINES, make_links
from src.storage import Storage
from tests.test_main import fake_world, telegram_calls

PREMIUM_ID = "-1001234567890"
SECRETS = Secrets("tp-token", "12345", "123:bot", "@flyfromtirana", PREMIUM_ID)


# --- the early-access gate in deal detection ---------------------------------

def find_free(route, storage, rules, quotes):
    return find_route_deal(route, "TIA", quotes, [], storage, rules, NOW, "telegram",
                           early_access_from="telegram_premium",
                           early_access_until=NOW - timedelta(hours=6))


def premium_posted(storage, price, hours_ago, depart=date(2026, 10, 20)):
    storage.record_post("telegram_premium", make_quote(price, depart), price_band(price, 10),
                        posted_at=NOW - timedelta(hours=hours_ago))


def test_free_waits_until_premium_had_it_long_enough(storage, route, rules):
    assert find_free(route, storage, rules, [make_quote(19)]) is None   # premium never posted it
    premium_posted(storage, 19, hours_ago=2)
    assert find_free(route, storage, rules, [make_quote(19)]) is None   # only 2h ago


def test_free_gets_it_after_the_delay(storage, route, rules):
    premium_posted(storage, 19, hours_ago=7)
    deal = find_free(route, storage, rules, [make_quote(19)])
    assert deal is not None
    assert deal.premium_lead_hours == 7


def test_free_ignores_premium_posts_older_than_cooldown(storage, route, rules):
    premium_posted(storage, 19, hours_ago=24 * 8)  # cooldown is 7 days
    assert find_free(route, storage, rules, [make_quote(19)]) is None


# --- post text -----------------------------------------------------------------

def test_free_post_advertises_premium(route):
    deal = Deal(route=route, quotes=[make_quote(19)], median=None, reason="threshold", premium_lead_hours=6)
    text = format_post(deal, make_links(), language="sq", channel_handle="@flyfromtirana",
                       airline_names=AIRLINES, premium_link="https://t.me/+abc")
    assert '⚡ Anëtarët Premium e morën këtë ofertë 6 orë më parë · <a href="https://t.me/+abc">Bashkohu</a>' in text


def test_no_premium_line_when_premium_is_off(route):
    deal = Deal(route=route, quotes=[make_quote(19)], median=None, reason="threshold")
    text = format_post(deal, make_links(), language="sq", channel_handle="@flyfromtirana", airline_names=AIRLINES)
    assert "Premium" not in text


def test_premium_template(route):
    deal = Deal(route=route, quotes=[make_quote(19)], median=None, reason="threshold")
    text = format_post(deal, make_links(), language="sq", channel_handle="@flyfromtirana",
                       airline_names=AIRLINES, template="sq_premium")
    assert text.startswith("💎 <b>PREMIUM</b>\n✈️ <b>TIRANA → MILAN</b> (Bergamo) 🇮🇹")
    assert "Ndiq @flyfromtirana" not in text


# --- end to end ------------------------------------------------------------------

@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", request_delay_seconds=0,
                   premium=replace(base.premium, enabled=True, join_link="https://t.me/+abc"))


def sent_to(session, chat_id):
    return [telegram_text(c) for c in session.calls
            if "api.telegram.org" in c["url"] and telegram_chat(c) == chat_id]


def test_premium_first_then_free_six_hours_later(config):
    first = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=first, now=NOW) == 0
    assert len(sent_to(first, PREMIUM_ID)) == 1
    assert sent_to(first, "@flyfromtirana") == []          # free has to wait

    three_h = fake_world()
    run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=three_h, now=NOW + timedelta(hours=3))
    assert sent_to(three_h, "@flyfromtirana") == []        # still waiting
    assert sent_to(three_h, PREMIUM_ID) == []              # premium already has it

    six_h = fake_world()
    run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=six_h, now=NOW + timedelta(hours=6))
    free_posts = sent_to(six_h, "@flyfromtirana")
    assert len(free_posts) == 1
    assert "e morën këtë ofertë 6 orë më parë" in free_posts[0]


# --- quick premium scans between the full runs -------------------------------------

def price_rows(config):
    with Storage(config.db_path) as storage:
        return storage.conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]


def test_premium_only_scan_posts_to_premium_and_saves_no_prices(config):
    run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=fake_world(),
        now=NOW.replace(hour=5))                               # quiet hours: a full run saves the history
    history = price_rows(config)
    assert history > 0

    quick = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, premium_only=True, session=quick, now=NOW) == 0
    assert len(sent_to(quick, PREMIUM_ID)) == 1
    assert sent_to(quick, "@flyfromtirana") == []
    assert price_rows(config) == history                       # the quick scan added nothing

    # The free channel still gets it at its own run, once premium has had it for 6 hours.
    later = fake_world()
    run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=later, now=NOW + timedelta(hours=6))
    assert len(sent_to(later, "@flyfromtirana")) == 1
    assert sent_to(later, PREMIUM_ID) == []                    # premium is not told twice


def test_premium_only_scan_is_skipped_in_quiet_hours(config):
    session = fake_world()
    night = NOW.replace(hour=22)  # 00:00 in Tirana
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, premium_only=True, session=session,
               now=night) == 0
    assert session.calls == []                                 # not even the price API was asked


def test_premium_only_scan_does_nothing_when_premium_is_off(config):
    config = replace(config, premium=replace(config.premium, enabled=False))
    session = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, premium_only=True, session=session, now=NOW) == 0
    assert telegram_calls(session) == []


def test_scan_every_minutes_is_read_and_checked(tmp_path):
    assert load_config().premium.scan_every_minutes == 15

    def load(premium_yaml):
        path = tmp_path / "config.yaml"
        path.write_text(f"routes:\n  - {{ iata: BGY, city: Milan }}\npremium:\n  {premium_yaml}\n",
                        encoding="utf-8")
        return load_config(path)

    assert load("enabled: true").premium.scan_every_minutes == 0        # not set = no quick scans
    assert load("scan_every_minutes: 30").premium.scan_every_minutes == 30
    with pytest.raises(ConfigError, match="scan_every_minutes"):
        load("scan_every_minutes: 1")


def test_premium_rules_are_looser_than_free():
    config = load_config()
    assert config.premium.rules.discount_pct < config.rules.discount_pct
