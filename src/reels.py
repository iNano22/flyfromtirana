"""Turns a deal's card into a short video for Instagram Reels.

The Reel is the deal card brought to life: 10 seconds, 1080 x 1920, 30 frames a second.

    0.0 s   the destination's photo, slowly zooming in until the closing card
    0.1 s   the card's pieces come in one after the other (ENTRANCES): the route, the
            giant price, the city, the saving sticker slapped on, the dates, the flight
    7.0 s   a yellow closing card slides up: new deals every day, book through the
            link in the bio, the free alerts on Telegram
   10.0 s   the end (Instagram plays a Reel in a loop)

The frames are drawn with Pillow from the card's own pieces (cards.deal_pieces, in
the "reel" layout, which keeps the words clear of what Instagram lays over a Reel),
so the Reel looks exactly like the card. ffmpeg turns them into the MP4 Instagram
asks for: H.264 video, AAC sound, the index at the start of the file.

The sound is a track from assets/music, listed in its credits.json (free-licence
tracks from Pixabay, each cut to 50 energetic seconds). They take turns, one per
Reel, and each time the list comes round again a Reel uses the next 10 seconds of
its track (pick_music). With no track there, the Reel is silent. ffmpeg must be
installed (the Dockerfile installs it; on a Mac: brew install ffmpeg).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image, ImageDraw

from src.cards import (INK, LAYOUTS, SUN, archivo, cap_height, deal_background, deal_pieces, fit, mono,
                       plane_mark, save_jpeg, shade_for_words)
from src.deals import Deal
from src.photos import ASSETS_DIR

log = logging.getLogger(__name__)

LAYOUT = LAYOUTS["reel"]
SIZE = (LAYOUT.width, LAYOUT.height)
FPS = 30
DEAL_SECONDS = 7.0       # the deal itself
END_SECONDS = 3.0        # the closing card
SECONDS = DEAL_SECONDS + END_SECONDS
ZOOM = 0.08              # the photo grows by 8% while the deal shows
COME_IN = 0.45           # how long a piece of the card takes to come in (seconds)
SLIDE_UP = 0.35          # how long the closing card takes to slide up
COVER_AT = 5.0           # the moment whose frame is the Reel's cover: every piece in place

# When each piece of the card comes in (seconds), and how (see `placed` below).
# The price is the hook: it is already popping in on the very first frame (a start before 0),
# and the saving is slapped on a moment later. People decide in the first second whether to
# keep watching, so the rest follows quickly: everything is in place after 1.5 seconds.
ENTRANCES = {
    "brand": (-COME_IN, "fade"),
    "tag": (-COME_IN, "slide"),
    "price": (-0.25, "pop"),
    "sticker": (0.20, "slap"),
    "city": (0.40, "rise"),
    "dates": (0.65, "rise"),
    "details": (0.80, "rise"),
    "invite": (0.95, "slide"),
    "footer": (1.05, "fade"),
}

# The closing card's words. No emoji: the fonts have none.
END_HEADLINE = ("OFERTA TË REJA", "ÇDO DITË")
END_SUBTITLE = "Fluturime të lira nga Tirana"
# With the keyword ("OFERTA"), the closing card asks for the comment instead.
END_INVITE = ("SHKRUAJ «{keyword}»", "NË KOMENT")
END_INVITE_SUBTITLE = "Të dërgojmë çmimet e ditës në mesazh"
END_BUTTON = "REZERVO: LINKU NË BIO"
END_TELEGRAM = "Njoftime falas në Telegram"

MUSIC_DIR = ASSETS_DIR / "music"
MUSIC_PART = 10          # the seconds of a track that one Reel uses


class ReelError(Exception):
    """The video could not be made: no ffmpeg, or ffmpeg failed."""


@dataclass(frozen=True)
class Layer:
    """One piece of the card on a see-through picture of its own, and where it sits on the frame."""

    name: str
    image: Image.Image   # RGBA, cut down to what was drawn
    left: int
    top: int


# --- making a Reel -------------------------------------------------------------------------

def make_reel(deal: Deal, out_dir: Path, *, airline_names: dict[str, str], brand: str, handle: str,
              address: str, telegram: str, photo: Path | None, language: str = "sq",
              music: tuple[Path, float] | None = None, fps: int = FPS, keyword: str | None = None,
              place_video: Path | None = None) -> tuple[Path, Path]:
    """Write the deal's Reel and its cover picture: (out_dir/bgy_reel.mp4, out_dir/bgy_reel.jpg).

    telegram: the channel's address as people type it ("t.me/flyfromtirana"), for the closing
    card. music: a track and the second to start it at (pick_music), or None for silence.
    fps: frames a second (tests use fewer, to be quick). keyword: the word to comment for the
    day's prices ("OFERTA"), shown on the card and the closing card; None = not asked for.
    place_video: a video of the city to play behind the deal instead of the still photo
    (src/place_video.py); if it can't be read, the photo shows.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ReelError("ffmpeg is not installed, so no Reel can be made")
    out_dir.mkdir(parents=True, exist_ok=True)
    backdrop = video_frames(place_video, ffmpeg=ffmpeg, fps=fps) if place_video else None
    scene = Scene(deal, airline_names=airline_names, brand=brand, handle=handle, address=address,
                  telegram=telegram, photo=photo, language=language, keyword=keyword, backdrop=backdrop)
    name = deal.route.iata.lower()
    cover, video = out_dir / f"{name}_reel.jpg", out_dir / f"{name}_reel.mp4"
    cover_number = round(COVER_AT * fps)

    def frames() -> Iterator[Image.Image]:
        # In order, from the first to the last: a video behind the deal is read frame by frame.
        for number in range(round(SECONDS * fps)):
            frame = scene.frame(number / fps)
            if number == cover_number:
                save_jpeg(frame, cover)   # the cover is the Reel's own frame at COVER_AT
            yield frame

    try:
        encode(frames(), video, ffmpeg=ffmpeg, fps=fps, music=music)
    finally:
        if backdrop is not None:
            backdrop.close()   # stops the ffmpeg that was reading the city's video
    return video, cover


