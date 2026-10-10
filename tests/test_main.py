"""End-to-end runs of main.run() with fake Travelpayouts + Telegram."""
from dataclasses import replace
from datetime import date, time, timedelta

import pytest

from src.config import Secrets, load_config
from src.main import in_quiet_hours, main, run
from src.storage import Storage
from tests.conftest import NOW, FakeResponse, FakeSession, api_row, travelpayouts_handler

SECRETS = Secrets("tp-token", "12345", "123:bot", "@flyfromtirana")


@pytest.fixture
def config(tmp_path):
    base = load_config()
    # These tests cover the free channel on its own; premium has tests/test_premium.py.
    return replace(base, db_path=tmp_path / "prices.db", request_delay_seconds=0,
                   premium=replace(base.premium, enabled=False))


def fake_world(telegram_ok: bool = True) -> FakeSession:
    """BGY: ~€60 most days, two cheap days. A €24 flight back a week later."""
    rows = [api_row("TIA", "BGY", date(2026, 10, 10) + timedelta(days=i), 60 + i % 5) for i in range(30)]
    rows += [api_row("TIA", "BGY", date(2026, 11, 20), 19), api_row("TIA", "BGY", date(2026, 11, 22), 20)]
    rows += [api_row("BGY", "TIA", date(2026, 11, 27), 24)]
    api = travelpayouts_handler(rows)
    message_ids = iter(range(1, 100))

    def handler(method, url, kwargs):
        if "api.telegram.org" in url:
            if not telegram_ok:
                return FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})
            return FakeResponse(200, {"ok": True, "result": {"message_id": next(message_ids)}})
        return api(method, url, kwargs)

    return FakeSession(handler)


def telegram_calls(session):
    return [c for c in session.calls if "api.telegram.org" in c["url"]]


def posted_count(config):
    with Storage(config.db_path) as storage:
        return storage.conn.execute("SELECT COUNT(*) FROM posted_deals").fetchone()[0]


def test_dry_run_prints_posts_and_sends_nothing(config):
    session, printed = fake_world(), []
    code = run(config, SECRETS, dry_run=True, only_routes={"BGY"}, session=session, now=NOW, output=printed.append)

    assert code == 0
    assert len(printed) == 1
    post = printed[0]
    assert "✈️ <b>TIRANA → MILAN</b> (Bergamo) 🇮🇹" in post
    assert "💰 <b>nga €19</b> one way" in post
    assert "🔥 <b>-69%</b> · zakonisht ~€62" in post
    assert "📅 <b>20 Nën, 22 Nën</b>" in post
    assert "🔁 Kthimi nga €24" in post
    assert f'<a href="{config.website.url}">Të gjitha ofertat në faqen tonë</a>' in post
    assert telegram_calls(session) == []
    assert posted_count(config) == 0  # dry runs don't mark anything as posted


def test_real_run_posts_once_then_dedupes(config):
    session = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 0
    assert len(telegram_calls(session)) == 1
    assert posted_count(config) == 2  # both dates in the post are remembered

    # Three hours later, same prices: nothing new to post.
    session2 = fake_world()
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session2,
               now=NOW + timedelta(hours=3)) == 0
    assert telegram_calls(session2) == []


def test_quiet_hours_skip_posting(config):
    session = fake_world()
    night = NOW.replace(hour=22)  # 00:00 in Tirana
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=night) == 0
    assert telegram_calls(session) == []
    assert posted_count(config) == 0


def test_failed_post_exits_nonzero_and_is_not_recorded(config):
    session = fake_world(telegram_ok=False)
    assert run(config, SECRETS, dry_run=False, only_routes={"BGY"}, session=session, now=NOW) == 1
    assert posted_count(config) == 0  # so the next run tries again


def test_all_routes_failing_exits_nonzero(config):
    session = FakeSession(lambda m, u, kw: FakeResponse(400, {"success": False, "error": "bad request"}))
    assert run(config, SECRETS, dry_run=True, only_routes={"BGY"}, session=session, now=NOW) == 1


def test_missing_env_vars_exit_code_2(monkeypatch):
    for name in ("TRAVELPAYOUTS_TOKEN", "TRAVELPAYOUTS_MARKER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"):
        monkeypatch.setenv(name, "")  # set-but-empty also stops a local .env from filling them in
    assert main([]) == 2


def test_unknown_route_is_a_config_error(monkeypatch):
    monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "x")
    assert main(["--dry-run", "--routes", "XYZ"]) == 2


@pytest.mark.parametrize("now, expected", [
    (time(22, 59), False), (time(23, 0), True), (time(2, 0), True), (time(7, 59), True), (time(8, 0), False),
])
def test_quiet_hours_across_midnight(now, expected):
    assert in_quiet_hours(now, (time(23, 0), time(8, 0))) is expected


def test_quiet_hours_disabled():
    assert in_quiet_hours(time(3, 0), None) is False
