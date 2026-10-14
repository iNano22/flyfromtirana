"""Social media drafts: the pictures and the caption for Instagram and Facebook.

Phase 1 of the social media plan, where the bot prepares each post and you
publish it by hand. For a deal the free channel gets, the bot:

  1. draws the feed card and the story card (src/cards.py),
  2. writes the caption from templates/<language>_social.txt,
  3. sends them to the drafts chat (TELEGRAM_DRAFTS_CHAT_ID: your own chat with
     the bot, or a private channel it posts to): the two pictures as files (so
     Telegram doesn't shrink them), the caption as a message to copy, and the
     booking link for the Facebook post as a message of its own (an Instagram
     caption can't hold a link; its readers use the link in the bio).

Drafts are deduped and counted like the channels' posts, under the name
"social" in posted_deals (storage.py). main.publish_social decides which deals
get a draft; this file only makes and sends one.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

from src.cards import save_cards
from src.config import Config
from src.deals import Deal
from src.formatter import format_post, format_price
from src.links import LinkBuilder
from src.photos import PostPhoto
from src.telegram import TelegramClient

SOCIAL_CHANNEL = "social"   # its name in posted_deals


@dataclass(frozen=True)
class Draft:
    deal: Deal
    cards: dict[str, Path]      # {"feed": .../bgy_feed.jpg, "story": .../bgy_story.jpg}
    caption: str                # the caption, ready to paste under the post
    booking_link: str           # the flight link for the Facebook post (SubID: social.sub_id)


def make_draft(deal: Deal, config: Config, links: LinkBuilder, out_dir: Path, *,
               photo: PostPhoto | None) -> Draft:
    """Draw the cards into out_dir and write the caption. `links` carries the social SubID."""
    cards = save_cards(deal, out_dir, airline_names=config.airlines, brand=config.brand,
                       handle=config.social.handle or config.channel_handle, address=site_address(config),
                       photo=photo.path if photo else None, language=config.language)
    caption = format_post(deal, links, language=config.language, channel_handle=config.channel_handle,
                          airline_names=config.airlines, template=f"{config.language}_social",
                          website_url=config.website.url, photo=photo,
                          extra={"HASHTAGS": config.social.hashtags, "WEBSITE": site_address(config)})
    return Draft(deal=deal, cards=cards, caption=caption, booking_link=links.flight(deal.best))


def send_draft(telegram: TelegramClient, draft: Draft) -> int:
    """Send the draft to the drafts chat: the files, the caption, the link. Returns the last message_id."""
    telegram.send_files([draft.cards["feed"], draft.cards["story"]], header(draft))
    telegram.send_message(draft.caption)
    return telegram.send_message(
        "🔗 Linku i rezervimit, për postimin në <b>Facebook</b> (në Instagram vendose në bio: "
        "në përshkrim nuk klikohet):\n" + html.escape(draft.booking_link, quote=True))


def header(draft: Draft) -> str:
    """The text under the two files: which deal this is, and what to do with it."""
    route = draft.deal.route
    where = f"TIRANA → {html.escape(route.city.upper())}" + (f" ({html.escape(route.airport)})" if route.airport else "")
    return (f"📸 <b>Draft për Instagram dhe Facebook</b>\n{where} nga €{format_price(draft.deal.best.price)}\n\n"
            "1️⃣ Ruaj të dy fotot në galeri: feed (4:5) dhe story (9:16)\n"
            "2️⃣ Kopjo tekstin nga mesazhi tjetër\n"
            "3️⃣ Në Facebook shto edhe linkun e rezervimit (mesazhi i fundit)")


def site_address(config: Config) -> str:
    """The site's address as people write it: "https://flyfromtirana.al/" -> "flyfromtirana.al"."""
    return config.website.url.split("://")[-1].strip("/")
