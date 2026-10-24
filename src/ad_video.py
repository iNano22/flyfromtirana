"""The ads' video: 12 seconds, 1080 x 1920, for Reels and Stories ads (content/ads.yaml, card: video).

Evergreen like the pictures (src/ad_cards.py): no price, no date. Four scenes, each
sliding up over the one before, readable with the sound off:

    0.0 s   hook     a city photo zooming in, "BILETA TË LIRA / NGA TIRANA" pops in at once
    3.2 s   cities   six city photos come in one by one, then a "110 QYTETE" sticker
    6.4 s   steps    how it works, in three big lines over a dark photo
    9.0 s   end      a yellow card: free in Telegram, the button, the site's address
   12.0 s   the end

Meta lays its own name over the top ~14% of a Reels ad and the caption and button
over the bottom ~35%, so the words stay between TOP and BOTTOM. The frames are drawn
with Pillow and joined by ffmpeg, the same way as a deal's Reel (src/reels.py lends
the entrances, the easing and the encoding).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from src.ad_cards import lines_of, longest
from src.cards import (INK, SUN, WHITE, archivo, cap_height, fit, fit_mono, mono, photo_band, plane_mark, put,
                       save_jpeg, shade, sticker, tag, wrap)
from src.photos import post_photo
from src.reels import Layer, ReelError, ease_out, encode, placed

W, H = 1080, 1920
FPS = 30
PAD = 64
TOP = 270          # Meta's name above this
BOTTOM = 1250      # Meta's caption and button below this
FOOTER_MID = 1300  # the address line, just under the words (it may be covered: it is on the end card too)
ROOM = W - 2 * PAD - 60   # text width: Meta's like/share buttons sit on the right
SLIDE_UP = 0.3     # how long a scene takes to slide up over the one before
ZOOM = 0.08        # how much a photo grows during its scene

# When each scene starts (seconds), in order; the video ends at SECONDS.
STARTS = {"hook": 0.0, "cities": 3.2, "steps": 6.4, "end": 9.0}
SECONDS = 12.0
COVER_AT = 2.8     # the cover picture: the hook with everything in place


def make_ad_video(ad: dict, out_dir: Path, *, brand: str, address: str, handle: str,
                  music: tuple[Path, float] | None = None, fps: int = FPS) -> tuple[Path, Path]:
    """Write the ad's video and its cover: (out_dir/<name>.mp4, out_dir/<name>_cover.jpg).
    music: a track and the second to start it at (reels.pick_music), or None for silence."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ReelError("ffmpeg is not installed, so the video can't be made (on a Mac: brew install ffmpeg)")
    video = AdVideo(ad, brand=brand, address=address, handle=handle)
    name = str(ad["name"])
    cover = save_jpeg(video.frame(COVER_AT), out_dir / f"{name}_cover.jpg")
    out = out_dir / f"{name}.mp4"
    frames = (video.frame(number / fps) for number in range(round(SECONDS * fps)))
    encode(frames, out, ffmpeg=ffmpeg, fps=fps, music=music, seconds=SECONDS)
    return out, cover


class Scene:
    """One scene: a background (a photo that zooms, or a still picture) and layers that come in."""

    def __init__(self, background: Image.Image, *, zoom: bool):
        self.background = background
        self.zoom = zoom
        self.layers: list[Layer] = []
        self.entrances: dict[str, tuple[float, str]] = {}

    def add(self, name: str, start: float, how: str, paint: Callable[[Image.Image, ImageDraw.ImageDraw], None]) -> None:
        """A layer called `name`, drawn by paint(canvas, draw) on a see-through frame, coming in
        at `start` seconds into the scene, the `how` way (see reels.placed)."""
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        paint(canvas, ImageDraw.Draw(canvas))
        box = canvas.getbbox()
        if box:
            self.layers.append(Layer(name, canvas.crop(box), box[0], box[1]))
            self.entrances[name] = (start, how)

    def frame(self, t: float, length: float) -> Image.Image:
        """The scene t seconds after it started (RGB)."""
        if self.zoom:
            zoom = 1 + ZOOM * min(t, length) / length
            crop_w, crop_h = W / zoom, H / zoom
            left, top = (W - crop_w) / 2, 0.42 * (H - crop_h)
            frame = self.background.resize((W, H), Image.Resampling.BILINEAR,
                                           box=(left, top, left + crop_w, top + crop_h))
        else:
            frame = self.background.copy()
        for layer in self.layers:
            shown = placed(layer, t, self.entrances)
            if shown:
                image, x, y, mask = shown
                frame.paste(image, (x, y), mask)
        return frame