def video_frames(video: Path, *, ffmpeg: str, fps: int) -> Iterator[Image.Image]:
    """The frames of a video of the city, one after the other, each cut to fill a Reel's frame
    (1080 x 1920). A clip shorter than the Reel starts again from its beginning. Stops quietly
    if ffmpeg can't read the file: the Reel then goes on with the photo."""
    width, height = SIZE
    command = [
        ffmpeg, "-loglevel", "error", "-stream_loop", "-1", "-i", str(video), "-an",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}",
        "-t", f"{DEAL_SECONDS + SLIDE_UP + 1:g}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = width * height * 3
    try:
        while True:
            pixels = process.stdout.read(size)
            if len(pixels) < size:
                return
            yield Image.frombytes("RGB", SIZE, pixels)
    finally:
        process.kill()
        process.wait()


class Scene:
    """Everything the frames are made of, drawn once: the photo, the card's pieces as layers,
    and the closing card. frame(t) puts them together for the moment t (seconds)."""

    def __init__(self, deal: Deal, *, airline_names: dict[str, str], brand: str, handle: str, address: str,
                 telegram: str, photo: Path | None, language: str, keyword: str | None = None,
                 backdrop: Iterator[Image.Image] | None = None):
        # backdrop: the frames of a video of the city (video_frames), to show instead of the photo.
        # They can only be read in order, so frame(t) must then be asked for t going up.
        self.backdrop = backdrop
        self.background = deal_background(photo, deal.route.iata, LAYOUT)
        self.layers = []
        for name, paint in deal_pieces(deal, LAYOUT, airline_names=airline_names, brand=brand, handle=handle,
                                       address=address, language=language, keyword=keyword):
            canvas = Image.new("RGBA", SIZE, (0, 0, 0, 0))
            paint(canvas, ImageDraw.Draw(canvas))
            box = canvas.getbbox()
            if box:   # a piece with nothing to draw has no layer
                self.layers.append(Layer(name, canvas.crop(box), box[0], box[1]))
        self.closing = closing_card(brand=brand, address=address, telegram=telegram, keyword=keyword)

    def frame(self, t: float) -> Image.Image:
        """The frame at t seconds (RGB)."""
        if t >= DEAL_SECONDS + SLIDE_UP:
            return self.closing
        frame = self.backdrop_at(t)
        for layer in self.layers:
            shown = placed(layer, t)
            if shown:
                image, x, y, mask = shown
                frame.paste(image, (x, y), mask)
        if t > DEAL_SECONDS:   # the closing card sliding up over the deal
            top = round(SIZE[1] * (1 - ease_out((t - DEAL_SECONDS) / SLIDE_UP)))
            frame.paste(self.closing, (0, top))
        return frame

    def backdrop_at(self, t: float) -> Image.Image:
        """What is behind the words at t: the video's next frame, darkened like the photo, or the photo."""
        if self.backdrop is not None:
            picture = next(self.backdrop, None)
            if picture is not None:
                return shade_for_words(picture, LAYOUT)
            log.warning("[reel] The city's video ended or could not be read: the photo from here on")
            self.backdrop = None
        return self.photo_at(t)

    def photo_at(self, t: float) -> Image.Image:
        """The photo at t: zoomed in a little more every frame, towards a point above the middle
        (where the landmark usually is)."""
        width, height = SIZE
        zoom = 1 + ZOOM * min(t, DEAL_SECONDS) / DEAL_SECONDS
        crop_width, crop_height = width / zoom, height / zoom
        left, top = (width - crop_width) / 2, 0.42 * (height - crop_height)
        return self.background.resize(SIZE, Image.Resampling.BILINEAR,
                                      box=(left, top, left + crop_width, top + crop_height))


