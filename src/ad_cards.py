"""The pictures of the paid Facebook/Instagram ads (content/ads.yaml, scripts/ad_kit.py).

Evergreen: they never show a price or a date, so an ad can run for weeks without
going stale (a price on an ad has to be one we really found, and those last hours).
Same look as the daily cards (src/cards.py lends the fonts, colours, photos and
stickers). Four kinds, each named by an ad's `card`:

    photo    a full photo, a big headline and a sticker
    mosaic   six city photos and a big sticker across them
    steps    how it works, in three big figures over a dark photo
    phone    a phone showing a post of the Telegram channel

Each comes in two sizes (AD_SIZES): feed 1080 x 1350 (4:5, the Facebook and Instagram
feeds) and story 1080 x 1920 (9:16, Stories and Reels). On a story Meta lays its own
name at the top and the ad's button at the bottom, so the words stay between `top`
and `bottom` there (Meta asks for about 14% clear at the top and 20% at the bottom).
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

from src.cards import (INK, SUN, WHITE, archivo, cap_height, fit, fit_mono, mono, photo_band, plane_mark, put,
                       shade, sticker, tag, wrap)
from src.intro_cards import chat_screen, phone
from src.photos import post_photo

PAD = 64


@dataclass(frozen=True)
class AdSize:
    """Where the words may go on one size of picture."""

    name: str
    width: int
    height: int
    top: int          # the words start below this
    bottom: int       # and end above this
    footer_mid: int   # the middle of the address line
    band: bool        # a black band under the address (the feed); False = the address is on the picture


AD_SIZES = {
    "feed": AdSize("feed", 1080, 1350, top=0, bottom=1220, footer_mid=1310, band=True),
    "story": AdSize("story", 1080, 1920, top=260, bottom=1440, footer_mid=1500, band=False),
}


def draw_ad(ad: dict, size_name: str, *, brand: str, address: str, handle: str) -> Image.Image:
    """The picture of one ad (an entry of content/ads.yaml, its {PLACEHOLDERS} already filled)."""
    drawers = {"photo": photo_ad, "mosaic": mosaic_ad, "steps": steps_ad, "phone": phone_ad}
    try:
        drawer = drawers[str(ad.get("card"))]
    except KeyError:
        raise ValueError(f"unknown card {ad.get('card')!r}: use one of {', '.join(drawers)}") from None
    size = AD_SIZES[size_name]
    card = drawer(ad, size, brand)
    footer(card, size, address=address, handle=handle)
    return card


# --- the four kinds ---------------------------------------------------------------------

def photo_ad(ad: dict, size: AdSize, brand: str) -> Image.Image:
    """A full photo, the kicker, a big headline and a line under it; a sticker up top."""
    card = full_photo(str(ad["photo"]), size)
    shade(card, 0, size.top + 280, start=170, end=0)
    shade(card, int(size.height * 0.3), size.height, start=0, end=235)
    draw = ImageDraw.Draw(card)
    brand_mark(card, draw, brand, size)
    if ad.get("sticker"):
        sticker(card, str(ad["sticker"]), center=(size.width - 270, size.top + 230), angle=-7, size=52)
    # From the bottom up: the line, the headline, the kicker.
    width = size.width - 2 * PAD
    sub_font = archivo(44, 600, 92)
    sub_lines = wrap(str(ad.get("sub") or ""), sub_font, width)[:3]
    head_font = fit(longest(ad["headline"]), width, size=150, weight=800, width_axis=72)
    head_lines = lines_of(ad["headline"])
    y = size.bottom - 30
    for line in reversed(sub_lines):
        draw.text((PAD, y), line, font=sub_font, fill=WHITE, anchor="ls")
        y -= cap_height(sub_font) + 18
    y -= 30
    for index, line in enumerate(reversed(head_lines)):
        colour = SUN if index == len(head_lines) - 1 else WHITE   # the first line in yellow
        draw.text((PAD - 6, y), line, font=head_font, fill=colour, anchor="ls")
        y -= cap_height(head_font) + 22
    tag(draw, str(ad.get("kicker") or "NGA TIRANA"), left=PAD, middle=y - 30, font=mono(34), fill=SUN, color=INK)
    return card


def mosaic_ad(ad: dict, size: AdSize, brand: str) -> Image.Image:
    """Six city photos in a grid, a big sticker across them, the headline on black under them."""
    card = Image.new("RGB", (size.width, size.height), INK)
    draw = ImageDraw.Draw(card)
    brand_mark(card, draw, brand, size)
    width = size.width - 2 * PAD
    head_font = fit(longest(ad["headline"]), width, size=110, weight=800, width_axis=72)
    sub_font = archivo(40, 600, 92)
    head_lines = lines_of(ad["headline"])
    sub_lines = wrap(str(ad.get("sub") or ""), sub_font, width)[:2]
    text_height = (len(head_lines) * (cap_height(head_font) + 20) + 30
                   + len(sub_lines) * (cap_height(sub_font) + 16))
    # The grid fills the room between the brand and the words.
    gap, cols, rows = 6, 2, 3
    grid_top = size.top + 130
    grid_bottom = size.bottom - text_height - 60
    tile_w = (size.width - (cols - 1) * gap) // cols
    tile_h = (grid_bottom - grid_top - (rows - 1) * gap) // rows
    for index, tile in enumerate(list(ad.get("tiles") or [])[:cols * rows]):
        x = (index % cols) * (tile_w + gap)
        y = grid_top + (index // cols) * (tile_h + gap)
        photo = post_photo(str(tile["photo"]))
        card.paste(photo_band(photo.path if photo else None, tile_w, tile_h, str(tile["photo"])), (x, y))
        shade(card, y + tile_h // 2, y + tile_h, start=0, end=210, left=x, right=x + tile_w)
        draw.text((x + 26, y + tile_h - 26), str(tile["city"]).upper(), font=archivo(46, 800, 80), fill=WHITE,
                  anchor="ls")
    sticker(card, str(ad.get("sticker") or ""), center=(size.width // 2, (grid_top + grid_bottom) // 2), angle=-6,
            size=100)
    y = grid_bottom + 40
    for index, line in enumerate(head_lines):
        y += cap_height(head_font)
        draw.text((PAD - 4, y), line, font=head_font, fill=SUN if index == 0 else WHITE, anchor="ls")
        y += 20
    y += 30
    for line in sub_lines:
        y += cap_height(sub_font)
        draw.text((PAD, y), line, font=sub_font, fill=WHITE, anchor="ls")
        y += 16
    return card


def steps_ad(ad: dict, size: AdSize, brand: str) -> Image.Image:
    """How it works: a kicker, a headline and three figures with a line each, over a dark photo."""
    card = full_photo(str(ad["photo"]), size)
    shade(card, 0, size.height, start=175, end=235)
    draw = ImageDraw.Draw(card)
    brand_mark(card, draw, brand, size)
    width = size.width - 2 * PAD
    rows = list(ad.get("rows") or [])[:3]
    head_font = fit(longest(ad["headline"]), width, size=100, weight=800, width_axis=72)
    text_font = archivo(40, 600, 92)
    # Spread the rows over the room the size has (a story is taller).
    y = size.top + 170
    tag(draw, str(ad.get("kicker") or "SI FUNKSIONON"), left=PAD, middle=y, font=mono(34), fill=SUN, color=INK)
    y += 50
    for line in lines_of(ad["headline"]):
        y += cap_height(head_font)
        draw.text((PAD - 4, y), line, font=head_font, fill=WHITE, anchor="ls")
        y += 18
    room = size.bottom - 110 - y
    step = room // max(len(rows), 1)
    for index, row in enumerate(rows):
        top = y + index * step + 40
        figure = str(row["figure"]).upper()
        figure_font = fit(figure, width, size=150 if size.name == "story" else 128, weight=800, width_axis=72)
        baseline = top + cap_height(figure_font, figure)
        draw.text((PAD - 6, baseline), figure, font=figure_font, fill=SUN, anchor="ls")
        baseline += 20
        for line in wrap(str(row["text"]), text_font, width)[:2]:
            baseline += cap_height(text_font) + 10
            draw.text((PAD, baseline), line, font=text_font, fill=WHITE, anchor="ls")
    tag(draw, str(ad.get("pill") or "FALAS NË TELEGRAM"), left=PAD, middle=size.bottom - 40,
        font=archivo(42, 800, 84), fill=SUN, color=INK)
    return card


def phone_ad(ad: dict, size: AdSize, brand: str) -> Image.Image:
    """A phone showing a post of the Telegram channel, over a dark photo, under the headline."""
    card = full_photo(str(ad["photo"]), size)
    shade(card, 0, size.height, start=140, end=230)
    draw = ImageDraw.Draw(card)
    brand_mark(card, draw, brand, size)
    width = size.width - 2 * PAD
    y = size.top + 150
    tag(draw, str(ad.get("kicker") or "TELEGRAM"), left=PAD, middle=y, font=mono(34), fill=SUN, color=INK)
    y += 50
    head_font = fit(longest(ad["headline"]), width, size=110, weight=800, width_axis=72)
    for index, line in enumerate(lines_of(ad["headline"])):
        y += cap_height(head_font)
        draw.text((PAD - 4, y), line, font=head_font, fill=SUN if index == 0 else WHITE, anchor="ls")
        y += 18
    # The phone takes the room left, its bottom cut off by the address line.
    phone_width = 500 if size.name == "story" else 400
    screen = chat_screen(ad, _BrandOnly(brand))
    picture = phone(screen, width=phone_width)
    phone_top = y + 50
    room = size.bottom - phone_top
    if picture.height > room:
        picture = picture.crop((0, 0, picture.width, room))
    put(card, picture, (size.width - picture.width - 30, phone_top))
    if ad.get("note"):
        draw.text((PAD, size.bottom - 10), str(ad["note"]), font=mono(26), fill=WHITE, anchor="ls")
    return card


# --- pieces --------------------------------------------------------------------------------

class _BrandOnly:
    """chat_screen (src/intro_cards.py) only reads the brand from the config."""

    def __init__(self, brand: str):
        self.brand = brand


def full_photo(iata: str, size: AdSize) -> Image.Image:
    """The route's photo filling the whole picture."""
    photo = post_photo(iata)
    return photo_band(photo.path if photo else None, size.width, size.height, iata)


