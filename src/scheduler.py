"""Runs the scan on a server, on the schedule GitHub Actions used to keep.

    python -m src.scheduler            # run forever: scan + rebuild the site every full_run_every_minutes
    python -m src.scheduler --once     # one scan + site build now, then exit

Every slot does what the old workflow did: `python -m src.main`, then
`python -m src.website` whatever the scan's result (prices saved before a
failure still belong on the page). Runs never overlap, and on SIGTERM a run in
progress is allowed to finish, so a redeploy can't cut a post off between
"sent" and "remembered as sent".

After each full run, `python -m src.growth` notes who joined the Telegram
channels through which invite link and how many members they have, and sends
the owner the weekly report when it is due.

Between those full runs, premium gets its own quick scan every
premium.scan_every_minutes (config.yaml): `python -m src.main --premium-only`,
which posts new deals to the premium channel, makes the Instagram/Facebook post
whose time has come (social.post_times), and nothing else. The free channel and
the website keep the full runs' rhythm.

The database no longer travels through git. On the server it lives in a Docker
volume; the copy baked into the image (seed/prices.db) is only the starting
point for a brand-new volume.

When a scan or a website build fails, the owner hears about it on Telegram
(Alerts below), once when it starts failing and once when it works again.

Alongside, a thread runs the bot's chat (src/bot.py): people set their price
alerts there, and it notes who joins the channels.
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.bot import Bot
from src.config import PROJECT_ROOT, Config, ConfigError, load_config
from src.growth import channel_chats
from src.telegram import TelegramClient, TelegramError

log = logging.getLogger("flyfromtirana.scheduler")

# Full runs start full_run_every_minutes apart (config.yaml), counted from minute 17
# past midnight UTC: at 180 that is the old cron ("17 */3 * * *"), which kept off the
# top of the hour; at 15 it is minutes 2, 17, 32 and 47 of every hour.
SLOT_MINUTE = 17

SCAN_TIMEOUT_SECONDS = 20 * 60   # the old workflow's timeout-minutes
SITE_TIMEOUT_SECONDS = 5 * 60
STATS_TIMEOUT_SECONDS = 2 * 60
TICK_SECONDS = 20

SEED_DB = PROJECT_ROOT / "seed" / "prices.db"

# The chat that hears about failures: your own chat with the bot (press Start in it first)
# or a private channel where the bot is an admin. Not set = failures are only logged.
ALERT_CHAT_ENV = "TELEGRAM_ALERT_CHAT_ID"
# What an exit code means, for the alert (src/main.py; run_step gives 1 for a timeout too).
EXIT_MEANING = {
    1: "the run failed: the price API or Telegram didn't answer, a post was refused, or it took too long",
    2: "a configuration problem: a missing variable or a mistake in config.yaml",
}


def next_slot(after: datetime, every: timedelta = timedelta(hours=3)) -> datetime:
    """The first slot strictly after `after`, with slots `every` apart (a whole number of them in a day)."""
    after = after.astimezone(timezone.utc)
    midnight = after.replace(hour=0, minute=0, second=0, microsecond=0)
    first = midnight + timedelta(minutes=SLOT_MINUTE) % every   # the day's first slot
    passed = (after - first) // every                            # slots up to `after` (-1 before the first)
    return first + (passed + 1) * every


def premium_scan_interval(config: Config) -> timedelta | None:
    """How often premium gets a quick scan between full runs, or None when that's off."""
    premium = config.premium
    if premium.enabled and premium.scan_every_minutes > 0:
        return timedelta(minutes=premium.scan_every_minutes)
    return None


def next_run(last_start: datetime, quick_every: timedelta | None,
             full_every: timedelta = timedelta(hours=3)) -> tuple[datetime, bool]:
    """When the next run is due after one that started at `last_start`, and whether it is a full run.

    Full runs keep their slots. In between, a quick premium scan is due
    `quick_every` after the last run; one that would land on the next slot
    (or past it) gives way to the full run, which posts to premium as well.
    """
    full_at = next_slot(last_start, full_every)
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


