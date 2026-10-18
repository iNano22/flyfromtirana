"""Social media posts: the pictures and the captions for Instagram and Facebook.

Three kinds of post, all made the same way (main.publish_social decides which one
goes out at each posting time, social.post_times):

  - the best deal on the website the accounts haven't had: the deal's feed card
    and story card (src/cards.py) with the captions from
    templates/<language>_social.txt (Instagram) and <language>_facebook.txt (the
    Page, which can hold the booking link). With social.reels on, Instagram gets
    the deal as a Reel (src/reels.py: the card brought to life, with music) instead
    of the feed card; the story and the Facebook post keep the pictures;
  - the week's top deals, one day a week: a cover card plus each deal's card,
    posted together as a carousel;
  - a city guide, on a day with no post at all: the guide's cover card, a card
    per section of the guide (sights, plan, areas, food, airports, tips) as one
    carousel, and a story, all pointing at the city's page on the website.

Then one of two things happens:

  - With META_PAGE_TOKEN set, the bot posts them itself (src/meta.py) on the
    platforms in social.publish. Instagram only fetches pictures from a public
    address, so the pictures are first put on the website, under /social/.
    The owner's chat (TELEGRAM_DRAFTS_CHAT_ID) gets a report with the posts' links.
  - Otherwise the owner's chat gets a draft to post by hand: the pictures as
    files (so Telegram doesn't shrink them), the caption as a message to copy,
    and the link for the Facebook post as a message of its own. A post that
    failed gets the same draft, so it can still go out by hand.

Posts are counted and deduped like the channels' posts, in posted_deals
(storage.py), under the names "social" (deals), "social_weekly" and "social_guide".
"""
from __future__ import annotations

import html
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.cards import draw_card, draw_guide_card, draw_list_card, draw_top_card, save_cards, save_jpeg
from src.config import Config
from src.deals import Deal
from src.formatter import WORDS, format_dates, format_post, format_price
from src.growth import join_link, short_address
from src.guides import Guide
from src.links import LinkBuilder
from src.meta import MetaClient, MetaError
from src.photos import PostPhoto
from src.reels import ReelError, make_reel
from src.telegram import TelegramClient

log = logging.getLogger(__name__)

SOCIAL_CHANNEL = "social"           # the deal posts' name in posted_deals
WEEKLY_CHANNEL = "social_weekly"    # the week's top deals
GUIDE_CHANNEL = "social_guide"      # the city guides (one row per city: the destination code)
PICTURES_FOLDER = "social"          # under the website's folder: flyfromtirana.al/social/<picture>.jpg
WEEKLY_MIN_DEALS = 3                # fewer deals than this: no "top of the week"


@dataclass(frozen=True)
class Post:
    """One social media post, ready to go out or to be sent as a draft."""

    kind: str                   # "deal", "weekly" or "guide": the first part of the pictures' names
    title: str                  # for the report and the draft, e.g. "TIRANA → MILAN (Bergamo) nga €14"
    pictures: list[Path]        # the feed pictures in order: one = a plain post, several = a carousel
    story: Path | None          # the story card, when there is one
    caption: str                # plain text for Instagram (no link: "linku në bio")
    facebook_text: str          # plain text for the Page, with the link
    link: str                   # the link the draft gives for the Facebook post
    video: Path | None = None   # the Reel (MP4): Instagram gets it instead of the feed picture
    cover: Path | None = None   # the Reel's cover picture, shown in the profile grid


@dataclass(frozen=True)
class Online:
    """The public addresses of a post's files on the website (Instagram fetches them from there)."""

    feed: list[str]             # the feed pictures, in order
    story: str | None
    video: str | None = None
    cover: str | None = None


# --- making the posts ----------------------------------------------------------------

def deal_post(deal: Deal, config: Config, links: LinkBuilder, out_dir: Path, *, photo: PostPhoto | None,
              music: tuple[Path, float] | None = None) -> Post:
    """A deal's post: its two cards, its Reel (with social.reels on) and the captions.

    `links` carries the social SubID. music: the Reel's track (reels.pick_music), or None.
    A Reel that can't be made only leaves the post a picture, as before.
    """
    cards = save_cards(deal, out_dir, airline_names=config.airlines, brand=config.brand, handle=handle(config),
                       address=site_address(config), photo=photo.path if photo else None, language=config.language)
    video = cover = None
    if config.social.reels:
        try:
            video, cover = make_reel(deal, out_dir, airline_names=config.airlines, brand=config.brand,
                                     handle=handle(config), address=site_address(config),
                                     telegram=short_address(config), photo=photo.path if photo else None,
                                     language=config.language, music=music)
        except ReelError as exc:
            log.warning("[social] No Reel for %s, the post is a picture: %s", deal.route.iata, exc)
    booking_link = links.flight(deal.best)
    extra = {"HASHTAGS": config.social.hashtags, "WEBSITE": site_address(config), "BOOKING_LINK": booking_link}

    def caption(template: str, telegram: str | None) -> str:
        # format_post writes Telegram HTML (values escaped); the platforms want plain text.
        text = format_post(deal, links, language=config.language, channel_handle=config.channel_handle,
                           airline_names=config.airlines, template=f"{config.language}_{template}",
                           website_url=config.website.url, photo=photo, extra=extra | {"TELEGRAM": telegram})
        return html.unescape(text)

    return Post(kind="deal", title=deal_title(deal), pictures=[cards["feed"]], story=cards["story"],
                caption=caption("social", instagram_channel(config)),
                facebook_text=caption("facebook", facebook_channel(config)), link=booking_link,
                video=video, cover=cover)