def placed(layer: Layer, t: float, entrances: dict[str, tuple[float, str]] | None = None
           ) -> tuple[Image.Image, int, int, Image.Image] | None:
    """How a layer shows at t: (picture, x, y, its see-through mask), or None before it comes in.

    The ways in: "fade" (appears), "slide" (from the left), "rise" (from a little lower),
    "pop" (from bigger, overshooting a little) and "slap" (like "pop", from much bigger).
    entrances: when and how each layer comes in, by name (ENTRANCES when not given).
    """
    start, how = (ENTRANCES if entrances is None else entrances).get(layer.name, (0.0, "fade"))
    progress = (t - start) / COME_IN
    if progress <= 0:
        return None
    progress = min(progress, 1.0)
    image, x, y = layer.image, layer.left, layer.top
    opacity = ease_out(progress)
    if how == "slide":
        x -= round((1 - ease_out(progress)) * 140)
    elif how == "rise":
        y += round((1 - ease_out(progress)) * 70)
    elif how in ("pop", "slap"):
        start_scale = 1.6 if how == "pop" else 2.3
        scale = start_scale - (start_scale - 1) * ease_out_back(progress)
        opacity = min(1.0, progress * 3)
        if abs(scale - 1) > 0.005:
            width, height = image.size
            image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                                 Image.Resampling.BILINEAR)
            x -= (image.width - width) // 2   # grows and shrinks around its middle
            y -= (image.height - height) // 2
    mask = image.getchannel("A")
    if opacity < 1:
        mask = mask.point(lambda value: round(value * opacity))
    return image, x, y, mask


def ease_out(progress: float) -> float:
    """Fast at first, gently settling at the end (0 -> 1)."""
    return 1 - (1 - progress) ** 3


def ease_out_back(progress: float) -> float:
    """Like ease_out, but goes a little past 1 before settling: a bounce."""
    overshoot = 1.7
    progress -= 1
    return 1 + (overshoot + 1) * progress ** 3 + overshoot * progress ** 2


