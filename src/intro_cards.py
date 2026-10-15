"""The pictures of the introduction posts (content/intro_posts.yaml, scripts/intro_posts.py).

Nine one-off cards, bolder than the daily ones: full-bleed photos with a dark
gradient and a giant price, a grid of real deals, big numbers on black, photo
mosaics, and a phone showing the Telegram channel or the website. Same colours
and typefaces as the rest (src/cards.py lends the fonts, the brand mark, the
photos and the footer), so the grid still reads as one brand.

Every drawer takes the post's entry from the YAML (its parameters) and the
config, and returns a 1080 x 1350 image.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.cards import (BOARD_LINE, INK, LAYOUTS, SUN, WHITE, archivo, cap_height, fit, footer, mono, photo_band,
                       plane_mark, tag, wrap)
from src.config import Config
from src.photos import ASSETS_DIR, post_photo

INTRO_DIR = ASSETS_DIR / "social" / "intro"   # screenshots the phone mock-ups show
LAYOUT = LAYOUTS["feed"]
W, H = LAYOUT.width, LAYOUT.height
PAD = LAYOUT.padding
CHAT_BG = (14, 22, 33)        # Telegram's dark chat background
LINK_BLUE = (100, 170, 255)


def draw_intro(post: dict, config: Config, *, address: str, handle: str) -> Image.Image:
    """The card of one post: `post["card"]` names the drawer below."""
    drawers = {"hook": hook, "prices": prices, "stats": stats, "mosaic": mosaic, "telegram": telegram,
               "premium": premium, "website": website, "guides": guides, "follow": follow}
    try:
        drawer = drawers[str(post.get("card"))]
    except KeyError:
        raise ValueError(f"unknown card {post.get('card')!r}: use one of {', '.join(drawers)}") from None
    card = drawer(post, config)
    footer(ImageDraw.Draw(card), LAYOUT, address=address, handle=handle)
    return card


# --- the nine cards ----------------------------------------------------------------------

def hook(post: dict, config: Config) -> Image.Image:
    """A full photo, a giant price and the city: 'Venice for €13'."""
    card = full_photo(str(post["photo"]))
    shade(card, 0, 260, start=150, end=0)
    shade(card, int(H * 0.38), H, start=0, end=240)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, config)
    y = H - LAYOUT.footer - 60
    details_font = archivo(40, 600, 92)
    line = str(post.get("line") or "")
    if line:
        draw.text((PAD, y), line, font=details_font, fill=WHITE, anchor="ls")
        y -= cap_height(details_font) + 26
    city_font = fit(str(post["city"]).upper(), W - 2 * PAD, size=150, weight=800, width_axis=72)
    draw.text((PAD, y), str(post["city"]).upper(), font=city_font, fill=WHITE, anchor="ls")
    y -= cap_height(city_font) + 20
    price_font = archivo(380, 800, 72)
    draw.text((PAD - 8, y), str(post["price"]), font=price_font, fill=SUN, anchor="ls")
    y -= cap_height(price_font) + 50
    tag(draw, str(post.get("kicker") or "NGA TIRANA"), left=PAD, middle=y, font=mono(34), fill=INK, color=SUN)
    if post.get("sticker"):
        sticker(card, str(post["sticker"]), center=(W - 300, 330), angle=-8)
    return card


def prices(post: dict, config: Config) -> Image.Image:
    """A photo with this week's deals as stickers: 'this week we found...'."""
    card = full_photo(str(post["photo"]))
    shade(card, 0, H, start=175, end=215)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, config)
    y = LAYOUT.bar + 70
    draw.text((PAD, y + cap_height(mono(36))), str(post.get("kicker") or "OFERTA REALE"), font=mono(36), fill=SUN,
              anchor="ls")
    y += 70
    title = str(post.get("title") or "KËTË JAVË GJETËM:")
    title_font = fit(title, W - 2 * PAD, size=120, weight=800, width_axis=72)
    for line in wrap(title, title_font, W - 2 * PAD)[:2]:
        y += cap_height(title_font, line)
        draw.text((PAD, y), line, font=title_font, fill=WHITE, anchor="ls")
        y += 16
    # The deals, as stickers down the card, each a little askew.
    tiles = list(post.get("tiles") or [])[:5]
    top = y + 90
    step = (LAYOUT.sign_bottom - 120 - top) // max(len(tiles), 1)
    for index, tile in enumerate(tiles):
        text = f"{str(tile['city']).upper()}  {tile['price']}"
        angle = -5 if index % 2 == 0 else 4
        x = W // 2 + (-110 if index % 2 == 0 else 110)
        sticker(card, text, center=(x, top + index * step + step // 2), angle=angle, size=76)
    return card


def stats(post: dict, config: Config) -> Image.Image:
    """Big numbers over a photo: how it works in three figures."""
    card = full_photo(str(post["photo"]))
    shade(card, 0, H, start=160, end=235)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, config)
    y = LAYOUT.bar + 50
    tag(draw, str(post.get("kicker") or "SI FUNKSIONON"), left=PAD, middle=y + 30, font=mono(34), fill=SUN, color=INK)
    y += 100
    rows = list(post.get("rows") or [])[:3]
    text_font = archivo(40, 600, 92)
    for index, row in enumerate(rows):
        figure = str(row["figure"])
        figure_font = fit(figure, W - 2 * PAD, size=170, weight=800, width_axis=72)
        y += cap_height(figure_font, figure)
        draw.text((PAD - 6, y), figure, font=figure_font, fill=SUN, anchor="ls")
        y += 62
        for line in wrap(str(row["text"]), text_font, W - 2 * PAD)[:2]:
            draw.text((PAD, y), line, font=text_font, fill=WHITE, anchor="ls")
            y += cap_height(text_font) + 16
        if index < len(rows) - 1:
            y += 56
    if post.get("sticker"):
        sticker(card, str(post["sticker"]), center=(W - 240, LAYOUT.bar + 36), angle=6)
    return card


def mosaic(post: dict, config: Config) -> Image.Image:
    """Six photo tiles with the city names, and a big sticker across them."""
    card = Image.new("RGB", (W, H), INK)
    draw = ImageDraw.Draw(card)
    brand_bar(card, draw, config)
    tiles = list(post.get("tiles") or [])[:6]
    cols, rows = 2, 3
    gap = 6
    top = LAYOUT.bar + gap
    tile_w = (W - (cols - 1) * gap) // cols
    tile_h = (LAYOUT.sign_bottom - top - (rows - 1) * gap) // rows
    for index, tile in enumerate(tiles):
        x = (index % cols) * (tile_w + gap)
        y = top + (index // cols) * (tile_h + gap)
        photo = post_photo(str(tile["photo"]))
        card.paste(photo_band(photo.path if photo else None, tile_w, tile_h, str(tile["photo"])), (x, y))
        shade(card, y + tile_h * 2 // 3, y + tile_h, start=0, end=200, left=x, right=x + tile_w)
        draw.text((x + 26, y + tile_h - 26), str(tile["city"]).upper(), font=archivo(44, 800, 80), fill=WHITE,
                  anchor="ls")
    sticker(card, str(post.get("sticker") or "24 DESTINACIONE"), center=(W // 2, (top + LAYOUT.sign_bottom) // 2),
            angle=-6, size=110)
    return card


def telegram(post: dict, config: Config) -> Image.Image:
    """A phone showing a post of the Telegram channel, over a darkened photo."""
    card = full_photo(str(post["photo"]))
    shade(card, 0, H, start=120, end=230)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, config)
    headline = str(post.get("headline") or "ÇDO OFERTË, NË ÇAST")
    y = LAYOUT.bar + 70
    draw.text((PAD, y + cap_height(mono(36))), str(post.get("kicker") or "TELEGRAM"), font=mono(36), fill=SUN,
              anchor="ls")
    y += 70
    head_font = fit(headline, W - 2 * PAD, size=104, weight=800, width_axis=72)
    for line in wrap(headline, head_font, W - 2 * PAD)[:2]:
        y += cap_height(head_font, line)
        draw.text((PAD, y), line, font=head_font, fill=WHITE, anchor="ls")
        y += 16
    screen = chat_screen(post, config)
    phone_img = phone(screen)
    card.paste(phone_img, (W - phone_img.width - 70, LAYOUT.sign_bottom - phone_img.height + 90), phone_img)
    tag(draw, str(post.get("pill") or "t.me/flyfromtirana"), left=PAD, middle=LAYOUT.sign_bottom - 70,
        font=mono(34), fill=SUN, color=INK)
    return card


def premium(post: dict, config: Config) -> Image.Image:
    """'6 hours earlier' over a photo, with a PREMIUM sticker and three pills."""
    card = full_photo(str(post["photo"]))
    shade(card, 0, 300, start=170, end=0)
    shade(card, int(H * 0.25), H, start=0, end=225)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, config)
    y = LAYOUT.bar + 50
    tag(draw, str(post.get("kicker") or "PREMIUM"), left=PAD, middle=y + 30, font=mono(34), fill=SUN, color=INK)
    figure = str(post.get("figure") or "6 ORË")
    figure_font = fit(figure, W - 2 * PAD - 60, size=300, weight=800, width_axis=72)
    y += 110 + cap_height(figure_font, figure)
    draw.text((PAD - 8, y), figure, font=figure_font, fill=SUN, anchor="ls")
    y += 40
    headline = str(post.get("headline") or "MË PARË SE TË GJITHË")
    head_font = fit(headline, W - 2 * PAD, size=110, weight=800, width_axis=72)
    for line in wrap(headline, head_font, W - 2 * PAD)[:2]:
        y += cap_height(head_font, line)
        draw.text((PAD, y), line, font=head_font, fill=WHITE, anchor="ls")
        y += 16
    y += 60
    for pill_text in list(post.get("pills") or [])[:4]:
        tag(draw, str(pill_text), left=PAD, middle=y + 36, font=archivo(44, 800, 84), fill=SUN, color=INK)
        y += 100
    y += 24
    text_font = archivo(40, 500, 94)
    for line in wrap(str(post.get("text") or ""), text_font, W - 2 * PAD)[:3]:
        y += cap_height(text_font)
        draw.text((PAD, y), line, font=text_font, fill=WHITE, anchor="ls")
        y += 16
    sticker(card, str(post.get("sticker") or "PREMIUM"), center=(W - 220, LAYOUT.bar + 110), angle=8)
    return card


def website(post: dict, config: Config) -> Image.Image:
    """The website on a tilted phone, on yellow."""
    card = Image.new("RGB", (W, H), SUN)
    draw = ImageDraw.Draw(card)
    brand_bar(card, draw, config)
    y = LAYOUT.bar + 70
    draw.text((PAD, y + cap_height(mono(36))), str(post.get("kicker") or "FAQJA JONË"), font=mono(36), fill=INK,
              anchor="ls")
    y += 70
    headline = str(post.get("headline") or "flyfromtirana.al")
    head_font = fit(headline, W - 2 * PAD, size=120, weight=800, width_axis=72)
    y += cap_height(head_font, headline)
    draw.text((PAD, y), headline, font=head_font, fill=INK, anchor="ls")
    y += 34
    sub_font = archivo(42, 600, 92)
    for line in wrap(str(post.get("sub") or ""), sub_font, W - 2 * PAD)[:3]:
        y += cap_height(sub_font)
        draw.text((PAD, y), line, font=sub_font, fill=INK, anchor="ls")
        y += 16
    screenshot = INTRO_DIR / str(post.get("screenshot") or "site.png")
    if screenshot.exists():
        with Image.open(screenshot) as source:
            screen = source.convert("RGB")
    else:
        screen = Image.new("RGB", (430, 900), WHITE)
    phone_img = phone(screen, width=600).rotate(-7, resample=Image.Resampling.BICUBIC, expand=True)
    card.paste(phone_img, (W - phone_img.width + 30, y + 10), phone_img)
    return card


def guides(post: dict, config: Config) -> Image.Image:
    """A phone showing a city's guide page, over a darkened photo: '19 city guides'."""
    card = full_photo(str(post["photo"]))
    shade(card, 0, H, start=140, end=230)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, config)
    y = LAYOUT.bar + 70
    draw.text((PAD, y + cap_height(mono(36))), str(post.get("kicker") or "UDHËZUESIT"), font=mono(36), fill=SUN,
              anchor="ls")
    y += 50
    figure = str(post.get("figure") or "19")
    figure_font = archivo(250, 800, 72)
    y += cap_height(figure_font, figure)
    draw.text((PAD - 8, y), figure, font=figure_font, fill=SUN, anchor="ls")
    y += 26
    headline = str(post.get("headline") or "UDHËZUES QYTETESH")
    head_font = fit(headline, W - 2 * PAD, size=100, weight=800, width_axis=72)
    y += cap_height(head_font, headline)
    draw.text((PAD, y), headline, font=head_font, fill=WHITE, anchor="ls")
    y += 34
    sub_font = archivo(36, 600, 92)
    for line in wrap(str(post.get("sub") or ""), sub_font, W - 2 * PAD)[:2]:
        y += cap_height(sub_font)
        draw.text((PAD, y), line, font=sub_font, fill=WHITE, anchor="ls")
        y += 14
    screenshot = INTRO_DIR / str(post.get("screenshot") or "guide.png")
    if screenshot.exists():
        with Image.open(screenshot) as source:
            screen = source.convert("RGB")
    else:
        screen = Image.new("RGB", (430, 900), WHITE)
    phone_img = phone(screen, width=470)
    card.paste(phone_img, (W - phone_img.width - 50, y + 50), phone_img)
    return card

def follow(post: dict, config: Config) -> Image.Image:
    """A full photo, 'follow us' and three pills with what comes every week."""
    card = full_photo(str(post["photo"]))
    shade(card, 0, 260, start=150, end=0)
    shade(card, H // 3, H, start=0, end=235)
    draw = ImageDraw.Draw(card)
    brand_over_photo(card, draw, config)
    headline = str(post.get("headline") or "NDIQNA")
    head_font = fit(headline, W - 2 * PAD, size=230, weight=800, width_axis=72)
    y = LAYOUT.bar + 330 + cap_height(head_font, headline)
    draw.text((PAD - 8, y), headline, font=head_font, fill=SUN, anchor="ls")
    y += 60
    for pill_text in list(post.get("pills") or [])[:4]:
        tag(draw, str(pill_text), left=PAD, middle=y + 36, font=archivo(44, 800, 84), fill=SUN, color=INK)
        y += 100
    y += 20
    sub_font = archivo(44, 600, 92)
    for line in wrap(str(post.get("sub") or ""), sub_font, W - 2 * PAD)[:2]:
        y += cap_height(sub_font)
        draw.text((PAD, y), line, font=sub_font, fill=WHITE, anchor="ls")
        y += 16
    return card


# --- pieces ------------------------------------------------------------------------------

def full_photo(iata: str) -> Image.Image:
    """The route's photo filling the whole card (the footer is drawn over it later)."""
    photo = post_photo(iata)
    card = Image.new("RGB", (W, H), INK)
    card.paste(photo_band(photo.path if photo else None, W, LAYOUT.sign_bottom, iata), (0, 0))
    return card


def shade(card: Image.Image, top: int, bottom: int, *, start: int, end: int, left: int = 0,
          right: int | None = None) -> None:
    """Darken a band of the card with a vertical gradient: `start` opacity (0-255) at the
    top of the band, `end` at the bottom."""
    right = W if right is None else right
    height, width = bottom - top, right - left
    if height <= 0 or width <= 0:
        return
    ramp = Image.linear_gradient("L").resize((width, height))   # 0 at the top, 255 at the bottom
    mask = ramp.point(lambda v: start + (end - start) * v // 255)
    card.paste(Image.new("RGB", (width, height), INK), (left, top), mask)


def brand_bar(card: Image.Image, draw: ImageDraw.ImageDraw, config: Config) -> None:
    """The black brand bar at the top, as on the daily cards (without a tag)."""
    draw.rectangle((0, 0, W, LAYOUT.bar), fill=INK)
    mark = LAYOUT.bar - 32
    card.paste(plane_mark(mark), (PAD, (LAYOUT.bar - mark) // 2))
    draw.text((PAD + mark + 18, LAYOUT.bar / 2), config.brand, font=archivo(38, 800, 88), fill=WHITE, anchor="lm")


def brand_over_photo(card: Image.Image, draw: ImageDraw.ImageDraw, config: Config) -> None:
    """The brand mark and name over a photo (the top of the photo is shaded first)."""
    mark = 64
    card.paste(plane_mark(mark), (PAD, 32), plane_mark(mark))
    draw.text((PAD + mark + 18, 32 + mark / 2), config.brand, font=archivo(38, 800, 88), fill=WHITE, anchor="lm")


def sticker(card: Image.Image, text: str, *, center: tuple[int, int], angle: float, size: int = 44) -> None:
    """A rotated yellow label with black text, like a sticker on the picture."""
    font = archivo(size, 800, 80)
    pad_x, pad_y = 34, 26
    width = int(font.getlength(text)) + 2 * pad_x
    height = cap_height(font) + 2 * pad_y
    label = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(label)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=14, fill=SUN)
    draw.text((pad_x, pad_y + cap_height(font)), text, font=font, fill=INK, anchor="ls")
    label = label.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    shadow = Image.new("RGBA", label.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), (0, 0, label.width, label.height), label)
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    x, y = center[0] - label.width // 2, center[1] - label.height // 2
    card.paste(shadow, (x + 6, y + 12), shadow)
    card.paste(label, (x, y), label)


def phone(screen: Image.Image, *, width: int = 440) -> Image.Image:
    """A phone (RGBA): a black bezel with rounded corners around the top of `screen`."""
    bezel = 14
    screen_w = width - 2 * bezel
    screen_h = int(screen_w * 2.05)
    shown = screen.resize((screen_w, int(screen.height * screen_w / screen.width)), Image.Resampling.LANCZOS)
    shown = shown.crop((0, 0, screen_w, min(screen_h, shown.height)))
    body = Image.new("RGBA", (width, screen_h + 2 * bezel), (0, 0, 0, 0))
    draw = ImageDraw.Draw(body)
    draw.rounded_rectangle((0, 0, width - 1, body.height - 1), radius=58, fill=INK)
    mask = Image.new("L", shown.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, shown.width - 1, shown.height - 1), radius=44, fill=255)
    body.paste(shown, (bezel, bezel), mask)
    # A soft shadow under the phone.
    shadow = Image.new("RGBA", (body.width + 80, body.height + 80), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((40, 50, body.width + 40, body.height + 50), radius=58, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    shadow.paste(body, (40, 40), body)
    return shadow


def chat_screen(post: dict, config: Config) -> Image.Image:
    """A Telegram chat with one of the channel's posts: the photo and the text in a bubble."""
    width, height = 430, 900
    screen = Image.new("RGB", (width, height), CHAT_BG)
    draw = ImageDraw.Draw(screen)
    # The channel's name at the top.
    draw.rectangle((0, 0, width, 84), fill=(23, 33, 43))
    draw.text((24, 42), config.brand, font=archivo(30, 700, 90), fill=WHITE, anchor="lm")
    draw.text((24, 68), str(post.get("members") or "kanal"), font=archivo(20, 500, 96), fill=(140, 150, 160), anchor="lm")
    # The bubble: the route's photo, then the lines of the post.
    bubble = (16, 110, width - 16, height - 20)
    draw.rounded_rectangle(bubble, radius=22, fill=(33, 46, 61))
    sample = post.get("sample") or {}
    photo = post_photo(str(sample.get("photo") or "BGY"))
    picture = photo_band(photo.path if photo else None, width - 32, 230, str(sample.get("photo") or "BGY"))
    mask = Image.new("L", picture.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, picture.width - 1, picture.height - 1), radius=22, fill=255)
    screen.paste(picture, (16, 110), mask)
    y = 110 + 230 + 26
    for index, line in enumerate(list(sample.get("lines") or [])[:8]):
        bold = index == 0
        font = archivo(28 if bold else 25, 700 if bold else 500, 92)
        colour = LINK_BLUE if str(line).startswith("Rezervo") else WHITE
        draw.text((34, y + cap_height(font)), str(line), font=font, fill=colour, anchor="ls")
        y += 44 if bold else 38
    draw.text((width - 34, height - 36), str(sample.get("time") or "08:17"), font=archivo(20, 500, 96),
              fill=(140, 150, 160), anchor="rs")
    return screen
