"""The server scheduler: slot times, database seeding, and scan-then-website order."""
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src import scheduler
from src.config import ConfigError, load_config
from src.scheduler import (Alerts, next_run, next_slot, premium_scan_interval, run_once, run_premium_scan,
                           seed_database)
from src.telegram import TelegramClient, TelegramError


def utc(hour: int, minute: int, second: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, expected", [
    (utc(13, 5), utc(15, 17)),            # between slots
    (utc(15, 16, 59), utc(15, 17)),       # a second before one
    (utc(15, 17), utc(18, 17)),           # exactly on a slot: that one is taken, wait for the next
    (utc(15, 30), utc(18, 17)),
    (utc(0, 0), utc(0, 17)),
    (utc(23, 30), utc(0, 17, day=11)),    # rolls over midnight
])
def test_next_slot_is_minute_17_of_every_third_hour(now, expected):
    assert next_slot(now) == expected


def test_next_slot_reads_other_timezones_as_utc():
    tirana = timezone(timedelta(hours=2))
    assert next_slot(datetime(2026, 10, 10, 17, 0, tzinfo=tirana)) == utc(15, 17)


QUARTER = timedelta(minutes=15)


@pytest.mark.parametrize("last_start, expected", [
    (utc(15, 17, 5), (utc(15, 32, 5), False)),     # after a full run: a premium scan 15 minutes later
    (utc(15, 32, 5), (utc(15, 47, 5), False)),
    (utc(18, 2, 30), (utc(18, 17), True)),         # 18:17:30 would pass the slot: the full run takes over
    (utc(18, 2), (utc(18, 17), True)),             # landing exactly on the slot counts too
])
def test_premium_scans_fill_the_time_between_full_runs(last_start, expected):
    assert next_run(last_start, QUARTER) == expected


def test_without_premium_scans_every_run_is_a_full_one():
    assert next_run(utc(15, 17, 5), None) == (utc(18, 17), True)


@pytest.mark.parametrize("now, expected", [
    (utc(13, 5), utc(13, 17)),            # minutes 2, 17, 32 and 47 of every hour
    (utc(13, 17), utc(13, 32)),
    (utc(13, 50), utc(14, 2)),
    (utc(0, 0), utc(0, 2)),
    (utc(23, 50), utc(0, 2, day=11)),     # rolls over midnight without a gap
])
def test_full_runs_every_15_minutes(now, expected):
    assert next_slot(now, QUARTER) == expected
    assert next_run(now, None, QUARTER) == (expected, True)


def test_full_run_every_minutes_is_read_and_checked(tmp_path):
    assert load_config().full_run_every_minutes == 15

    def load(extra_yaml):
        path = tmp_path / "config.yaml"
        path.write_text(f"routes:\n  - {{ iata: BGY, city: Milan }}\n{extra_yaml}\n", encoding="utf-8")
        return load_config(path)

    assert load("").full_run_every_minutes == 180                      # not set = every 3 hours, as before
    assert load("full_run_every_minutes: 30").full_run_every_minutes == 30
    for wrong in (0, 4, 7, 100):                                       # too often, or doesn't fit a day evenly
        with pytest.raises(ConfigError, match="full_run_every_minutes"):
            load(f"full_run_every_minutes: {wrong}")


def test_premium_scan_interval_comes_from_the_config():
    config = load_config()
    on = replace(config, premium=replace(config.premium, enabled=True, scan_every_minutes=15))
    assert premium_scan_interval(on) == QUARTER
    assert premium_scan_interval(replace(on, premium=replace(on.premium, scan_every_minutes=0))) is None
    assert premium_scan_interval(replace(on, premium=replace(on.premium, enabled=False))) is None


def test_premium_scan_runs_the_scan_only(monkeypatch):
    ran = []
    monkeypatch.setattr(scheduler, "run_step", lambda name, module, timeout, *args: ran.append((module, args)) or 0)
    assert run_premium_scan() == 0
    assert ran == [("src.main", ("--premium-only",))]          # no website build