def weekly_post(deals: list[Deal], config: Config, out_dir: Path, *, photos: dict[str, PostPhoto | None],
                now: datetime) -> Post:
    """The week's top deals: a cover card and each deal's card, as one carousel.

    deals: the best first, at most social.weekly_count. photos: each route's photo by airport code.
    """
    deals = deals[: config.social.weekly_count]
    words = WORDS[config.language]
    first = photos.get(deals[0].route.iata) if deals else None
    cover = save_jpeg(draw_top_card(deals, brand=config.brand, handle=handle(config), address=site_address(config),
                                    photo=first.path if first else None, title=f"TOP {len(deals)}"),
                      out_dir / "weekly_cover.jpg")
    pictures = [cover]
    lines = []
    for number, deal in enumerate(deals, start=1):
        photo = photos.get(deal.route.iata)
        card = draw_card(deal, "feed", airline_names=config.airlines, brand=config.brand, handle=handle(config),
                         address=site_address(config), photo=photo.path if photo else None, language=config.language)
        pictures.append(save_jpeg(card, out_dir / f"weekly_{number}_{deal.route.iata.lower()}.jpg"))
        where = deal.route.city + (f" ({deal.route.airport})" if deal.route.airport else "")
        when = format_dates([q.depart_date for q in deal.quotes], words["months"])
        lines.append(f"{number}️⃣ {where} nga €{format_price(deal.best.price)} · {when}")
    head = f"🏆 TOP {len(deals)} OFERTAT E JAVËS nga Tirana ({now:%d.%m})\n\n" + "\n".join(lines) + "\n\n"

    def tail(telegram: str) -> str:
        return (f"🔔 Ofertat dalin të parat në Telegram: {telegram}\n⏳ Çmimet ndryshojnë shpejt!"
                + hashtags(config))

    caption = head + f"👉 Rezervo nga linku në bio: {site_address(config)}\n" + tail(instagram_channel(config))
    facebook_text = (head + f"👉 Të gjitha ofertat dhe rezervimi: {config.website.url}\n"
                     + tail(facebook_channel(config)))
    return Post(kind="weekly", title=f"TOP {len(deals)} ofertat e javës", pictures=pictures, story=None,
                caption=caption, facebook_text=facebook_text, link=config.website.url)


def guide_post(guide: Guide, iata: str, price: float | None, config: Config, out_dir: Path, *,
               photo: PostPhoto | None, page_url: str) -> Post:
    """A city guide's post: its cover card plus a card per section (a carousel), and a story.

    iata: the city's route to show (its photo and code). price: the cheapest current
    flight to the city, if any. page_url: the guide page's full address.
    """
    page = page_url.split("://")[-1].rstrip("/") + "/"   # "flyfromtirana.al/rome/"
    cover = save_jpeg(draw_guide_card("feed", city=guide.display_name, iata=iata, tagline=guide.tagline, price=price,
                                      page=page, photo=photo.path if photo else None, brand=config.brand,
                                      handle=handle(config)), out_dir / f"guide_{guide.slug}_feed.jpg")
    story = save_jpeg(draw_guide_card("story", city=guide.display_name, iata=iata, tagline=guide.tagline, price=price,
                                      page=page, photo=photo.path if photo else None, brand=config.brand,
                                      handle=handle(config)), out_dir / f"guide_{guide.slug}_story.jpg")
    pictures = [cover]
    sections = guide_sections(guide)
    for number, (key, heading, entries) in enumerate(sections, start=2):
        kicker = f"{guide.display_name.upper()}  ·  {number}/{len(sections) + 1}"
        card = draw_list_card(heading, kicker, entries, page=page, brand=config.brand, handle=handle(config))
        pictures.append(save_jpeg(card, out_dir / f"guide_{guide.slug}_{number}_{key}.jpg"))

    flights = f"✈️ Fluturime nga Tirana nga €{format_price(price)}\n" if price is not None else ""
    taste = ""
    if guide.sights:
        taste += "👀 Mos humb: " + ", ".join(entry.name for entry in guide.sights[:3]) + "\n"
    if guide.food:
        taste += "🍴 Provo: " + ", ".join(entry.name for entry in guide.food[:2]) + "\n"
    head = f"📍 {guide.display_name.upper()}: {guide.tagline}\n\n{guide.intro}\n\n{taste}{flights}"

    def tail(telegram: str) -> str:
        return f"🔔 Ofertat dalin të parat në Telegram: {telegram}" + hashtags(config)

    caption = (head + f"📖 Udhëzuesi i plotë (çfarë të shohësh, ku të flesh, çfarë të hash): {page} — linku në bio\n"
               + tail(instagram_channel(config)))
    facebook_text = head + f"📖 Udhëzuesi i plotë: {page_url}\n" + tail(facebook_channel(config))
    return Post(kind="guide", title=f"Udhëzues: {guide.display_name}", pictures=pictures, story=story,
                caption=caption, facebook_text=facebook_text, link=page_url)


