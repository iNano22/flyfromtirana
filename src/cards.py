"""Draws the pictures for Instagram and Facebook.

Three kinds, all in the website's airport-signage look (yellow sign, black
bars, Archivo + IBM Plex Mono, the fonts in assets/fonts):

    draw_card        a deal: the destination's photo filling the card, a giant price and the
                     city over its darkened lower half, the saving as a sticker
    draw_guide_card  a city guide's cover: the photo, the city's name, its tagline, the page
    draw_list_card   one section of a guide (sights, food, ...): its entries on a black board
    draw_top_card    the week's top deals: the first one's photo and a sticker per deal

Each comes in two sizes:

    feed    1080 x 1350   the tallest picture Instagram's feed allows (4:5); Facebook too
    story   1080 x 1920   an Instagram or Facebook story (9:16)

The photos are the ones the Telegram posts use (assets/telegram/<iata>.jpg).
Drawing is done with Pillow, the Python image library: there is no Chrome on
the server, and this way a card takes well under a second. Pillow draws text
one line at a time with no layout engine, so every line's position is worked
out here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from src.deals import Deal
from src.formatter import WORDS, format_dates, format_duration, format_price, saving_percent
from src.photos import ASSETS_DIR

FONTS_DIR = ASSETS_DIR / "fonts"
ARCHIVO = FONTS_DIR / "Archivo[wdth,wght].ttf"      # one file holds every weight and width
PLEX_MONO = FONTS_DIR / "IBMPlexMono-SemiBold.ttf"

# The site's colours (templates/site.html).
INK = (15, 17, 20)          # black
SUN = (255, 214, 10)        # yellow
WHITE = (255, 255, 255)
BOARD_LINE = (42, 46, 53)   # the grey of the departures board's lines; the photo's stand-in too

JPEG_QUALITY = 90


@dataclass(frozen=True)
class Layout:
    """Where the bands of a card are. Heights add up to `height`."""

    name: str
    width: int
    height: int
    bar: int        # the black brand bar at the top
    photo: int      # the destination photo under it
    footer: int     # the black strip at the bottom (the site's address)
    padding: int    # left/right margin inside the yellow sign
    scale: float    # text sizes and spacing on the sign, relative to the feed card's

    @property
    def sign_top(self) -> int:
        return self.bar + self.photo

    @property
    def sign_bottom(self) -> int:
        return self.height - self.footer


# The story's bottom 250px and top 250px are covered by Instagram's own buttons
# and the account name, so its sign ends above 1670 and its footer is only a
# black band with the address high up in it.
LAYOUTS = {
    "feed": Layout("feed", 1080, 1350, bar=96, photo=624, footer=80, padding=56, scale=1.0),
    "story": Layout("story", 1080, 1920, bar=120, photo=820, footer=320, padding=64, scale=1.16),
}


def save_cards(deal: Deal, out_dir: Path, *, airline_names: dict[str, str], brand: str, handle: str,
               address: str, photo: Path | None, language: str = "sq") -> dict[str, Path]:
    """Write both cards of a deal as JPEGs: {"feed": out_dir/bgy_feed.jpg, "story": out_dir/bgy_story.jpg}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in LAYOUTS:
        card = draw_card(deal, name, airline_names=airline_names, brand=brand, handle=handle,
                         address=address, photo=photo, language=language)
        paths[name] = save_jpeg(card, out_dir / f"{deal.route.iata.lower()}_{name}.jpg")
    return paths