def brand_mark(card: Image.Image, draw: ImageDraw.ImageDraw, brand: str, size: AdSize) -> None:
    """The plane mark and the brand name, top left (below Meta's own name on a story)."""
    mark = 68
    top = size.top + 36
    put(card, plane_mark(mark), (PAD, top))
    draw.text((PAD + mark + 18, top + mark / 2), brand, font=archivo(40, 800, 88), fill=WHITE, anchor="lm")


def footer(card: Image.Image, size: AdSize, *, address: str, handle: str) -> None:
    """The site's address on the left and the account's name on the right: on a black band at the
    bottom of a feed picture, above Meta's button on a story."""
    draw = ImageDraw.Draw(card)
    if size.band:
        draw.rectangle((0, size.footer_mid - 40, size.width, size.height), fill=INK)
    draw.text((PAD, size.footer_mid), address, font=fit_mono(address, size.width - 2 * PAD - 340, size=32),
              fill=WHITE, anchor="lm")
    draw.text((size.width - PAD, size.footer_mid), handle, font=archivo(34, 700, 90), fill=SUN, anchor="rm")


def lines_of(text) -> list[str]:
    """A headline's lines: the YAML may give a list, or one string with line breaks."""
    if isinstance(text, list):
        return [str(line) for line in text]
    return [line for line in str(text).splitlines() if line.strip()]


def longest(text) -> str:
    """The widest line of a headline, to size the font so every line fits."""
    return max(lines_of(text), key=archivo(100, 800, 72).getlength)