def closing_card(*, brand: str, address: str, telegram: str, keyword: str | None = None) -> Image.Image:
    """The last seconds: a yellow card saying there are new deals every day and where to find them.
    With a keyword it asks for the comment instead: "SHKRUAJ «OFERTA» NË KOMENT"."""
    lines = tuple(line.format(keyword=keyword) for line in END_INVITE) if keyword else END_HEADLINE
    subtitle_text = END_INVITE_SUBTITLE if keyword else END_SUBTITLE
    card = Image.new("RGB", SIZE, SUN)
    draw = ImageDraw.Draw(card)
    width = SIZE[0]
    pad = LAYOUT.padding
    room = width - 2 * pad - LAYOUT.safe_right

    y = LAYOUT.safe_top + 60
    mark = 84
    card.paste(plane_mark(mark), (pad, y), plane_mark(mark))
    draw.text((pad + mark + 20, y + mark / 2), brand, font=archivo(46, 800, 88), fill=INK, anchor="lm")
    y += mark + 110

    headline = fit(max(lines, key=len), room, size=190, weight=800, width_axis=72, smallest=90)
    for line in lines:
        y += cap_height(headline, line)
        draw.text((pad - 6, y), line, font=headline, fill=INK, anchor="ls")
        y += 34
    y += 30
    subtitle = fit(subtitle_text, room, size=54, weight=600, width_axis=90, smallest=36)
    y += cap_height(subtitle)
    draw.text((pad, y), subtitle_text, font=subtitle, fill=INK, anchor="ls")
    y += 70

    # The button: black, with the words in yellow.
    button_font = archivo(52, 800, 84)
    button_height = cap_height(button_font) + 64
    button_width = int(draw.textlength(END_BUTTON, font=button_font)) + 80
    draw.rounded_rectangle((pad, y, pad + button_width, y + button_height), radius=18, fill=INK)
    draw.text((pad + 40, y + 32 + cap_height(button_font)), END_BUTTON, font=button_font, fill=SUN, anchor="ls")
    y += button_height + 60

    telegram_font = archivo(44, 700, 90)
    y += cap_height(telegram_font)
    draw.text((pad, y), END_TELEGRAM, font=telegram_font, fill=INK, anchor="ls")
    y += 54
    draw.text((pad, y + cap_height(mono(38))), telegram, font=mono(38), fill=INK, anchor="ls")

    # The site's address where the card's footer has it.
    draw.text((pad, LAYOUT.sign_bottom + 42), address, font=mono(34), fill=INK, anchor="lm")
    return card


# --- the video file --------------------------------------------------------------------------

def encode(frames: Iterable[Image.Image], out: Path, *, ffmpeg: str, fps: int,
           music: tuple[Path, float] | None, seconds: float = SECONDS) -> None:
    """Hand the frames to ffmpeg, which writes them as an MP4 with the music (or silence) under them.
    seconds: how long the video is (the ads' video is longer than a deal's Reel)."""
    if music:
        track, start = music
        sound = ["-ss", f"{start:g}", "-i", str(track)]
    else:
        sound = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    command = [
        ffmpeg, "-y", "-loglevel", "error",
        # The frames come through a pipe, as plain pixels.
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{SIZE[0]}x{SIZE[1]}", "-r", str(fps), "-i", "-",
        *sound,
        "-map", "0:v:0", "-map", "1:a:0",
        # What Instagram asks for: H.264 in yuv420p, a keyframe every 2 seconds, AAC sound, and the
        # index at the start of the file so it can be read while it downloads.
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-g", str(fps * 2),
        "-af", f"afade=t=in:d=0.3,afade=t=out:st={seconds - 1.5:g}:d=1.5",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-t", f"{seconds:g}", "-movflags", "+faststart", str(out),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for frame in frames:
            process.stdin.write(frame.tobytes())
        process.stdin.close()
    except BrokenPipeError:
        pass   # ffmpeg stopped early: its own message below says why
    error = process.stderr.read().decode(errors="replace").strip()
    if process.wait(timeout=300) != 0:
        raise ReelError(f"ffmpeg could not make the Reel: {error[-500:] or 'no details'}")


# --- the music -------------------------------------------------------------------------------

def pick_music(number: int, folder: Path = MUSIC_DIR) -> tuple[Path, float] | None:
    """The track for the Reel with this number (0, 1, 2, ...: how many came before it), and the
    second to start it at. None without any track (the Reel is then silent).

    The tracks are the ones listed in <folder>/credits.json whose file is there:
    [{"file": "pop.m4a", "seconds": 50, "start": 0, "title": ..., "author": ..., "source": ..., "licence": ...}].
    They take turns in that order, one per Reel, so two Reels in a row never share a track.
    Each time the list comes round again, the next MUSIC_PART seconds of the track play:
    17 tracks of 50 seconds give 85 Reels before the same sound comes back.
    """
    credits = folder / "credits.json"
    if not credits.exists():
        return None
    tracks = [track for track in json.loads(credits.read_text(encoding="utf-8"))
              if (folder / track["file"]).exists()]
    if not tracks:
        return None
    track = tracks[number % len(tracks)]
    rounds = number // len(tracks)                                     # how many times the list came round
    parts = max(1, int(float(track.get("seconds", MUSIC_PART)) // MUSIC_PART))
    return folder / track["file"], float(track.get("start", 0)) + MUSIC_PART * (rounds % parts)
