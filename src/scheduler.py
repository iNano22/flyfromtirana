"""Runs the scan on a server, on the schedule GitHub Actions used to keep.

    python -m src.scheduler            # run forever: scan + rebuild the site every 3 hours
    python -m src.scheduler --once     # one scan + site build now, then exit

Every slot does what the old workflow did: `python -m src.main`, then
`python -m src.website` whatever the scan's result (prices saved before a
failure still belong on the page). Runs never overlap, and on SIGTERM a run in
progress is allowed to finish, so a redeploy can't cut a post off between
"sent" and "remembered as sent".

Between those full runs, premium gets its own quick scan every
premium.scan_every_minutes (config.yaml): `python -m src.main --premium-only`,
which posts new deals to the premium channel and nothing else. The free
channel and the website keep the 3-hour rhythm.

The database no longer travels through git. On the server it lives in a Docker
volume; the copy baked into the image (seed/prices.db) is only the starting
point for a brand-new volume.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import PROJECT_ROOT, Config, ConfigError, load_config

log = logging.getLogger("flyfromtirana.scheduler")

# Minute 17 of every third hour, UTC: the old cron ("17 */3 * * *"), which kept
# off the top of the hour. Kept so the posting rhythm doesn't change.
SLOT_HOURS = 3
SLOT_MINUTE = 17

SCAN_TIMEOUT_SECONDS = 20 * 60   # the old workflow's timeout-minutes
SITE_TIMEOUT_SECONDS = 5 * 60
TICK_SECONDS = 20

SEED_DB = PROJECT_ROOT / "seed" / "prices.db"


def next_slot(after: datetime) -> datetime:
    """The first slot strictly after `after`."""
    after = after.astimezone(timezone.utc)
    slot = after.replace(hour=after.hour - after.hour % SLOT_HOURS, minute=SLOT_MINUTE,
                         second=0, microsecond=0)
    while slot <= after:
        slot += timedelta(hours=SLOT_HOURS)
    return slot


def premium_scan_interval(config: Config) -> timedelta | None:
    """How often premium gets a quick scan between full runs, or None when that's off."""
    premium = config.premium
    if premium.enabled and premium.scan_every_minutes > 0:
        return timedelta(minutes=premium.scan_every_minutes)
    return None


def next_run(last_start: datetime, quick_every: timedelta | None) -> tuple[datetime, bool]:
    """When the next run is due after one that started at `last_start`, and whether it is a full run.

    Full runs keep their slots. In between, a quick premium scan is due
    `quick_every` after the last run; one that would land on the next slot
    (or past it) gives way to the full run, which posts to premium as well.
    """
    full_at = next_slot(last_start)
    if quick_every is None or last_start + quick_every >= full_at:
        return full_at, True
    return last_start + quick_every, False


def seed_database(db_path: Path, seed: Path = SEED_DB) -> bool:
    """Start a brand-new volume from the price history shipped in the image.

    Without it the first run would have no posted_deals and repost everything
    from the last week. An existing database is never touched.
    """
    if db_path.exists() or not seed.exists():
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed, db_path)
    log.info("No database at %s yet: started it from %s", db_path, seed)
    return True


def run_step(name: str, module: str, timeout: int, *args: str) -> int:
    """Run `python -m <module> <args>` from the repo root. Returns its exit code (1 on timeout)."""
    log.info("Starting %s", name)
    try:
        code = subprocess.run([sys.executable, "-m", module, *args], cwd=PROJECT_ROOT,
                              timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log.error("%s was stopped after %d minutes", name, timeout // 60)
        return 1
    log.log(logging.INFO if code == 0 else logging.ERROR, "%s finished with exit code %d", name, code)
    return code


def run_once() -> int:
    """One scan, then the website. The site is rebuilt even after a failed scan."""
    scan = run_step("scan", "src.main", SCAN_TIMEOUT_SECONDS)
    site = run_step("website build", "src.website", SITE_TIMEOUT_SECONDS)
    return scan or site


def run_premium_scan() -> int:
    """The quick scan between full runs: new deals go to the premium channel straight away."""
    return run_step("premium scan", "src.main", SCAN_TIMEOUT_SECONDS, "--premium-only")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan and rebuild the website every 3 hours.")
    parser.add_argument("--once", action="store_true", help="one scan + site build now, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        config = load_config()
        seed_database(config.db_path)
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return 2
    if args.once:
        return run_once()

    stopping = False

    def stop(signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        log.info("%s received: stopping after the current run, if one is in progress",
                 signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # So the page exists right after a deploy instead of at the next slot.
    run_step("website build", "src.website", SITE_TIMEOUT_SECONDS)

    quick_every = premium_scan_interval(config)
    if quick_every:
        log.info("Premium gets a quick scan every %g minutes between the full runs",
                 quick_every.total_seconds() / 60)

    def plan(last_start: datetime) -> tuple[datetime, bool]:
        due, full = next_run(last_start, quick_every)
        log.info("Next %s at %s", "full run" if full else "premium scan", due.strftime("%Y-%m-%d %H:%M UTC"))
        return due, full

    due, full = plan(datetime.now(timezone.utc))
    # A short tick against the wall clock rather than one long sleep, so a
    # stalled timer or a clock step delays a run instead of dropping it.
    while not stopping:
        time.sleep(TICK_SECONDS)
        started = datetime.now(timezone.utc)
        if started < due:
            continue
        if full:
            run_once()
        else:
            run_premium_scan()
        due, full = plan(started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