def save_jpeg(card: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    card.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return path


# --- the deal card ---------------------------------------------------------------------

def flight_line(deal: Deal, airline_names: dict[str, str], words: dict) -> str:
    """'Wizz Air · direkt · 1 orë 55 min · kthimi nga €14'."""
    best = deal.best
    parts = [airline_names.get(best.airline, best.airline) or "",
             words["direct"] if best.is_direct else words["with_stops"],
             format_duration(best.duration_min, words) or ""]
    if deal.return_quote:
        parts.append(f"kthimi nga €{format_price(deal.return_quote.price)}")
    return "  ·  ".join(part for part in parts if part)


# --- the city guide card ---------------------------------------------------------------

# --- a city guide's section (one card of the carousel) -----------------------------------

def draw_list_card(heading: str, kicker: str, entries: list[tuple[str | None, str]], *, page: str, brand: str,
                   handle: str, tag_text: str = "UDHËZUES") -> Image.Image:
    """One section of a guide as a card: the heading on yellow, the entries on a black board.

    entries: (name, text) pairs, e.g. ("Koloseu", "Amfiteatri më i madh..."); a name may be
    None (a tip). Entries that don't fit on the board are left off, so give the best first.
    kicker: the small mono line above the heading, e.g. "ROME  ·  2/7".
    """
    layout = LAYOUTS["feed"]
    width, pad = layout.width, layout.padding
    card = Image.new("RGB", (width, layout.height), SUN)
    draw = ImageDraw.Draw(card)
    brand_bar(draw, card, layout, brand=brand, tag_text=tag_text)
    footer(draw, layout, address=page, handle=handle)

    y = layout.bar + 48
    codes = mono(34)
    y += cap_height(codes)
    draw.text((pad, y), kicker, font=codes, fill=INK, anchor="ls")
    y += 26
    heading_font = fit(heading, width - 2 * pad, size=120, weight=800, width_axis=72, smallest=60)
    for line in wrap(heading, heading_font, width - 2 * pad)[:2]:
        y += cap_height(heading_font, line)
        draw.text((pad, y), line, font=heading_font, fill=INK, anchor="ls")
        y += 14
    y += 30

    # The board: each entry is its name, then its text on up to three lines.
    top, bottom = y, layout.sign_bottom - 40
    draw.rounded_rectangle((pad, top, width - pad, bottom), radius=20, fill=INK)
    inner = width - 2 * pad - 72
    name_font = archivo(42, 700, 86)
    text_font = archivo(33, 500, 96)
    line_gap = 44
    y = top + 36
    for number, (name, text) in enumerate(entries):
        lines = wrap(text, text_font, inner)
        if len(lines) > 3:   # too long for the card: keep three lines, the last one shortened
            lines = lines[:3]
            lines[-1] = shorten(lines[-1], text_font, inner)
        needed = (cap_height(name_font) + 16 if name else 0) + line_gap * len(lines) + 30
        if y + needed > bottom - 20:
            break   # the rest doesn't fit
        if number:
            draw.line((pad + 36, y, width - pad - 36, y), fill=BOARD_LINE, width=2)
            y += 30
        if name:
            y += cap_height(name_font)
            draw.text((pad + 36, y), name, font=name_font, fill=SUN, anchor="ls")
            y += 16
        for line in lines:
            y += cap_height(text_font)
            draw.text((pad + 36, y), line, font=text_font, fill=WHITE, anchor="ls")
            y += line_gap - cap_height(text_font)
        y += 30
    return card


def shorten(line: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Cut a line of text so that it fits max_width with an ellipsis at the end."""
    words = line.split()
    while words and font.getlength(" ".join(words) + "…") > max_width:
        words.pop()
    return " ".join(words) + "…"


# --- the week's top deals --------------------------------------------------------------

# --- the common frame: brand bar, photo, footer -----------------------------------------

# --- the deal card ---------------------------------------------------------------------

def draw_card(deal: Deal, layout_name: str, *, airline_names: dict[str, str], brand: str, handle: str,
              address: str, photo: Path | None, language: str = "sq") -> Image.Image:
    """A deal's card as a Pillow image (RGB): the destination's photo filling the card, a giant
    price and the city over its darkened lower half, the saving as a sticker. layout_name: "feed" or "story"."""
    layout = LAYOUTS[layout_name]
    words = WORDS[language]
    card = full_photo(photo, deal.route.iata, layout)
    shade(card, 0, px(layout, 260), start=150, end=0)
    shade(card, int(layout.sign_bottom * 0.4), layout.sign_bottom, start=0, end=240)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, brand, layout)
    footer(draw, layout, address=address, handle=handle)
    width, pad = layout.width, layout.padding

    # The saving as a sticker at the top right; without a usual price, "OFERTË".
    saving = saving_percent(deal)
    sticker(card, f"-{saving}%" if saving else "OFERTË", center=(width - px(layout, 230), px(layout, 330)),
            angle=-8, size=px(layout, 72 if saving else 52))

    # The words, from the bottom up: the flight, the dates, the city, the price, the route.
    y = layout.sign_bottom - px(layout, 56)
    details = flight_line(deal, airline_names, words)
    details_font = fit(details, width - 2 * pad, size=px(layout, 38), weight=600, width_axis=92, smallest=px(layout, 28))
    draw.text((pad, y), details, font=details_font, fill=WHITE, anchor="ls")
    y -= cap_height(details_font) + px(layout, 24)
    dates = format_dates([q.depart_date for q in deal.quotes], words["months"]).replace(", ", "  ·  ")
    dates_font = archivo(px(layout, 46), 700, 90)
    draw.text((pad, y), dates, font=dates_font, fill=SUN, anchor="ls")
    y -= cap_height(dates_font) + px(layout, 26)
    city = deal.route.city.upper()
    city_font = fit(city, width - 2 * pad, size=px(layout, 150), weight=800, width_axis=72)
    draw.text((pad, y), city, font=city_font, fill=WHITE, anchor="ls")
    if deal.route.airport:
        airport_font = archivo(px(layout, 50), 600, 90)
        x = pad + draw.textlength(city, font=city_font) + px(layout, 22)
        if x + draw.textlength(deal.route.airport, font=airport_font) <= width - pad:
            draw.text((x, y), deal.route.airport, font=airport_font, fill=WHITE, anchor="ls")
    y -= cap_height(city_font, city) + px(layout, 20)
    price = f"€{format_price(deal.best.price)}"
    price_font = archivo(px(layout, 330), 800, 72)
    small = archivo(px(layout, 52), 700, 90)
    draw.text((pad, y), "nga", font=small, fill=WHITE, anchor="ls")
    x = pad + draw.textlength("nga", font=small) + px(layout, 18)
    draw.text((x - px(layout, 8), y), price, font=price_font, fill=SUN, anchor="ls")
    if deal.median:
        x += draw.textlength(price, font=price_font) + px(layout, 24)
        draw.text((x, y), f"zakonisht ~€{deal.median:.0f}", font=archivo(px(layout, 36), 500, 95), fill=WHITE,
                  anchor="ls")
    y -= cap_height(price_font) + px(layout, 50)
    tag(draw, f"{deal.best.origin} → {deal.best.destination}  ·  ONE WAY", left=pad, middle=y,
        font=mono(px(layout, 32)), fill=INK, color=SUN)
    return card


def flight_line(deal: Deal, airline_names: dict[str, str], words: dict) -> str:
    """'Wizz Air · direkt · 1 orë 55 min · kthimi nga €14'."""
    best = deal.best
    parts = [airline_names.get(best.airline, best.airline) or "",
             words["direct"] if best.is_direct else words["with_stops"],
             format_duration(best.duration_min, words) or ""]
    if deal.return_quote:
        parts.append(f"kthimi nga €{format_price(deal.return_quote.price)}")
    return "  ·  ".join(part for part in parts if part)


def px(layout: Layout, n: float) -> int:
    """A size of the feed card, scaled for this layout (the story is a little bigger)."""
    return round(n * layout.scale)


# --- the city guide's cover ------------------------------------------------------------

def draw_guide_card(layout_name: str, *, city: str, iata: str, tagline: str, price: float | None, page: str,
                    photo: Path | None, brand: str, handle: str) -> Image.Image:
    """A city guide's cover: the photo filling the card, the city's name, its tagline, the flights'
    price and the page's address. page: the guide page's address as people write it."""
    layout = LAYOUTS[layout_name]
    card = full_photo(photo, iata, layout)
    shade(card, 0, px(layout, 260), start=150, end=0)
    shade(card, int(layout.sign_bottom * 0.4), layout.sign_bottom, start=0, end=240)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, brand, layout)
    footer(draw, layout, address=page, handle=handle)
    width, pad = layout.width, layout.padding
    sticker(card, "UDHËZUES", center=(width - px(layout, 220), px(layout, 330)), angle=-8, size=px(layout, 52))

    y = layout.sign_bottom - px(layout, 56)
    contents = "Çfarë të shohësh  ·  ku të flesh  ·  çfarë të hash  ·  si të lëvizësh"
    contents_font = fit(contents, width - 2 * pad, size=px(layout, 34), weight=500, width_axis=95, smallest=px(layout, 26))
    draw.text((pad, y), contents, font=contents_font, fill=WHITE, anchor="ls")
    y -= cap_height(contents_font) + px(layout, 24)
    if price is not None:
        price_font = archivo(px(layout, 46), 700, 90)
        draw.text((pad, y), f"Fluturime nga Tirana nga €{format_price(price)}", font=price_font, fill=SUN, anchor="ls")
        y -= cap_height(price_font) + px(layout, 26)
    tagline_font = archivo(px(layout, 54), 600, 88)
    for line in reversed(wrap(tagline, tagline_font, width - 2 * pad)[:2]):
        draw.text((pad, y), line, font=tagline_font, fill=WHITE, anchor="ls")
        y -= cap_height(tagline_font) + px(layout, 18)
    y -= px(layout, 10)
    name = city.upper()
    name_font = fit(name, width - 2 * pad, size=px(layout, 230), weight=800, width_axis=72)
    draw.text((pad - px(layout, 8), y), name, font=name_font, fill=SUN, anchor="ls")
    y -= cap_height(name_font, name) + px(layout, 50)
    tag(draw, f"TIA → {iata}  ·  UDHËZUESI I QYTETIT", left=pad, middle=y, font=mono(px(layout, 32)), fill=INK, color=SUN)
    return card


# --- the week's top deals --------------------------------------------------------------

def draw_top_card(deals: list[Deal], *, brand: str, handle: str, address: str, photo: Path | None,
                  title: str = "TOP 5", subtitle: str = "OFERTAT E JAVËS") -> Image.Image:
    """The cover of the weekly carousel: the first deal's photo darkened, the title, and one
    sticker per deal with its city and price."""
    layout = LAYOUTS["feed"]
    width, pad = layout.width, layout.padding
    iata = deals[0].route.iata if deals else "TIA"
    card = full_photo(photo, iata, layout)
    shade(card, 0, layout.sign_bottom, start=175, end=215)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, brand, layout)
    footer(draw, layout, address=address, handle=handle)

    y = layout.bar + 70
    draw.text((pad, y + cap_height(mono(36))), f"NGA TIRANA  ·  {subtitle}", font=mono(36), fill=SUN, anchor="ls")
    y += 70
    title_font = fit(title, width - 2 * pad, size=200, weight=800, width_axis=72)
    y += cap_height(title_font, title)
    draw.text((pad - 8, y), title, font=title_font, fill=WHITE, anchor="ls")
    rows = deals[:6]
    top = y + 60
    step = (layout.sign_bottom - 80 - top) // max(len(rows), 1)
    for index, deal in enumerate(rows):
        where = deal.route.city.upper() + (f" · {deal.route.airport.upper()}" if deal.route.airport else "")
        text = f"{where}  €{format_price(deal.best.price)}"
        size = 64 if len(rows) <= 4 else 54
        angle = -5 if index % 2 == 0 else 4
        x = width // 2 + (-90 if index % 2 == 0 else 90)
        sticker(card, text, center=(x, top + index * step + step // 2), angle=angle, size=size)
    return card


# --- the common pieces: full photo, shading, brand, stickers, footer ------------------------

def full_photo(photo: Path | None, iata: str, layout: Layout) -> Image.Image:
    """A card with the photo filling everything above the footer."""
    card = Image.new("RGB", (layout.width, layout.height), INK)
    card.paste(photo_band(photo, layout.width, layout.sign_bottom, iata), (0, 0))
    return card


def shade(card: Image.Image, top: int, bottom: int, *, start: int, end: int, left: int = 0,
          right: int | None = None) -> None:
    """Darken a band of the card with a vertical gradient: `start` opacity (0-255) at the
    top of the band, `end` at the bottom."""
    right = card.width if right is None else right
    height, width = bottom - top, right - left
    if height <= 0 or width <= 0:
        return
    ramp = Image.linear_gradient("L").resize((width, height))   # 0 at the top, 255 at the bottom
    mask = ramp.point(lambda v: start + (end - start) * v // 255)
    card.paste(Image.new("RGB", (width, height), INK), (left, top), mask)


def brand_over_photo(card: Image.Image, draw: ImageDraw.ImageDraw, brand: str, layout: Layout) -> None:
    """The brand mark and name over a photo (shade the top of the photo first)."""
    mark = px(layout, 64)
    top = px(layout, 32)
    card.paste(plane_mark(mark), (layout.padding, top), plane_mark(mark))
    draw.text((layout.padding + mark + 18, top + mark / 2), brand, font=archivo(px(layout, 38), 800, 88), fill=WHITE,
              anchor="lm")


def sticker(card: Image.Image, text: str, *, center: tuple[int, int], angle: float, size: int = 44) -> None:
    """A rotated yellow label with black text, like a sticker on the picture."""
    font = archivo(size, 800, 80)
    pad_x, pad_y = round(size * 0.75), round(size * 0.6)
    width = int(font.getlength(text)) + 2 * pad_x
    height = cap_height(font) + 2 * pad_y
    label = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(label)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=round(size * 0.3), fill=SUN)
    draw.text((pad_x, pad_y + cap_height(font)), text, font=font, fill=INK, anchor="ls")
    label = label.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    shadow = Image.new("RGBA", label.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), (0, 0, label.width, label.height), label)
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    x, y = center[0] - label.width // 2, center[1] - label.height // 2
    card.paste(shadow, (x + 6, y + 12), shadow)
    card.paste(label, (x, y), label)


def frame(layout: Layout, *, photo: Path | None, iata: str, brand: str, handle: str, address: str,
          tag_text: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A yellow card with the black brand bar, the photo band and the black footer drawn
    (the guide's section cards and the share pictures use it)."""
    card = Image.new("RGB", (layout.width, layout.height), SUN)
    draw = ImageDraw.Draw(card)
    brand_bar(draw, card, layout, brand=brand, tag_text=tag_text)
    card.paste(photo_band(photo, layout.width, layout.photo, iata), (0, layout.bar))
    footer(draw, layout, address=address, handle=handle)
    return card, draw


def brand_bar(draw: ImageDraw.ImageDraw, card: Image.Image, layout: Layout, *, brand: str, tag_text: str) -> None:
    """The black bar at the top: the plane mark, the brand name, a yellow tag on the right."""
    width, pad = layout.width, layout.padding
    draw.rectangle((0, 0, width, layout.bar), fill=INK)
    mark_size = layout.bar - 32
    card.paste(plane_mark(mark_size), (pad, (layout.bar - mark_size) // 2))
    draw.text((pad + mark_size + 18, layout.bar / 2), brand, font=archivo(38, 800, 88), fill=WHITE, anchor="lm")
    tag(draw, tag_text, right=width - pad, middle=layout.bar / 2, font=mono(24), fill=SUN, color=INK)


def footer(draw: ImageDraw.ImageDraw, layout: Layout, *, address: str, handle: str) -> None:
    """The black strip at the bottom: an address on the left, the account's name on the right."""
    width, pad = layout.width, layout.padding
    draw.rectangle((0, layout.sign_bottom, width, layout.height), fill=INK)
    middle = layout.sign_bottom + 42   # high up in the band: a story's bottom is covered by Instagram
    draw.text((pad, middle), address, font=fit_mono(address, width - 2 * pad - 320, size=30), fill=WHITE, anchor="lm")
    draw.text((width - pad, middle), handle, font=archivo(32, 700, 90), fill=SUN, anchor="rm")


# --- text ------------------------------------------------------------------------------

@lru_cache(maxsize=64)
def archivo(size: int, weight: int, width_axis: int) -> ImageFont.FreeTypeFont:
    """Archivo at one size, weight (100-900) and width (62-125: 72 is the narrow headline cut)."""
    font = ImageFont.truetype(str(ARCHIVO), size)
    font.set_variation_by_axes([weight, width_axis])   # the font's axes, in its order: weight, width
    return font


@lru_cache(maxsize=16)
def mono(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(PLEX_MONO), size)


def fit(text: str, max_width: int, *, size: int, weight: int, width_axis: int,
        smallest: int = 40) -> ImageFont.FreeTypeFont:
    """Archivo at `size`, or smaller until `text` fits in max_width (never under `smallest`)."""
    while size > smallest and archivo(size, weight, width_axis).getlength(text) > max_width:
        size -= 2
    return archivo(size, weight, width_axis)


def fit_mono(text: str, max_width: int, *, size: int, smallest: int = 20) -> ImageFont.FreeTypeFont:
    """IBM Plex Mono at `size`, or smaller until `text` fits in max_width."""
    while size > smallest and mono(size).getlength(text) > max_width:
        size -= 2
    return mono(size)


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Break `text` into lines no wider than max_width (at spaces)."""
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and font.getlength(candidate) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def cap_height(font: ImageFont.FreeTypeFont, text: str = "H") -> int:
    """How far the text reaches above its baseline: the top of a capital H (or of `text`)."""
    x0, y0, x1, y1 = font.getbbox(text, anchor="ls")
    return -y0


def tag(draw: ImageDraw.ImageDraw, text: str, *, middle: float, font: ImageFont.FreeTypeFont,
        fill: tuple, color: tuple, left: float | None = None, right: float | None = None) -> None:
    """A rounded label (like the site's OFERTË stamp), its left or right edge at the given x."""
    pad_x, pad_y = 18, 12
    text_width = draw.textlength(text, font=font)
    height = cap_height(font) + 2 * pad_y
    if left is None:
        left = right - text_width - 2 * pad_x
    draw.rounded_rectangle((left, middle - height / 2, left + text_width + 2 * pad_x, middle + height / 2),
                           radius=10, fill=fill)
    draw.text((left + pad_x, middle + cap_height(font) / 2), text, font=font, fill=color, anchor="ls")


# --- pictures --------------------------------------------------------------------------

def photo_band(photo: Path | None, width: int, height: int, iata: str) -> Image.Image:
    """The destination photo cropped to width x height (centred a little above the middle,
    where the landmark usually is). Without a photo: a dark board with the airport code."""
    if photo is not None and photo.exists():
        with Image.open(photo) as source:
            return ImageOps.fit(source.convert("RGB"), (width, height), Image.Resampling.LANCZOS,
                                centering=(0.5, 0.4))
    board = Image.new("RGB", (width, height), BOARD_LINE)
    ImageDraw.Draw(board).text((width / 2, height / 2), iata, font=mono(220), fill=SUN, anchor="mm")
    return board


@lru_cache(maxsize=4)
def plane_mark(size: int) -> Image.Image:
    """The brand mark: the site's plane on a yellow rounded square.

    The plane is the Material "flight" icon the site uses (an SVG path of straight
    lines plus a round nose), turned 45° to point up and to the right. It is drawn
    4x too big, then shrunk, which smooths its edges.
    """
    scale = 4
    big = size * scale
    mark = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.18, fill=SUN)
    # The icon in its own 24 x 24 box, before the turn.
    nose = [(11.5 + 1.5 * math.cos(a), 3.5 - 1.5 * math.sin(a)) for a in
            (math.radians(d) for d in range(0, 181, 20))]           # from (13, 3.5) over the top to (10, 3.5)
    outline = [(21, 16), (21, 14), (13, 9), *nose, (10, 9), (2, 14), (2, 16), (10, 13.5), (10, 19),
               (8, 20.5), (8, 22), (11.5, 21), (15, 22), (15, 20.5), (13, 19), (13, 13.5)]
    angle = math.radians(45)
    unit = big * 0.62 / 24          # the icon takes 62% of the square
    offset = (big - 24 * unit) / 2
    points = []
    for x, y in outline:
        # Turn around the icon's centre (12, 12), then place it in the square.
        dx, dy = x - 12, y - 12
        rx = 12 + dx * math.cos(angle) - dy * math.sin(angle)
        ry = 12 + dx * math.sin(angle) + dy * math.cos(angle)
        points.append((offset + rx * unit, offset + ry * unit))
    draw.polygon(points, fill=INK)
    return mark.resize((size, size), Image.Resampling.LANCZOS)
