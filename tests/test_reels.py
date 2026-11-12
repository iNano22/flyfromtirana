"""The Reel: the card's pieces coming in over time, the closing card, the MP4 and the music's turns."""
import json
import shutil

import pytest
from PIL import Image

from src.cards import LAYOUTS, SUN
from src.photos import POST_PHOTOS_DIR
from src.reels import COVER_AT, DEAL_SECONDS, ENTRANCES, SECONDS, Scene, make_reel, pick_music, placed
from tests.test_cards import AIRLINES, make_deal

LOOK = dict(airline_names=AIRLINES, brand="FlyFromTirana", handle="@flyfromtirana", address="flyfromtirana.al",
            telegram="t.me/flyfromtirana")


@pytest.fixture
def scene(route):
    return Scene(make_deal(route), photo=POST_PHOTOS_DIR / "bgy.jpg", language="sq", keyword="OFERTA", **LOOK)


def test_every_piece_of_the_card_has_an_entrance(scene):
    assert {layer.name for layer in scene.layers} == set(ENTRANCES)


def test_pieces_come_in_one_after_the_other(scene):
    price = next(layer for layer in scene.layers if layer.name == "price")
    assert placed(price, ENTRANCES["price"][0] - 0.01) is None              # not yet
    image, x, y, mask = placed(price, ENTRANCES["price"][0] + 0.1)
    assert image.width > price.image.width                                 # popping in: bigger at first
    image, x, y, mask = placed(price, COVER_AT)
    assert (image, x, y) == (price.image, price.left, price.top)           # then in its place
    assert mask.getextrema()[1] == 255                                     # and fully shown


def test_frames_start_on_the_price_and_end_on_the_closing_card(scene):
    first, cover, last = scene.frame(0), scene.frame(COVER_AT), scene.frame(SECONDS - 0.1)
    assert first.size == cover.size == (1080, 1920)
    # The hook: the very first frame already shows the price popping in (and the brand)...
    price = next(layer for layer in scene.layers if layer.name == "price")
    assert placed(price, 0) is not None
    assert first.getpixel((LAYOUTS["reel"].padding + 6, 300)) == SUN     # the brand's mark
    # ...while the rest follows: the city isn't there yet, and all is in place within 1.5 seconds.
    city = next(layer for layer in scene.layers if layer.name == "city")
    assert placed(city, 0) is None
    assert max(start for start, _ in ENTRANCES.values()) + 0.45 <= 1.5
    assert last.getpixel((540, 1800)) == SUN                             # the yellow closing card
    assert scene.frame(DEAL_SECONDS + 0.1).getpixel((540, 100)) != SUN    # still sliding up


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_make_reel_writes_an_mp4_and_its_cover(route, tmp_path):
    video, cover = make_reel(make_deal(route), tmp_path, photo=POST_PHOTOS_DIR / "bgy.jpg", fps=4, **LOOK)
    assert (video.name, cover.name) == ("bgy_reel.mp4", "bgy_reel.jpg")
    data = video.read_bytes()
    assert data[4:8] == b"ftyp"                          # an MP4
    assert data.index(b"moov") < data.index(b"mdat")     # its index first, as Instagram wants
    with Image.open(cover) as picture:
        assert (picture.format, picture.size) == ("JPEG", (1080, 1920))


def test_music_takes_turns_a_track_a_reel_then_the_next_part_of_it(tmp_path):
    assert pick_music(0, tmp_path) is None                                         # no credits.json: silent
    (tmp_path / "a.m4a").write_bytes(b"a")
    (tmp_path / "b.m4a").write_bytes(b"b")
    tracks = [{"file": "a.m4a", "seconds": 30, "start": 2}, {"file": "gone.m4a"}, {"file": "b.m4a", "seconds": 50}]
    (tmp_path / "credits.json").write_text(json.dumps(tracks), encoding="utf-8")
    picked = [pick_music(number, tmp_path) for number in range(7)]
    a, b = tmp_path / "a.m4a", tmp_path / "b.m4a"
    assert picked == [(a, 2.0), (b, 0.0),        # a missing file is skipped
                      (a, 17.0), (b, 15.0),      # the list came round: the next 15 seconds
                      (a, 2.0), (b, 30.0),       # a's 30 seconds are used up: from its start again
                      (a, 17.0)]


def test_a_reel_is_long_enough_to_read_and_its_music_lasts_that_long():
    from src.reels import END_SECONDS, MUSIC_PART
    assert DEAL_SECONDS >= 10 and END_SECONDS >= 4       # 7 and 3 seconds were too short to read
    assert MUSIC_PART >= SECONDS                         # a Reel never runs past its part of the track


def test_the_music_in_the_repo_is_listed_with_its_licence():
    from src.reels import MUSIC_DIR
    credits = json.loads((MUSIC_DIR / "credits.json").read_text(encoding="utf-8"))
    files = {path.name for path in MUSIC_DIR.glob("*.m4a")}
    assert {track["file"] for track in credits} == files and len(files) >= 10
    assert all(track["licence"].startswith("Pixabay Content License") and track["source"] for track in credits)


def test_the_closing_card_asks_for_the_comment_only_with_a_keyword():
    from src.reels import closing_card
    look = dict(brand="FlyFromTirana", address="flyfromtirana.al", telegram="t.me/flyfromtirana")
    assert closing_card(keyword="OFERTA", **look).tobytes() != closing_card(**look).tobytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_reel_plays_the_citys_video_behind_the_deal(route, tmp_path):
    import subprocess
    clip = tmp_path / "clip.mp4"   # one second of plain red, wide: shorter than the Reel, and not upright
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=4",
                    "-t", "1", "-pix_fmt", "yuv420p", str(clip)], check=True)
    video, cover = make_reel(make_deal(route), tmp_path, photo=POST_PHOTOS_DIR / "bgy.jpg", fps=4,
                             place_video=clip, **LOOK)
    with Image.open(cover) as picture:   # the cover is a frame at 5 seconds: the clip, played again and again
        red, green, blue = picture.convert("RGB").getpixel((900, 500))   # the photo's sky there is blue
    assert picture.size == (1080, 1920) and red > 120 and green < 60 and blue < 60
    # A file that is no video: the Reel is made all the same, with the photo.
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    video, cover = make_reel(make_deal(route), tmp_path / "again", photo=POST_PHOTOS_DIR / "bgy.jpg", fps=4,
                             place_video=broken, **LOOK)
    assert video.read_bytes()[4:8] == b"ftyp"
