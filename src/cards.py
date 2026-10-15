"""Draws the pictures for Instagram and Facebook.

Three kinds, all in the website's airport-signage look (yellow sign, black
bars, Archivo + IBM Plex Mono, the fonts in assets/fonts):

    draw_card        a deal: the destination's photo, then a yellow sign with the
                     city, the price, the dates and the flight
    draw_guide_card  a city guide: the photo, the city, its tagline and the page's address
    draw_list_card   one section of a guide (sights, food, ...): its entries on a black board
    draw_photo_card  a photo with a headline: the introduction posts
    draw_top_card    the week's top deals: a black departures board listing them

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

from PIL import Image, ImageDraw, ImageFont, ImageOps

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

def draw_card(deal: Deal, layout_name: str, *, airline_names: dict[str, str], brand: str, handle: str,
              address: str, photo: Path | None, language: str = "sq") -> Image.Image:
    """A deal's card as a Pillow image (RGB). layout_name: "feed" or "story"."""
    layout = LAYOUTS[layout_name]
    words = WORDS[language]
    card, draw = frame(layout, photo=photo, iata=deal.route.iata, brand=brand, handle=handle, address=address,
                       tag_text="OFERTË")
    width, pad = layout.width, layout.padding

    # The yellow sign. Lines are placed one under the other from `y` down; each
    # line's text sits on its baseline (anchor "ls" = left, baseline), so "nga" and
    # the big price can share one. px() scales the feed card's sizes for the story.
    def px(n: float) -> int:
        return round(n * layout.scale)

    y = layout.sign_top + px(44)

    # TIA → BGY, and the airport's name on the right when the city has several.
    codes = mono(px(34))
    y += cap_height(codes)
    draw.text((pad, y), f"{deal.best.origin} → {deal.best.destination}", font=codes, fill=INK, anchor="ls")
    if deal.route.airport:
        draw.text((width - pad, y), deal.route.airport.upper(), font=codes, fill=INK, anchor="rs")
    y += px(26)

    # The city, as big as fits the width.
    city = deal.route.city.upper()
    city_font = fit(city, width - 2 * pad, size=px(210), weight=800, width_axis=72)
    y += cap_height(city_font, city)
    draw.text((pad, y), city, font=city_font, fill=INK, anchor="ls")
    y += px(22)

    # nga €14, with the saving and the usual price beside it.
    price_font = archivo(px(150), 800, 80)
    small = archivo(px(44), 700, 90)
    y += cap_height(price_font)
    draw.text((pad, y), "nga", font=small, fill=INK, anchor="ls")
    x = pad + draw.textlength("nga", font=small) + px(20)
    price = f"€{format_price(deal.best.price)}"
    draw.text((x, y), price, font=price_font, fill=INK, anchor="ls")
    x += draw.textlength(price, font=price_font) + px(28)
    saving = saving_percent(deal)
    top = y - cap_height(price_font)  # the top of the digits
    if saving:
        tag(draw, f"-{saving}%", left=x, middle=top + px(34), font=archivo(px(46), 800, 90), fill=INK, color=SUN)
    if deal.median:
        usual = f"zakonisht ~€{deal.median:.0f}"
        draw.text((x, y), usual, font=archivo(px(34), 500, 95), fill=INK, anchor="ls")
    y += px(30)

    # The dates, then the flight in one line.
    dates = format_dates([q.depart_date for q in deal.quotes], words["months"]).replace(", ", "  ·  ")
    dates_font = archivo(px(42), 700, 90)
    y += cap_height(dates_font)
    draw.text((pad, y), dates, font=dates_font, fill=INK, anchor="ls")
    y += px(24)
    details = flight_line(deal, airline_names, words)
    details_font = fit(details, width - 2 * pad, size=px(34), weight=500, width_axis=95, smallest=px(26))
    y += cap_height(details_font)
    draw.text((pad, y), details, font=details_font, fill=INK, anchor="ls")
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


# --- the city guide card ---------------------------------------------------------------

def draw_guide_card(layout_name: str, *, city: str, iata: str, tagline: str, price: float | None, page: str,
                    photo: Path | None, brand: str, handle: str) -> Image.Image:
    """A city guide's card: the photo, the city's name, its tagline, the flights' price and the page.

    page: the guide page's address as people write it, e.g. "flyfromtirana.al/rome/".
    """
    layout = LAYOUTS[layout_name]
    card, draw = frame(layout, photo=photo, iata=iata, brand=brand, handle=handle, address=page,
                       tag_text="UDHËZUES")
    width, pad = layout.width, layout.padding

    def px(n: float) -> int:
        return round(n * layout.scale)

    y = layout.sign_top + px(44)
    codes = mono(px(34))
    y += cap_height(codes)
    draw.text((pad, y), f"TIA → {iata}", font=codes, fill=INK, anchor="ls")
    draw.text((width - pad, y), "UDHËZUESI I QYTETIT", font=codes, fill=INK, anchor="rs")
    y += px(26)

    name = city.upper()
    name_font = fit(name, width - 2 * pad, size=px(210), weight=800, width_axis=72)
    y += cap_height(name_font, name)
    draw.text((pad, y), name, font=name_font, fill=INK, anchor="ls")
    y += px(30)

    # The tagline, wrapped onto two lines at most.
    tagline_font = archivo(px(52), 600, 88)
    for line in wrap(tagline, tagline_font, width - 2 * pad)[:2]:
        y += cap_height(tagline_font)
        draw.text((pad, y), line, font=tagline_font, fill=INK, anchor="ls")
        y += px(16)
    y += px(18)

    # What's in the guide, and the flights' price.
    y += px(8)
    contents_font = archivo(px(34), 500, 95)
    y += cap_height(contents_font)
    draw.text((pad, y), "Çfarë të shohësh  ·  ku të flesh  ·  çfarë të hash  ·  si të lëvizësh",
              font=fit("Çfarë të shohësh  ·  ku të flesh  ·  çfarë të hash  ·  si të lëvizësh", width - 2 * pad,
                       size=px(34), weight=500, width_axis=95, smallest=px(26)),
              fill=INK, anchor="ls")
    if price is not None:
        y += px(30)
        price_font = archivo(px(46), 800, 88)
        y += cap_height(price_font)
        draw.text((pad, y), f"Fluturime nga Tirana nga €{format_price(price)}", font=price_font, fill=INK,
                  anchor="ls")
    return card


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


