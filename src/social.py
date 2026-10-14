"""Social media posts: the pictures and the caption of a deal for Instagram and Facebook.

For a deal the free channel gets, the bot draws the feed card and the story card
(src/cards.py) and writes the captions (templates/<language>_social.txt for
Instagram and the drafts, <language>_facebook.txt for the Page, which can hold
the booking link). Then one of two things happens (main.publish_social decides):

  - With META_PAGE_TOKEN set, the bot posts them itself (src/meta.py) on the
    platforms in social.publish. Instagram only fetches pictures from a public
    address, so the cards are first put on the website, under /social/. The
    owner's chat (TELEGRAM_DRAFTS_CHAT_ID) gets a report with the posts' links.
  - Otherwise the owner's chat gets a draft to post by hand: the two pictures as
    files (so Telegram doesn't shrink them), the caption as a message to copy,
    and the booking link for the Facebook post as a message of its own.
    A post that failed gets the same draft, so it can still go out by hand.

Posts are counted and deduped like the channels' posts, under the name
"social" in posted_deals (storage.py).
"""
from __future__ import annotations

import html
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.cards import save_cards
from src.config import Config
from src.deals import Deal
from src.formatter import format_post, format_price
from src.links import LinkBuilder
from src.meta import MetaClient, MetaError
from src.photos import PostPhoto
from src.telegram import TelegramClient

log = logging.getLogger(__name__)

SOCIAL_CHANNEL = "social"   # its name in posted_deals
PICTURES_FOLDER = "social"  # under the website's folder: flyfromtirana.al/social/<picture>.jpg


@dataclass(frozen=True)
class Draft:
    deal: Deal
    cards: dict[str, Path]      # {"feed": .../bgy_feed.jpg, "story": .../bgy_story.jpg}
    caption: str                # plain text for Instagram (no link: "linku në bio")
    facebook_text: str          # plain text for the Page, with the booking link
    booking_link: str           # the flight link (SubID: social.sub_id)


def make_draft(deal: Deal, config: Config, links: LinkBuilder, out_dir: Path, *,
               photo: PostPhoto | None) -> Draft:
    """Draw the cards into out_dir and write both captions. `links` carries the social SubID."""
    cards = save_cards(deal, out_dir, airline_names=config.airlines, brand=config.brand,
                       handle=config.social.handle or config.channel_handle, address=site_address(config),
                       photo=photo.path if photo else None, language=config.language)
    booking_link = links.flight(deal.best)
    extra = {"HASHTAGS": config.social.hashtags, "WEBSITE": site_address(config), "BOOKING_LINK": booking_link}

    def caption(template: str) -> str:
        # format_post writes Telegram HTML (values escaped); the platforms want plain text.
        text = format_post(deal, links, language=config.language, channel_handle=config.channel_handle,
                           airline_names=config.airlines, template=f"{config.language}_{template}",
                           website_url=config.website.url, photo=photo, extra=extra)
        return html.unescape(text)

    return Draft(deal=deal, cards=cards, caption=caption("social"), facebook_text=caption("facebook"),
                 booking_link=booking_link)


# --- posting by the bot ----------------------------------------------------------------

def put_pictures_online(cards: dict[str, Path], folder: Path, site_url: str, now: datetime) -> dict[str, str]:
    """Copy the cards into the website's /social/ folder. Returns their public addresses.

    The names carry the moment of posting (bgy_20261015-0817_feed.jpg), so a route
    posted twice keeps both pictures until prune_pictures() removes the old ones.
    """
    folder.mkdir(parents=True, exist_ok=True)
    base = site_url.rstrip("/") + "/" + PICTURES_FOLDER + "/"
    urls = {}
    for kind, path in cards.items():
        name = f"{path.stem.split('_')[0]}_{now:%Y%m%d-%H%M}_{kind}.jpg"
        shutil.copy2(path, folder / name)
        urls[kind] = base + name
    return urls


def prune_pictures(folder: Path, older_than: datetime) -> int:
    """Delete posted pictures older than `older_than`. Returns how many went."""
    if not folder.is_dir():
        return 0
    deleted = 0
    for path in folder.glob("*.jpg"):
        if datetime.fromtimestamp(path.stat().st_mtime, tz=older_than.tzinfo) < older_than:
            path.unlink()
            deleted += 1
    return deleted


