"""The paid ads: their links, their pictures, and the site's switch to the ads' Telegram link."""
import importlib.util
import re
import shutil
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from src.ad_cards import AD_SIZES, draw_ad
from src.ad_video import SECONDS, STARTS, AdVideo, make_ad_video
from src.ads import ad_url, load_ads
from src.config import PROJECT_ROOT, ConfigError, load_config
from src.website import build_site
from tests.conftest import NOW

ADS_LINK = "https://t.me/+adslink"


def ad_kit():
    """scripts/ad_kit.py as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("ad_kit", PROJECT_ROOT / "scripts" / "ad_kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path):
    base = load_config()
    return replace(base, db_path=tmp_path / "prices.db", website=replace(base.website, output_dir=tmp_path / "site"),
                   premium=replace(base.premium, enabled=False))


def test_ad_links_carry_the_utm_tags(config):
    url = ad_url(config, campaign="launch", content="photo-cheap")
    parts = urlsplit(url)
    assert url.startswith("https://flyfromtirana.al/?")
    assert parse_qs(parts.query) == {"utm_source": ["meta"], "utm_medium": ["paid"], "utm_campaign": ["launch"],
                                     "utm_content": ["photo-cheap"]}
    assert ad_url(config, campaign="c", content="a", page="/milan/").startswith("https://flyfromtirana.al/milan/?")


def test_ads_need_their_own_names(tmp_path):
    path = tmp_path / "ads.yaml"
    path.write_text("ads:\n  - {name: one, card: photo}\n  - {name: one, card: steps}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_ads(path)
    path.write_text("campaign: x\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_ads(path)


def test_every_ad_draws_in_both_sizes_with_its_words_filled_in(config):
    kit = ad_kit()
    words = kit.facts(config)
    assert words["CITIES"] == str(len({route.city for route in config.routes}))
    for ad in load_ads()["ads"]:
        ad = kit.fill(ad, words)
        assert not re.search(r"\{[A-Z_]+\}", str(ad)), f"{ad['name']} has a {{WORD}} that ad_kit.py doesn't know"
        if ad.get("card") == "video":
            continue   # the video has its own tests below
        for size in AD_SIZES.values():
            card = draw_ad(ad, size.name, brand="FlyFromTirana", address="flyfromtirana.al", handle="@flyfromtirana")
            assert card.size == (size.width, size.height)


def test_ad_visitors_get_the_ads_telegram_link(config):
    config = replace(config, invite_links={**config.invite_links, "meta_ads": ADS_LINK})
    out = config.website.output_dir
    build_site(config, "12345", now=NOW)
    for page in (out / "index.html", out / "milan" / "index.html", out / "privacy" / "index.html"):
        text = page.read_text(encoding="utf-8")
        assert f'adsLink = "{ADS_LINK}"' in text and 'source = "meta"' in text
        assert "{" + "ADS" not in text


def test_no_switch_without_an_ads_link(config):
    config = replace(config, invite_links={k: v for k, v in config.invite_links.items() if k != "meta_ads"})
    page = build_site(config, "12345", now=NOW).read_text(encoding="utf-8")
    assert "from-ad" not in page


def video_ad(config):
    kit = ad_kit()
    ad = next(ad for ad in load_ads()["ads"] if ad.get("card") == "video")
    return kit.fill(ad, kit.facts(config))


def test_the_video_has_every_scene_in_the_frame(config):
    video = AdVideo(video_ad(config), brand="FlyFromTirana", address="flyfromtirana.al", handle="@flyfromtirana")
    for t in (0.0, 1.0, STARTS["cities"] + 0.1, STARTS["steps"] + 1.0, STARTS["end"] + 2.0, SECONDS - 0.01):
        assert video.frame(t).size == (1080, 1920)
    # The hook's headline is there from the first moments (the sound-off viewer decides in a second).
    first, empty = video.frame(0.6), AdVideo({}, brand="", address="", handle="").scenes["hook"].frame(0.6, 3.2)
    assert first.tobytes() != empty.tobytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_video_file_is_written(config, tmp_path):
    video, cover = make_ad_video(video_ad(config), tmp_path, brand="FlyFromTirana", address="flyfromtirana.al",
                                 handle="@flyfromtirana", fps=2)
    assert video.name == "video-reel.mp4" and video.stat().st_size > 0
    assert cover.name == "video-reel_cover.jpg" and cover.exists()