# --- a photo with a headline (the introduction posts) ------------------------------------

def draw_photo_card(headline: str, sub: str, *, kicker: str, photo: Path | None, iata: str, tag_text: str,
                    brand: str, handle: str, address: str) -> Image.Image:
    """A photo on top, a headline of up to three lines and a line under it on the yellow sign."""
    layout = LAYOUTS["feed"]
    width, pad = layout.width, layout.padding
    card, draw = frame(layout, photo=photo, iata=iata, brand=brand, handle=handle, address=address,
                       tag_text=tag_text)
    y = layout.sign_top + 44
    codes = mono(34)
    y += cap_height(codes)
    draw.text((pad, y), kicker, font=codes, fill=INK, anchor="ls")
    y += 30
    headline_font = fit(headline, (width - 2 * pad) * 2, size=128, weight=800, width_axis=72, smallest=80)
    for line in wrap(headline, headline_font, width - 2 * pad)[:3]:
        y += cap_height(headline_font, line)
        draw.text((pad, y), line, font=headline_font, fill=INK, anchor="ls")
        y += 18
    y += 20
    sub_font = archivo(40, 500, 92)
    for line in wrap(sub, sub_font, width - 2 * pad)[:2]:
        y += cap_height(sub_font)
        draw.text((pad, y), line, font=sub_font, fill=INK, anchor="ls")
        y += 16
    return card


# --- the week's top deals --------------------------------------------------------------

def draw_top_card(deals: list[Deal], *, brand: str, handle: str, address: str, title: str = "TOP 5",
                  subtitle: str = "OFERTAT E JAVËS") -> Image.Image:
    """The cover of the weekly carousel: the title on yellow, a black board with one line per deal."""
    layout = LAYOUTS["feed"]
    width, pad = layout.width, layout.padding
    card = Image.new("RGB", (width, layout.height), SUN)
    draw = ImageDraw.Draw(card)
    brand_bar(draw, card, layout, brand=brand, tag_text="JAVA")
    footer(draw, layout, address=address, handle=handle)

    # The title.
    y = layout.bar + 52
    codes = mono(36)
    y += cap_height(codes)
    draw.text((pad, y), f"TIA →  {subtitle}", font=codes, fill=INK, anchor="ls")
    y += 28
    title_font = fit(title, width - 2 * pad, size=230, weight=800, width_axis=72)
    y += cap_height(title_font, title)
    draw.text((pad, y), title, font=title_font, fill=INK, anchor="ls")
    y += 44

    # The board: one line per deal, like the departures board on the site.
    rows = deals[:6]
    top = y
    row_height = min(140, (layout.sign_bottom - 40 - top - 70) // max(len(rows), 1))
    bottom = top + 70 + row_height * len(rows)
    draw.rounded_rectangle((pad, top, width - pad, bottom), radius=20, fill=INK)
    head = mono(30)
    draw.text((pad + 36, top + 24 + cap_height(head)), "DESTINACIONI", font=head, fill=SUN, anchor="ls")
    draw.text((width - pad - 36, top + 24 + cap_height(head)), "NGA", font=head, fill=SUN, anchor="rs")
    line_y = top + 70
    code_font = mono(40)
    city_font = archivo(48, 700, 82)
    price_font = archivo(56, 800, 84)
    for deal in rows:
        draw.line((pad + 36, line_y, width - pad - 36, line_y), fill=BOARD_LINE, width=2)
        baseline = line_y + row_height // 2 + cap_height(city_font) // 2
        draw.text((pad + 36, baseline), deal.route.iata, font=code_font, fill=SUN, anchor="ls")
        city = deal.route.city.upper() + (f" · {deal.route.airport}" if deal.route.airport else "")
        price = f"€{format_price(deal.best.price)}"
        price_width = draw.textlength(price, font=price_font)
        city_font_fit = fit(city, width - 2 * pad - 72 - 130 - price_width - 30, size=48, weight=700,
                            width_axis=82, smallest=30)
        draw.text((pad + 36 + 130, baseline), city, font=city_font_fit, fill=WHITE, anchor="ls")
        draw.text((width - pad - 36, baseline), price, font=price_font, fill=SUN, anchor="rs")
        line_y += row_height
    return card


# --- the common frame: brand bar, photo, footer -----------------------------------------

def frame(layout: Layout, *, photo: Path | None, iata: str, brand: str, handle: str, address: str,
          tag_text: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A yellow card with the black brand bar, the photo band and the black footer drawn."""
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