class AdVideo:
    """The four scenes, drawn once; frame(t) puts the video's moment t together."""

    def __init__(self, ad: dict, *, brand: str, address: str, handle: str):
        self.brand, self.address, self.handle = brand, address, handle
        self.scenes = {"hook": self.hook(ad.get("hook") or {}), "cities": self.cities(ad.get("cities") or {}),
                       "steps": self.steps(ad.get("steps") or {}), "end": self.end(ad.get("end") or {})}
        names = list(STARTS)
        self.lengths = {name: (STARTS[names[i + 1]] if i + 1 < len(names) else SECONDS) - STARTS[name]
                        for i, name in enumerate(names)}

    def frame(self, t: float) -> Image.Image:
        names = list(STARTS)
        index = max(i for i, name in enumerate(names) if STARTS[name] <= t)
        name = names[index]
        local = t - STARTS[name]
        frame = self.scenes[name].frame(local, self.lengths[name])
        if index > 0 and local < SLIDE_UP:   # sliding up over the end of the scene before
            before = names[index - 1]
            under = self.scenes[before].frame(self.lengths[before], self.lengths[before])
            top = round(H * (1 - ease_out(local / SLIDE_UP)))
            under.paste(frame.crop((0, 0, W, H - top)), (0, top))
            return under
        return frame

    # --- the scenes ---------------------------------------------------------------------------

    def hook(self, part: dict) -> Scene:
        """A city photo, the kicker and a big two-line headline, at once: the first second sells."""
        background = photo(str(part.get("photo") or "MXP"))
        shade(background, 0, TOP + 260, start=170, end=0)
        shade(background, int(H * 0.28), H, start=0, end=230)
        scene = Scene(background, zoom=True)
        self.brand_layer(scene, start=0.0)
        head_lines = lines_of(part.get("headline") or ["BILETA TË LIRA", "NGA TIRANA"])
        head_font = fit(longest(head_lines), ROOM, size=160, weight=800, width_axis=72)
        sub_font = archivo(46, 600, 92)
        sub_lines = wrap(str(part.get("sub") or ""), sub_font, ROOM)[:2]
        # From the bottom up, so the block ends at BOTTOM.
        y = BOTTOM - 20
        sub_at = []
        for line in reversed(sub_lines):
            sub_at.insert(0, (line, y))
            y -= cap_height(sub_font) + 18
        y -= 30
        head_at = []
        for line in reversed(head_lines):
            head_at.insert(0, (line, y))
            y -= cap_height(head_font) + 22
        kicker_y = y - 48
        scene.add("kicker", 0.05, "slide", lambda c, d: tag_on(d, str(part.get("kicker") or "NGA RINASI"),
                                                               PAD, kicker_y))
        for index, (line, at) in enumerate(head_at):
            colour = SUN if index == 0 else WHITE
            scene.add(f"head{index}", 0.1 + 0.3 * index, "pop" if index == 0 else "rise",
                      lambda c, d, line=line, at=at, colour=colour: d.text((PAD - 6, at), line, font=head_font,
                                                                           fill=colour, anchor="ls"))
        for index, (line, at) in enumerate(sub_at):
            scene.add(f"sub{index}", 0.8 + 0.1 * index, "rise",
                      lambda c, d, line=line, at=at: d.text((PAD, at), line, font=sub_font, fill=WHITE, anchor="ls"))
        if part.get("sticker"):
            scene.add("sticker", 1.2, "slap", lambda c, d: sticker(c, str(part["sticker"]), center=(W - 300, TOP + 250),
                                                                   angle=-7, size=56))
        self.footer_layer(scene, start=0.0)
        return scene

    def cities(self, part: dict) -> Scene:
        """Six city photos come in one by one, a big sticker slaps on, then the headline."""
        scene = Scene(Image.new("RGB", (W, H), INK), zoom=False)
        self.brand_layer(scene, start=0.0, how="fade")
        head_lines = lines_of(part.get("headline") or ["NGA TIRANA,", "KU TË DUASH"])
        head_font = fit(longest(head_lines), ROOM, size=120, weight=800, width_axis=72)
        text_height = len(head_lines) * (cap_height(head_font) + 20)
        gap, cols, rows = 6, 2, 3
        grid_top, grid_bottom = TOP + 130, BOTTOM - text_height - 50
        tile_w = (W - (cols - 1) * gap) // cols
        tile_h = (grid_bottom - grid_top - (rows - 1) * gap) // rows
        for index, tile in enumerate(list(part.get("tiles") or [])[:cols * rows]):
            x = (index % cols) * (tile_w + gap)
            y = grid_top + (index // cols) * (tile_h + gap)
            picture = city_tile(str(tile["photo"]), str(tile["city"]), tile_w, tile_h)
            scene.add(f"tile{index}", 0.05 + 0.15 * index, "rise", lambda c, d, p=picture, x=x, y=y: c.paste(p, (x, y)))
        scene.add("sticker", 1.05, "slap", lambda c, d: sticker(c, str(part.get("sticker") or ""),
                                                                center=(W // 2, (grid_top + grid_bottom) // 2),
                                                                angle=-6, size=104))
        y = grid_bottom + 40
        for index, line in enumerate(head_lines):
            y += cap_height(head_font)
            colour = SUN if index == 0 else WHITE
            scene.add(f"head{index}", 1.4 + 0.2 * index, "rise",
                      lambda c, d, line=line, y=y, colour=colour: d.text((PAD - 4, y), line, font=head_font,
                                                                         fill=colour, anchor="ls"))
            y += 20
        self.footer_layer(scene, start=0.0)
        return scene

    def steps(self, part: dict) -> Scene:
        """How it works: three big lines, each with a small one under it, over a dark photo."""
        background = photo(str(part.get("photo") or "BCN"))
        shade(background, 0, H, start=180, end=235)
        scene = Scene(background, zoom=True)
        self.brand_layer(scene, start=0.0, how="fade")
        rows = list(part.get("rows") or [])[:3]
        text_font = archivo(42, 600, 92)
        step = (BOTTOM - (TOP + 170)) // max(len(rows), 1)
        for index, row in enumerate(rows):
            figure = str(row["figure"]).upper()
            figure_font = fit(figure, ROOM, size=150, weight=800, width_axis=72)
            top = TOP + 170 + index * step
            baseline = top + cap_height(figure_font, figure)
            scene.add(f"figure{index}", 0.1 + 0.65 * index, "pop",
                      lambda c, d, f=figure, b=baseline, font=figure_font: d.text((PAD - 6, b), f, font=font,
                                                                                    fill=SUN, anchor="ls"))
            y = baseline + 24
            for number, line in enumerate(wrap(str(row.get("text") or ""), text_font, ROOM)[:2]):
                y += cap_height(text_font) + 10
                scene.add(f"text{index}_{number}", 0.3 + 0.65 * index, "rise",
                          lambda c, d, line=line, y=y: d.text((PAD, y), line, font=text_font, fill=WHITE,
                                                              anchor="ls"))
        self.footer_layer(scene, start=0.0)
        return scene

    def end(self, part: dict) -> Scene:
        """The yellow closing card: the headline, the button, the channel and the site's address."""
        card = Image.new("RGB", (W, H), SUN)
        draw = ImageDraw.Draw(card)
        mark = 84
        y = TOP + 40
        card.paste(plane_mark(mark), (PAD, y), plane_mark(mark))
        draw.text((PAD + mark + 20, y + mark / 2), self.brand, font=archivo(46, 800, 88), fill=INK, anchor="lm")
        scene = Scene(card, zoom=False)
        head_lines = lines_of(part.get("headline") or ["OFERTAT FALAS", "NË TELEGRAM"])
        head_font = fit(longest(head_lines), ROOM, size=170, weight=800, width_axis=72, smallest=90)
        y += mark + 120
        for index, line in enumerate(head_lines):
            y += cap_height(head_font, line)
            scene.add(f"head{index}", 0.25 + 0.2 * index, "rise",
                      lambda c, d, line=line, y=y: d.text((PAD - 6, y), line, font=head_font, fill=INK, anchor="ls"))
            y += 34
        y += 20
        line_font = archivo(50, 600, 90)
        for index, line in enumerate(wrap(str(part.get("line") or ""), line_font, ROOM)[:2]):
            y += cap_height(line_font)
            scene.add(f"line{index}", 0.6, "rise",
                      lambda c, d, line=line, y=y: d.text((PAD, y), line, font=line_font, fill=INK, anchor="ls"))
            y += 22
        y += 50
        button = str(part.get("button") or "SHIKO OFERTAT")
        button_font = archivo(56, 800, 84)

        def paint_button(c, d, y=y):
            height = cap_height(button_font) + 66
            width = int(d.textlength(button, font=button_font)) + 84
            d.rounded_rectangle((PAD, y, PAD + width, y + height), radius=18, fill=INK)
            d.text((PAD + 42, y + 33 + cap_height(button_font)), button, font=button_font, fill=SUN, anchor="ls")

        scene.add("button", 0.9, "pop", paint_button)
        scene.add("address", 0.3, "fade", lambda c, d: d.text((PAD, FOOTER_MID), self.address,
                                                              font=fit_mono(self.address, ROOM, size=40),
                                                              fill=INK, anchor="lm"))
        return scene

    # --- pieces ---------------------------------------------------------------------------------

    def brand_layer(self, scene: Scene, *, start: float, how: str = "fade") -> None:
        """The plane mark and the brand name, top left, under Meta's own name."""
        mark = 68

        def paint(c, d):
            put(c, plane_mark(mark), (PAD, TOP + 30))
            d.text((PAD + mark + 18, TOP + 30 + mark / 2), self.brand, font=archivo(40, 800, 88), fill=WHITE,
                   anchor="lm")

        scene.add("brand", start, how, paint)

    def footer_layer(self, scene: Scene, *, start: float) -> None:
        """The site's address and the account's name, under the words."""
        def paint(c, d):
            d.text((PAD, FOOTER_MID), self.address, font=fit_mono(self.address, W - 2 * PAD - 340, size=32),
                   fill=WHITE, anchor="lm")
            d.text((W - PAD, FOOTER_MID), self.handle, font=archivo(34, 700, 90), fill=SUN, anchor="rm")

        scene.add("footer", start, "fade", paint)


def photo(iata: str) -> Image.Image:
    """The route's photo filling the frame."""
    found = post_photo(iata)
    return photo_band(found.path if found else None, W, H, iata)


def city_tile(iata: str, city: str, width: int, height: int) -> Image.Image:
    """A city's photo with its name at the bottom, darkened under the name."""
    found = post_photo(iata)
    tile = photo_band(found.path if found else None, width, height, iata)
    shade(tile, height // 2, height, start=0, end=210)
    ImageDraw.Draw(tile).text((26, height - 26), city.upper(), font=archivo(48, 800, 80), fill=WHITE, anchor="ls")
    return tile


def tag_on(draw: ImageDraw.ImageDraw, text: str, left: int, middle: int) -> None:
    """A yellow label with black mono text (cards.tag, at a fixed size)."""
    tag(draw, text, left=left, middle=middle, font=mono(36), fill=SUN, color=INK)
