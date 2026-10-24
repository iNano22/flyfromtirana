"""Growing the free Telegram channel: its join links, the WhatsApp share link, and the
owner's weekly report on both channels.

    python -m src.growth             # what the scheduler runs after every full scan
    python -m src.growth --report    # send the weekly report now, whatever the day
    python -m src.growth --dry-run   # print the report instead (a test from a laptop)

Telegram shows a channel to almost no one by itself: it grows from the places that
advertise it. So every place has its own invite link (invite_links in config.yaml):
the Instagram bio, the Facebook posts, the website and the WhatsApp share text.
A place without one uses the channel's public address (join_link below).

Who joined through which link: Telegram tells the bot, an admin of the channel,
with a "chat_member" update. The bot's listener (src/bot.py, always running in the
scanner container) hands each one to record_member_update, which notes the link's
place and the time, never who it was. Every full run notes how many members each
channel has. Once a week (growth.report_day) the owner's chat gets the numbers: the
members now, the change in the week, and where the free channel's new members came from.
--dry-run only reads the member counts, and saves nothing.
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from src.config import INVITE_SOURCES, PROJECT_ROOT, Config, ConfigError, load_config
from src.deals import Deal
from src.formatter import WORDS, format_dates, format_price
from src.i18n import load_strings
from src.storage import Storage
from src.telegram import TelegramClient, TelegramError

log = logging.getLogger("flyfromtirana.growth")

REPORT = "growth"                    # the weekly report's name in reports_sent
FREE, PREMIUM = "free", "premium"    # the channels' names in channel_members and channel_joins
CHANNEL_LABELS = {FREE: "👥 Kanali falas", PREMIUM: "💎 Premium"}
# How the report names each place. "" = no link of ours: the public address, Telegram's
# search, a forwarded post.
SOURCE_LABELS = {
    "instagram": "📸 Instagram (linku në bio)",
    "facebook": "📘 Facebook",
    "website": "🌐 Faqja",
    "whatsapp": "💬 WhatsApp",
    "meta_ads": "📣 Reklamat në Facebook/Instagram",
    "": "🔗 Pa linkun tonë (adresa publike, kërkimi, postime të përcjella)",
}
MEMBER_STATUSES = ("creator", "administrator", "member")


# --- the links ------------------------------------------------------------------------

def public_url(channel_handle: str) -> str | None:
    """'@flyfromtirana' -> 'https://t.me/flyfromtirana'. A numeric (private) id has no public link."""
    handle = channel_handle.strip().lstrip("@")
    if not handle or handle.lstrip("-").isdigit():
        return None
    return f"https://t.me/{handle}"


def join_link(config: Config, source: str) -> str | None:
    """The link to the free channel for people coming from `source` ("website", "facebook", ...):
    that place's own invite link, or else the channel's public address."""
    return config.invite_links.get(source) or public_url(config.channel_handle)


def short_address(config: Config) -> str:
    """'t.me/flyfromtirana': the public address as people type it (an Instagram caption can't hold a link)."""
    url = public_url(config.channel_handle)
    return url.split("://")[-1] if url else config.channel_handle


def whatsapp_link(deal: Deal, config: Config, language: str, *, city: str | None = None) -> str | None:
    """A link that opens WhatsApp with a message about the deal, to send a friend. The message
    ends with the channel's WhatsApp invite link. None when there is no link to the channel.

    language: the message's (words.share in templates/strings/<language>.yaml). city: the
    city's name in that language, when it isn't the route's English one ("Milano").
    """
    link = join_link(config, "whatsapp")
    if not link:
        return None
    text = load_strings(language).words["share"].format(
        city=city or deal.route.city,
        price=format_price(deal.best.price),
        dates=format_dates([q.depart_date for q in deal.quotes], WORDS[language]["months"]),
        link=link,
    )
    return "https://wa.me/?text=" + quote(text, safe="")


# --- noting the numbers -----------------------------------------------------------------

def channel_chats(config: Config, env: Mapping[str, str]) -> dict[str, str]:
    """Our name for each channel -> its chat id ("@flyfromtirana" or "-100..."), from the environment."""
    chats = {FREE: env.get("TELEGRAM_CHANNEL_ID", "").strip()}
    if config.premium.enabled:
        chats[PREMIUM] = env.get("TELEGRAM_PREMIUM_CHANNEL_ID", "").strip()
    return {name: chat_id for name, chat_id in chats.items() if chat_id}


def record_member_update(update: dict, storage: Storage, config: Config, chats: dict[str, str]) -> bool:
    """Note a join or a leave from one of Telegram's "chat_member" updates. Returns whether it was one.

    chats: see channel_chats. An update seen twice (after a restart) is kept once.
    """
    change = update.get("chat_member") or {}
    channel = channel_name(change.get("chat") or {}, chats)
    joined = membership_change(change)
    if channel is None or joined is None:
        return False   # another chat, or neither a join nor a leave (an admin's rights changed, say)
    source = link_source(change.get("invite_link"), config) if joined else ""
    at = datetime.fromtimestamp(change.get("date", 0), tz=timezone.utc)
    storage.save_join(update["update_id"], channel, source, joined, at)
    return True


def channel_name(chat: dict, chats: dict[str, str]) -> str | None:
    """Our name for the chat an update is about ("free", "premium"), or None for another chat."""
    for name, chat_id in chats.items():
        if chat_id.startswith("@"):
            if chat_id[1:].lower() == str(chat.get("username") or "").lower():
                return name
        elif chat_id == str(chat.get("id")):
            return name
    return None


def membership_change(change: dict) -> bool | None:
    """True when someone joined, False when someone left (or was removed), None for any other change."""
    before = is_member(change.get("old_chat_member") or {})
    after = is_member(change.get("new_chat_member") or {})
    return None if before == after else after


def is_member(member: dict) -> bool:
    status = member.get("status")
    return status in MEMBER_STATUSES or (status == "restricted" and bool(member.get("is_member")))


def link_source(invite: dict | None, config: Config) -> str:
    """The place of the invite link someone joined through ("instagram", ...). "" without a link;
    for a link that isn't one of ours (made by hand in the app, say), its name in the app."""
    if not invite:
        return ""
    name = str(invite.get("name") or "")
    if name in INVITE_SOURCES:
        return name
    for source, link in config.invite_links.items():
        if link == invite.get("invite_link"):
            return source
    return name or "link"


# --- the weekly report ----------------------------------------------------------------

def report_due(config: Config, storage: Storage, local_now: datetime) -> bool:
    """Is it time for the weekly report: the report day, past the report time, and not sent since?"""
    growth = config.growth
    if growth.report_day is None or local_now.weekday() != growth.report_day:
        return False
    due_at = local_now.replace(hour=growth.report_time.hour, minute=growth.report_time.minute,
                               second=0, microsecond=0)
    if local_now < due_at:
        return False
    last = storage.last_report(REPORT)
    return last is None or last < due_at


def report_text(config: Config, storage: Storage, now: datetime, counts: dict[str, int]) -> str:
    """The weekly report (Telegram HTML): each channel's members and their change in the
    last 7 days, then where the free channel's new members came from."""
    zone = ZoneInfo(config.timezone)
    months = WORDS["sq"]["months"]
    week_ago = now - timedelta(days=7)

    def day(moment: datetime) -> str:
        local = moment.astimezone(zone)
        return f"{local.day} {months[local.month - 1]}"

    lines = [f"📈 <b>{html.escape(config.brand)}: Telegram, java deri më {day(now)}</b>", ""]
    for channel, members in counts.items():
        # The first count of the week: a week ago, or the first one ever while there's less history.
        first = storage.member_count_since(channel, week_ago)
        change = f" ({members - first[0]:+d} që nga {day(first[1])})" if first else ""
        lines.append(f"{CHANNEL_LABELS[channel]}: <b>{members}</b> anëtarë{change}")
    lines.append("<i>Numrat përfshijnë ty dhe botin.</i>")

    joins = storage.joins_by_source(FREE, week_ago)
    lines.append("")
    if joins:
        lines.append(f"Të rinj në kanalin falas: <b>{sum(joins.values())}</b>. Nga erdhën:")
        # Our places first, in config order, then any other link, then the joins without a link.
        others = sorted(source for source in joins if source not in SOURCE_LABELS)
        for source in [*INVITE_SOURCES, *others, ""]:
            if joins.get(source):
                label = SOURCE_LABELS.get(source, f"🔗 Linku «{html.escape(source)}»")
                lines.append(f"{label}: {joins[source]}")
    else:
        lines.append("Asnjë anëtar i ri në kanalin falas këtë javë.")
    left = storage.count_leaves(FREE, week_ago)
    if left:
        lines.append(f"🚪 Dolën nga kanali falas: {left}")
    return "\n".join(lines)


# --- one run ---------------------------------------------------------------------------

def run(config: Config, env: Mapping[str, str], *, report_now: bool = False, dry_run: bool = False,
        now: datetime | None = None, session: requests.Session | None = None,
        output: Callable[[str], None] = print) -> int:
    """Note the member counts, and send the weekly report when it is due.

    env: the environment variables (os.environ). `now`, `session` and `output` are only
    passed in by tests.
    """
    now = now or datetime.now(timezone.utc)
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = channel_chats(config, env)
    if not token or FREE not in chats:
        raise ConfigError("the channels' numbers need TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID")
    # The report goes where the social drafts go: usually the owner's own chat with the bot.
    owner = env.get("TELEGRAM_DRAFTS_CHAT_ID", "").strip() or env.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
    session = session or requests.Session()

    def client(chat_id: str) -> TelegramClient:
        return TelegramClient(token, chat_id, session)

    with Storage(config.db_path) as storage:
        counts = {name: client(chat_id).member_count() for name, chat_id in chats.items()}
        log.info("Members: %s", ", ".join(f"{name} {members}" for name, members in counts.items()))
        if not dry_run:
            for name, members in counts.items():
                storage.save_member_count(name, members, now)

        if not (report_now or dry_run or report_due(config, storage, now.astimezone(ZoneInfo(config.timezone)))):
            return 0
        text = report_text(config, storage, now, counts)
        if dry_run:
            output(text)
            return 0
        if not owner:
            log.warning("Neither TELEGRAM_DRAFTS_CHAT_ID nor TELEGRAM_ALERT_CHAT_ID is set: no weekly report")
            return 0
        client(owner).send_message(text)
        storage.record_report(REPORT, now)
        log.info("Sent the weekly report")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Note the Telegram channels' numbers; send the weekly report.")
    parser.add_argument("--report", action="store_true", help="send the weekly report now, whatever the day")
    parser.add_argument("--dry-run", action="store_true", help="print the report instead of sending it; save nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # urllib3's debug lines include request URLs, and the Telegram URL contains the bot token.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv(PROJECT_ROOT / ".env")   # does nothing if there's no .env (e.g. on the server)
    try:
        return run(load_config(), os.environ, report_now=args.report, dry_run=args.dry_run)
    except ConfigError as exc:
        log.error("Configuration problem: %s", exc)
        return 2
    except TelegramError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