class Alerts:
    """Tells the owner on Telegram when a step starts failing, and when it works again.

    The steps are "scan" (the full scan and the quick premium scans), "website build" and
    "channel stats".
    One message when a step fails after working (or on its first run after a start), one
    when it works again: a long outage is two messages, not one every 15 minutes. Without
    TELEGRAM_ALERT_CHAT_ID nothing is sent and failures are only logged.
    """

    def __init__(self, client: TelegramClient | None, brand: str, timezone_name: str):
        self.client = client
        self.brand = html.escape(brand)
        self.zone = ZoneInfo(timezone_name)
        self.failing: dict[str, datetime] = {}   # step -> when it started failing

    @classmethod
    def from_env(cls, config: Config) -> Alerts:
        """Alerts to TELEGRAM_ALERT_CHAT_ID, sent by the channel's bot; none when it isn't set."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get(ALERT_CHAT_ENV, "").strip()
        if not (token and chat):
            log.info("%s is not set: a failed run is only logged", ALERT_CHAT_ENV)
            return cls(None, config.brand, config.timezone)
        return cls(TelegramClient(token, chat), config.brand, config.timezone)

    def report(self, step: str, code: int, now: datetime | None = None) -> None:
        """Note how a step ended (its exit code), and send a message when that is news."""
        now = now or datetime.now(timezone.utc)
        if code != 0 and step not in self.failing:
            self.failing[step] = now
            meaning = EXIT_MEANING.get(code, "an unexpected error")
            self.send(f"⚠️ <b>{self.brand}: the {step} failed</b> ({self.when(now)}).\n"
                      f"Exit code {code}: {meaning}. The scanner's logs say more.")
        elif code == 0 and step in self.failing:
            since = self.failing.pop(step)
            self.send(f"✅ <b>{self.brand}: the {step} works again</b>. It had been failing since {self.when(since)}.")

    def when(self, moment: datetime) -> str:
        """'13 Oct, 17:17 CEST': the time in the configured timezone, with that zone's short name."""
        local = moment.astimezone(self.zone)
        return f"{local.day} {local:%b}, {local:%H:%M %Z}"

    def send(self, text: str) -> None:
        if self.client is None:
            return
        try:
            self.client.send_message(text)
        except TelegramError as exc:   # an alert that can't be sent must never stop the scans
            log.error("Could not send the alert to %s: %s", ALERT_CHAT_ENV, exc)


def run_once(alerts: Alerts | None = None) -> int:
    """One scan, then the website, then the channels' numbers (src/growth.py: the joins, the
    member counts, and the weekly report when it is due). Each step runs whatever the others did."""
    scan = run_step("scan", "src.main", SCAN_TIMEOUT_SECONDS)
    site = run_step("website build", "src.website", SITE_TIMEOUT_SECONDS)
    stats = run_step("channel stats", "src.growth", STATS_TIMEOUT_SECONDS)
    if alerts:
        alerts.report("scan", scan)
        alerts.report("website build", site)
        alerts.report("channel stats", stats)
    return scan or site or stats


def start_bot(config: Config, stop: threading.Event) -> threading.Thread | None:
    """Run the bot's chat (src/bot.py) in a thread of its own until `stop` is set. None without a bot token."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.info("TELEGRAM_BOT_TOKEN is not set: the bot's chat is off")
        return None
    bot = Bot(config, token, channel_chats(config, os.environ))
    # daemon: a stop that comes while the bot waits for news doesn't wait for it to finish.
    thread = threading.Thread(target=bot.listen, args=(stop,), name="bot", daemon=True)
    thread.start()
    return thread


def run_premium_scan() -> int:
    """The quick scan between full runs: new deals go to the premium channel straight away."""
    return run_step("premium scan", "src.main", SCAN_TIMEOUT_SECONDS, "--premium-only")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan and rebuild the website every full_run_every_minutes.")
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
    bot_stop = threading.Event()

    def stop(signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        bot_stop.set()
        log.info("%s received: stopping after the current run, if one is in progress",
                 signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    alerts = Alerts.from_env(config)
    start_bot(config, bot_stop)
    # So the page exists right after a deploy instead of at the next slot.
    alerts.report("website build", run_step("website build", "src.website", SITE_TIMEOUT_SECONDS))

    full_every = timedelta(minutes=config.full_run_every_minutes)
    log.info("A full run every %d minutes", config.full_run_every_minutes)
    quick_every = premium_scan_interval(config)
    if quick_every:
        log.info("Premium gets a quick scan every %g minutes between the full runs",
                 quick_every.total_seconds() / 60)

    def plan(last_start: datetime) -> tuple[datetime, bool]:
        due, full = next_run(last_start, quick_every, full_every)
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
            run_once(alerts)
        else:
            alerts.report("scan", run_premium_scan())   # the same "scan" as the full run's
        due, full = plan(started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
