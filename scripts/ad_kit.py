"""Everything to paste into Meta Ads Manager: the ads' pictures, texts and links.

Usage (from the repo root):
    python scripts/ad_kit.py                  # into ig_preview/ads/<campaign>/
    python scripts/ad_kit.py --out ~/Desktop/ads

The ads are in content/ads.yaml; src/ad_cards.py draws them. For each ad this
writes <name>_feed.jpg (1080 x 1350, for the feeds) and <name>_story.jpg (1080 x
1920, for Stories and Reels), or for a `card: video` ad <name>.mp4 (12 seconds,
src/ad_video.py, needs ffmpeg) and its cover <name>_cover.jpg, and ads.txt with, per ad, the link (with its UTM
tags, src/ads.py) and the texts for Ads Manager's boxes. Nothing is posted or
sent: the ads are made by hand in Ads Manager.

The link's utm_source=meta also tells the site to use the ads' Telegram invite
link (invite_links.meta_ads), so the weekly report counts the members the ads
brought; without that link this warns.
"""
import argparse
import sys
from pathlib import Path

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ad_cards import AD_SIZES, draw_ad  # noqa: E402
from src.ad_video import make_ad_video  # noqa: E402
from src.ads import ad_url, load_ads  # noqa: E402
from src.cards import save_jpeg  # noqa: E402
from src.config import PROJECT_ROOT, Config, ConfigError, load_config  # noqa: E402
from src.i18n import load_strings  # noqa: E402
from src.photos import post_photo  # noqa: E402
from src.reels import MUSIC_DIR, ReelError, pick_music  # noqa: E402
from src.growth import short_address  # noqa: E402
from src.social import handle, site_address  # noqa: E402
from src.website import run_every  # noqa: E402

PREVIEW_DIR = PROJECT_ROOT / "ig_preview" / "ads"


def facts(config: Config) -> dict[str, str]:
    """The {WORDS} content/ads.yaml may use, from config.yaml (Albanian)."""
    return {
        "CITIES": str(len({route.city for route in config.routes})),
        "ROUTES": str(len(config.routes)),
        "RUN_EVERY": run_every(config, load_strings("sq")),
        "LOOKAHEAD_DAYS": str(config.lookahead_days),
        "CHANNEL": short_address(config),
    }


def fill(value, words: dict[str, str]):
    """`value` with every {WORD} in its strings replaced, through lists and dicts."""
    if isinstance(value, str):
        for word, text in words.items():
            value = value.replace("{" + word + "}", text)
        return value
    if isinstance(value, list):
        return [fill(item, words) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, words) for key, item in value.items()}
    return value


def photos_of(ad: dict) -> list[str]:
    """The airport codes of every photo an ad shows (a video's scenes included)."""
    parts = [ad, *(ad.get(scene) or {} for scene in ("hook", "cities", "steps"))]
    codes = [(ad.get("sample") or {}).get("photo")]
    for part in parts:
        codes.append(part.get("photo"))
        codes += [tile.get("photo") for tile in part.get("tiles") or []]
    return [str(code) for code in codes if code]


def music_for(ad: dict) -> tuple[Path, float] | None:
    """The video's track: `music` names a file in assets/music, else the first track there."""
    chosen = str(ad.get("music") or "")
    if chosen:
        track = MUSIC_DIR / chosen
        if not track.exists():
            raise ConfigError(f"{ad['name']}: no music file assets/music/{chosen}")
        return track, 0.0
    return pick_music(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="folder for the pictures and ads.txt")
    args = parser.parse_args()
    try:
        config = load_config()
        kit = load_ads()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}")
        return 2
    words = facts(config)
    out_dir = args.out or PREVIEW_DIR / kit["campaign"]
    out_dir.mkdir(parents=True, exist_ok=True)
    problems = []
    if not config.invite_links.get("meta_ads"):
        problems.append("invite_links.meta_ads is empty: the ads' Telegram joins will count as the website's "
                        "(python scripts/invite_links.py makes the link).")

    lines = [f"Campaign: {kit['campaign']}", ""]
    for ad in kit["ads"]:
        ad = fill(ad, words)
        name = str(ad["name"])
        for code in photos_of(ad):
            photo = post_photo(code)
            if photo is None:
                problems.append(f"{name}: no photo assets/telegram/{code.lower()}.jpg (an empty board shows instead)")
            elif photo.credit:
                problems.append(f"{name}: the {code} photo needs a credit line ({photo.credit}): pick another")
        files = []
        if ad.get("card") == "video":
            try:
                video, cover = make_ad_video(ad, out_dir, brand=config.brand, address=site_address(config),
                                             handle=handle(config), music=music_for(ad))
                files += [video.name, cover.name]
            except (ReelError, ConfigError) as exc:
                problems.append(f"{name}: {exc}")
        else:
            for size in AD_SIZES:
                card = draw_ad(ad, size, brand=config.brand, address=site_address(config), handle=handle(config))
                files.append(save_jpeg(card, out_dir / f"{name}_{size}.jpg").name)
        meta = ad.get("meta") or {}
        lines += [
            f"=== {name} ===",
            f"Files: {', '.join(files)}",
            f"Website URL: {ad_url(config, campaign=kit['campaign'], content=name, page=str(ad.get('page') or ''))}",
            f"Button: {meta.get('button', '')}",
            f"Headline: {meta.get('headline', '')}",
            f"Description: {meta.get('description', '')}",
            "Primary text:",
            str(meta.get("primary_text", "")).strip(),
            "",
        ]
    (out_dir / "ads.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(kit['ads'])} ads and ads.txt to {out_dir}")
    for problem in problems:
        print(f"Warning: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