def post_on_platforms(draft: Draft, meta: MetaClient, platforms: tuple[str, ...],
            pictures: dict[str, str]) -> list[tuple[str, str | None | MetaError]]:
    """Post the draft on each platform. Returns one (what, outcome) per post made:
    the post's address, None for a story (it has no address), or the MetaError."""
    outcomes: list[tuple[str, str | None | MetaError]] = []
    for platform in platforms:
        if platform == "instagram":
            try:
                outcomes.append(("Instagram", meta.post_instagram(pictures["feed"], draft.caption)))
            except MetaError as exc:
                outcomes.append(("Instagram", exc))
                continue   # no story without the post
            try:
                outcomes.append(("Story", meta.post_instagram(pictures["story"], story=True)))
            except MetaError as exc:
                outcomes.append(("Story", exc))
        elif platform == "facebook":
            try:
                outcomes.append(("Facebook", meta.post_facebook(pictures["feed"], draft.facebook_text)))
            except MetaError as exc:
                outcomes.append(("Facebook", exc))
    return outcomes


def failed(outcomes: list[tuple[str, str | None | MetaError]]) -> list[str]:
    """The names of the posts that did not go out."""
    return [what for what, outcome in outcomes if isinstance(outcome, MetaError)]


def report(draft: Draft, outcomes: list[tuple[str, str | None | MetaError]]) -> str:
    """The Telegram message (HTML) about the posts made: a link for each, or what went wrong."""
    problems = failed(outcomes)
    head = "⚠️ <b>Postim i pjesshëm</b>" if problems else "✅ <b>U postua</b>"
    if len(problems) == len(outcomes):
        head = "❌ <b>Postimi dështoi</b>"
    lines = [f"{head} · {deal_title(draft.deal)}"]
    icons = {"Instagram": "📸", "Story": "📱", "Facebook": "📘"}
    for what, outcome in outcomes:
        if isinstance(outcome, MetaError):
            lines.append(f"❌ {what}: {html.escape(str(outcome))}")
        elif outcome:
            lines.append(f'{icons.get(what, "")} {what}: <a href="{html.escape(outcome, quote=True)}">{html.escape(outcome)}</a>')
        else:
            lines.append(f"{icons.get(what, '')} {what}: ✅")
    if problems:
        lines.append("\nPostoje me dorë nga drafti më poshtë.")
    return "\n".join(lines)


# --- the draft for posting by hand -----------------------------------------------------

def send_draft(telegram: TelegramClient, draft: Draft) -> int:
    """Send the draft to the owner's chat: the files, the caption, the link. Returns the last message_id."""
    telegram.send_files([draft.cards["feed"], draft.cards["story"]], header(draft))
    telegram.send_message(html.escape(draft.caption, quote=False))
    return telegram.send_message(
        "🔗 Linku i rezervimit, për postimin në <b>Facebook</b> (në Instagram vendose në bio: "
        "në përshkrim nuk klikohet):\n" + html.escape(draft.booking_link, quote=True))


def header(draft: Draft) -> str:
    """The text under the two files: which deal this is, and what to do with it."""
    return (f"📸 <b>Draft për Instagram dhe Facebook</b>\n{deal_title(draft.deal)}\n\n"
            "1️⃣ Ruaj të dy fotot në galeri: feed (4:5) dhe story (9:16)\n"
            "2️⃣ Kopjo tekstin nga mesazhi tjetër\n"
            "3️⃣ Në Facebook shto edhe linkun e rezervimit (mesazhi i fundit)")


def deal_title(deal: Deal) -> str:
    """'TIRANA → MILAN (Bergamo) nga €14', HTML-escaped."""
    route = deal.route
    where = f"TIRANA → {html.escape(route.city.upper())}" + (f" ({html.escape(route.airport)})" if route.airport else "")
    return f"{where} nga €{format_price(deal.best.price)}"


def site_address(config: Config) -> str:
    """The site's address as people write it: "https://flyfromtirana.al/" -> "flyfromtirana.al"."""
    return config.website.url.split("://")[-1].strip("/")
