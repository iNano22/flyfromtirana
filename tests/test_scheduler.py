"""The server scheduler: slot times, database seeding, and scan-then-website order."""
from datetime import datetime, timedelta, timezone

import pytest

from src import scheduler
from src.scheduler import next_slot, run_once, seed_database


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
    assert ran == ["src.main", "src.website"]   # and the site was built after it