def test_seed_starts_a_new_database_and_never_overwrites_one(tmp_path):
    seed = tmp_path / "seed" / "prices.db"
    seed.parent.mkdir()
    seed.write_bytes(b"history")
    db = tmp_path / "data" / "prices.db"

    assert seed_database(db, seed) is True
    assert db.read_bytes() == b"history"

    db.write_bytes(b"newer history")
    assert seed_database(db, seed) is False
    assert db.read_bytes() == b"newer history"


def test_seed_does_nothing_without_a_seed_file(tmp_path):
    db = tmp_path / "data" / "prices.db"
    assert seed_database(db, tmp_path / "missing.db") is False
    assert not db.exists()


def test_website_is_rebuilt_even_when_the_scan_fails(monkeypatch):
    ran = []

    def fake_step(name, module, timeout):
        ran.append(module)
        return 1 if module == "src.main" else 0

    monkeypatch.setattr(scheduler, "run_step", fake_step)
    assert run_once() == 1                      # the scan's failure is still reported
    assert ran == ["src.main", "src.website", "src.growth"]   # and the site and the stats came after it


# --- alerts: the owner hears when a step starts failing, and when it works again ---------

class FakeClient:
    """Stands in for the Telegram client: keeps the messages instead of sending them."""

    def __init__(self, fail: bool = False):
        self.sent, self.fail = [], fail

    def send_message(self, text):
        if self.fail:
            raise TelegramError("Could not reach Telegram (ConnectionError)")
        self.sent.append(text)
        return 1


def alerts(client=None):
    return Alerts(client, "FlyFromTirana", "Europe/Tirane")


def test_alert_once_when_a_step_starts_failing_and_once_when_it_works_again():
    client = FakeClient()
    watch = alerts(client)
    watch.report("scan", 0, now=utc(12, 17))
    assert client.sent == []                                   # working: nothing to say

    watch.report("scan", 1, now=utc(15, 17))
    watch.report("scan", 1, now=utc(15, 32))                   # still failing: no second message
    assert len(client.sent) == 1
    assert "the scan failed</b> (10 Oct, 17:17 CEST)" in client.sent[0]   # 15:17 UTC = 17:17 in Tirana
    assert "Exit code 1: the run failed" in client.sent[0]

    watch.report("scan", 0, now=utc(18, 17))
    watch.report("scan", 0, now=utc(18, 32))
    assert len(client.sent) == 2
    assert "the scan works again</b>. It had been failing since 10 Oct, 17:17 CEST" in client.sent[1]


def test_steps_are_followed_separately_and_a_configuration_problem_says_so():
    client = FakeClient()
    watch = alerts(client)
    watch.report("website build", 2, now=utc(15, 17))
    watch.report("scan", 0, now=utc(15, 17))
    assert len(client.sent) == 1 and "the website build failed" in client.sent[0]
    assert "Exit code 2: a configuration problem" in client.sent[0]


def test_alerts_never_stop_the_scans():
    alerts(None).report("scan", 1)                             # no chat set: only logged
    watch = alerts(FakeClient(fail=True))
    watch.report("scan", 1)                                    # Telegram down: logged, no exception
    assert "scan" in watch.failing


def test_alerts_go_to_the_chat_in_the_environment(monkeypatch):
    config = load_config()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    assert Alerts.from_env(config).client is None
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "4242")
    client = Alerts.from_env(config).client
    assert isinstance(client, TelegramClient) and client.chat_id == "4242"


def test_full_run_reports_the_scan_and_the_website_build(monkeypatch):
    monkeypatch.setattr(scheduler, "run_step", lambda name, module, timeout: 1 if module == "src.main" else 0)
    client = FakeClient()
    assert run_once(alerts(client)) == 1
    assert len(client.sent) == 1 and "the scan failed" in client.sent[0]


def test_the_bot_runs_only_with_a_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert scheduler.start_bot(load_config(), threading.Event()) is None