def guide_sections(guide: Guide) -> list[tuple[str, str, list[tuple[str | None, str]]]]:
    """The guide's sections that get a card: (key, heading, entries), only the ones with content.

    At most 9 (Instagram's carousel holds 10 pictures with the cover); cards.draw_list_card
    leaves off the entries that don't fit a card.
    """
    sections: list[tuple[str, str, list[tuple[str | None, str]]]] = []
    if guide.sights:
        sections.append(("sights", "Çfarë të shohësh", [(e.name, e.text) for e in guide.sights]))
    if guide.itinerary:
        sections.append(("plan", f"Plani për {len(guide.itinerary)} ditë",
                         [(f"Dita {day}", text) for day, text in enumerate(guide.itinerary, start=1)]))
    if guide.areas:
        sections.append(("areas", "Ku të flesh", [(e.name, e.text) for e in guide.areas]))
    if guide.food:
        sections.append(("food", "Çfarë të hash", [(e.name, e.text) for e in guide.food]))
    getting_around = [(f"Nga {code}", text) for code, text in guide.airports.items()]
    if guide.transport:
        getting_around.append(("Në qytet", guide.transport))
    if getting_around:
        sections.append(("transport", "Nga aeroporti dhe nëpër qytet", getting_around))
    if guide.daytrips:
        sections.append(("daytrips", "Udhëtime ditore", [(e.name, e.text) for e in guide.daytrips]))
    practical = [(None, tip) for tip in guide.tips]
    if guide.when:
        practical.append(("Kur të shkosh", guide.when))
    if guide.budget:
        practical.append(("Çmimet", guide.budget))
    if practical:
        sections.append(("tips", "Këshilla praktike", practical))
    return sections[:9]


def hashtags(config: Config) -> str:
    return f"\n\n{config.social.hashtags}" if config.social.hashtags else ""


def handle(config: Config) -> str:
    return config.social.handle or config.channel_handle


def instagram_channel(config: Config) -> str:
    """How an Instagram caption points to the Telegram channel. Not "@flyfromtirana": on Instagram
    that is the Instagram account. A caption can't hold a link, so the address to type, and the
    bio holds the Instagram invite link (invite_links.instagram, put there by hand)."""
    return f"{short_address(config)} (linku në bio)"


def facebook_channel(config: Config) -> str:
    """How a Facebook post points to the Telegram channel: the Facebook invite link, which works there."""
    return join_link(config, "facebook") or config.channel_handle


# --- posting by the bot ----------------------------------------------------------------

def put_pictures_online(post: Post, folder: Path, site_url: str, now: datetime) -> Online:
    """Copy the post's pictures (and its Reel) into the website's /social/ folder. Returns their
    public addresses.

    The names carry the moment of posting (bgy_feed_20261015-0817.jpg), so a route
    posted twice keeps both pictures until prune_pictures() removes the old ones.
    """
    folder.mkdir(parents=True, exist_ok=True)
    base = site_url.rstrip("/") + "/" + PICTURES_FOLDER + "/"
    stamp = f"{now:%Y%m%d-%H%M}"

    def put(path: Path | None) -> str | None:
        if path is None:
            return None
        name = f"{path.stem}_{stamp}{path.suffix}"
        shutil.copy2(path, folder / name)
        return base + name

    return Online(feed=[put(path) for path in post.pictures], story=put(post.story), video=put(post.video),
                  cover=put(post.cover))


def prune_pictures(folder: Path, older_than: datetime) -> int:
    """Delete posted pictures and videos older than `older_than`. Returns how many went."""
    if not folder.is_dir():
        return 0
    deleted = 0
    for path in [*folder.glob("*.jpg"), *folder.glob("*.mp4")]:
        if datetime.fromtimestamp(path.stat().st_mtime, tz=older_than.tzinfo) < older_than:
            path.unlink()
            deleted += 1
    return deleted


