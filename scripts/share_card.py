"""Make the picture a link to the site shows on Telegram, WhatsApp or Facebook.

Usage (from the repo root):
    python scripts/share_card.py     # writes assets/img/share/<language>.png, one per language

Each picture is 1200 x 630, the size those apps ask for. The website build puts it
on the main page by itself (share_image in src/website.py). Run this again after
changing the headline (KICKER, HEADLINE_A or HEADLINE_B in
templates/strings/<language>.yaml) or the look (scripts/share_card.html).

How it works: scripts/share_card.html is a small web page drawn like the top of the
site. Its NAMES in curly braces are filled in here, then Google Chrome opens the
page without showing a window and saves a photo of it. Needs Google Chrome and an
internet connection (for the fonts).
"""
import html
import subprocess
import sys
import tempfile
from pathlib import Path

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config, load_config  # noqa: E402
from src.i18n import load_strings  # noqa: E402
from src.photos import ASSETS_DIR  # noqa: E402
from src.website import site_guides  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"   # macOS; change it on another system
CARD = Path(__file__).with_name("share_card.html")
OUT_DIR = ASSETS_DIR / "img" / "share"
WIDTH, HEIGHT = 1200, 630
# The destinations on the card's black board: a few well-known ones, from different countries.
BOARD = ["MXP", "FCO", "STN", "VIE", "BER"]


def card_html(config: Config, language: str) -> str:
    """scripts/share_card.html with one language's wording filled in."""
    words = load_strings(language).page
    guides = site_guides(config, language)   # for the city's name in this language ("Milano")
    cities = {route.iata: route.city for route in config.routes}
    rows = ""
    for iata in BOARD:
        city = cities[iata]
        name = guides[city].display_name if city in guides else city
        rows += f"<li><b>{iata}</b><span>{html.escape(name)}</span></li>"
    values = {
        "LANG": language,
        "BRAND": html.escape(config.brand),
        "KICKER": html.escape(words["KICKER"]),
        "HEADLINE_A": html.escape(words["HEADLINE_A"]),
        "HEADLINE_B": html.escape(words["HEADLINE_B"]),
        # "https://flyfromtirana.al/" -> "flyfromtirana.al"
        "ADDRESS": html.escape(config.website.url.split("://")[-1].strip("/")),
        "ORIGIN": config.origin,
        "ROWS": rows,
    }
    page = CARD.read_text(encoding="utf-8")
    for name, value in values.items():
        page = page.replace("{" + name + "}", value)
    return page


def photograph(page: str, target: Path) -> None:
    """Save a photo of an HTML page as a PNG, WIDTH x HEIGHT."""
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "card.html"
        source.write_text(page, encoding="utf-8")
        subprocess.run([
            CHROME, "--headless=new", f"--screenshot={target}", f"--window-size={WIDTH},{HEIGHT}",
            "--hide-scrollbars", "--force-device-scale-factor=1",
            "--virtual-time-budget=5000",            # give the fonts time to arrive
            f"--user-data-dir={folder}/profile",     # its own profile: a Chrome that is open is left alone
            source.as_uri(),
        ], check=True, capture_output=True, timeout=120)


def main() -> int:
    if not Path(CHROME).exists():
        print(f"❌ Google Chrome was not found at {CHROME}")
        return 2
    config = load_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for language in config.website.languages:
        target = OUT_DIR / f"{language}.png"
        photograph(card_html(config, language), target)
        print(f"✅ {target.relative_to(ASSETS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