def post_on_platforms(post: Post, meta: MetaClient, platforms: tuple[str, ...],
                      online: Online) -> list[tuple[str, str | None | MetaError]]:
    """Post on each platform. Returns one (what, outcome) per post made: the post's
    address, None for a story (it has no address), or the MetaError.

    With a Reel, Instagram gets the Reel; if Instagram refuses it, the feed picture
    goes out instead, so the deal is posted either way (the report shows both).
    """
    feed_urls = online.feed
    outcomes: list[tuple[str, str | None | MetaError]] = []
    for platform in platforms:
        if platform == "instagram":
            posted = False
            if online.video:
                try:
                    outcomes.append(("Reel", meta.post_instagram_reel(online.video, post.caption,
                                                                      cover_url=online.cover)))
                    posted = True
                except MetaError as exc:
                    outcomes.append(("Reel", exc))
            if not posted:
                try:
                    if len(feed_urls) == 1:
                        outcomes.append(("Instagram", meta.post_instagram(feed_urls[0], post.caption)))
                    else:
                        outcomes.append(("Instagram", meta.post_instagram_carousel(feed_urls, post.caption)))
                except MetaError as exc:
                    outcomes.append(("Instagram", exc))
                    continue   # no story without the post
            if online.story:
                try:
                    outcomes.append(("Story", meta.post_instagram(online.story, story=True)))
                except MetaError as exc:
                    outcomes.append(("Story", exc))
        elif platform == "facebook":
            try:
                if len(feed_urls) == 1:
                    outcomes.append(("Facebook", meta.post_facebook(feed_urls[0], post.facebook_text)))
                else:
                    outcomes.append(("Facebook", meta.post_facebook_album(feed_urls, post.facebook_text)))
            except MetaError as exc:
                outcomes.append(("Facebook", exc))
    return outcomes


def failed(outcomes: list[tuple[str, str | None | MetaError]]) -> list[str]:
    """The names of the posts that did not go out."""
    return [what for what, outcome in outcomes if isinstance(outcome, MetaError)]


def report(post: Post, outcomes: list[tuple[str, str | None | MetaError]]) -> str:
    """The Telegram message (HTML) about the posts made: a link for each, or what went wrong."""
    problems = failed(outcomes)
    head = "⚠️ <b>Postim i pjesshëm</b>" if problems else "✅ <b>U postua</b>"
    if len(problems) == len(outcomes):
        head = "❌ <b>Postimi dështoi</b>"
    lines = [f"{head} · {html.escape(post.title)}"]
    icons = {"Instagram": "📸", "Reel": "🎬", "Story": "📱", "Facebook": "📘"}
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

def send_draft(telegram: TelegramClient, post: Post) -> int:
    """Send the post to the owner's chat as a draft: the files, the caption, the link.
    Returns the last message_id."""
    files = ([post.video] if post.video else []) + post.pictures + ([post.story] if post.story else [])
    telegram.send_files(files, header(post))
    telegram.send_message(html.escape(post.caption, quote=False))
    return telegram.send_message(
        "🔗 Linku për postimin në <b>Facebook</b> (në Instagram vendose në bio: "
        "në përshkrim nuk klikohet):\n" + html.escape(post.link, quote=True))


def header(post: Post) -> str:
    """The text under the files: which post this is, and what to do with it."""
    if post.video:
        what = "Ruaj videon dhe postoje si Reel; fotot janë për story-n (9:16) dhe Facebook-un (4:5)"
    elif post.story:
        what = "Ruaj fotot në galeri: feed (4:5) dhe story (9:16)"
    else:
        what = f"Ruaj të {len(post.pictures)} fotot në galeri dhe postoji bashkë (carousel), me këtë radhë"
    return (f"📸 <b>Draft për Instagram dhe Facebook</b>\n{html.escape(post.title)}\n\n"
            f"1️⃣ {what}\n"
            "2️⃣ Kopjo tekstin nga mesazhi tjetër\n"
            "3️⃣ Në Facebook shto edhe linkun (mesazhi i fundit)")


def deal_title(deal: Deal) -> str:
    """'TIRANA → MILAN (Bergamo) nga €14'."""
    route = deal.route
    where = f"TIRANA → {route.city.upper()}" + (f" ({route.airport})" if route.airport else "")
    return f"{where} nga €{format_price(deal.best.price)}"


def site_address(config: Config) -> str:
    """The site's address as people write it: "https://flyfromtirana.al/" -> "flyfromtirana.al"."""
    return config.website.url.split("://")[-1].strip("/")
