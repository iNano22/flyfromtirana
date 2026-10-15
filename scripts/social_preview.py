"""Draw sample Instagram/Facebook cards for made-up deals, to check the look.

Usage (from the repo root):
    python scripts/social_preview.py              # writes ig_preview/<iata>_feed.jpg and _story.jpg,
                                                  # top_feed.jpg (the weekly cover) and guide_rome_*.jpg (its carousel)
    python scripts/social_preview.py --out DIR    # somewhere else

Nothing is sent anywhere. The deals below are invented (prices, dates, airlines);
only the routes, photos and wording come from the real configuration.
"""
import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Make "import src..." work when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cards import draw_guide_card, draw_list_card, draw_top_card, save_cards, save_jpeg  # noqa: E402
from src.config import load_config  # noqa: E402
from src.deals import Deal  # noqa: E402
from src.guides import load_guides  # noqa: E402
from src.photos import post_photo  # noqa: E402
from src.scanner import Quote  # noqa: E402
from src.social import guide_sections, site_address  # noqa: E402

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


def quote(origin: str, destination: str, day: date, price: float, *, airline: str = "W6",
          transfers: int = 0, duration: int = 115) -> Quote:
    return Quote(origin=origin, destination=destination, depart_date=day, price=price, airline=airline,
                 transfers=transfers, duration_min=duration, link=None, fetched_at=NOW)


def sample_deals(routes: dict) -> list[Deal]:
    """Three deals that show the card's variations: a saving tag, no usual price, four dates."""
    return [
        Deal(routes["BGY"], [quote("TIA", "BGY", date(2026, 10, 26), 13.6), quote("TIA", "BGY", date(2026, 10, 28), 14)],
             median=21, reason="median", return_quote=quote("BGY", "TIA", date(2026, 11, 2), 14)),
        Deal(routes["SKG"], [quote("TIA", "SKG", date(2026, 11, 5), 29, airline="A3", duration=55)],
             median=None, reason="threshold"),
        Deal(routes["LHR"], [quote("TIA", "LHR", date(2026, 11, 12), 44.3, airline="BA", transfers=1, duration=250),
                             quote("TIA", "LHR", date(2026, 11, 13), 45), quote("TIA", "LHR", date(2026, 11, 19), 46),
                             quote("TIA", "LHR", date(2026, 11, 20), 47)],
             median=118, reason="median", return_quote=quote("LHR", "TIA", date(2026, 11, 26), 52)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Draw sample social media cards.")
    parser.add_argument("--out", type=Path, default=Path("ig_preview"), help="folder for the pictures")
    args = parser.parse_args()
    config = load_config()
    routes = {route.iata: route for route in config.routes}
    for deal in sample_deals(routes):
        photo = post_photo(deal.route.iata)
        paths = save_cards(deal, args.out, airline_names=config.airlines, brand=config.brand,
                           handle=config.social.handle or config.channel_handle, address=site_address(config),
                           photo=photo.path if photo else None, language=config.language)
        for path in paths.values():
            print(f"✅ {path}")
    # The weekly cover with the three deals, and Rome's guide card in both sizes.
    deals = sample_deals(routes)
    first = post_photo(deals[0].route.iata)
    cover = draw_top_card(deals, brand=config.brand, handle=config.social.handle or config.channel_handle,
                          address=site_address(config), photo=first.path if first else None, title=f"TOP {len(deals)}")
    print(f"✅ {save_jpeg(cover, args.out / 'top_feed.jpg')}")
    guide = load_guides(config.language).get("Rome")
    if guide:
        photo = post_photo("FCO")
        for layout in ("feed", "story"):
            card = draw_guide_card(layout, city=guide.display_name, iata="FCO", tagline=guide.tagline, price=17,
                                   page=f"{site_address(config)}/{guide.slug}/", photo=photo.path if photo else None,
                                   brand=config.brand, handle=config.social.handle or config.channel_handle)
            print(f"✅ {save_jpeg(card, args.out / f'guide_{guide.slug}_{layout}.jpg')}")
        # The guide's section cards, as in its carousel.
        sections = guide_sections(guide)
        for number, (key, heading, entries) in enumerate(sections, start=2):
            card = draw_list_card(heading, f"{guide.display_name.upper()}  ·  {number}/{len(sections) + 1}", entries,
                                  page=f"{site_address(config)}/{guide.slug}/", brand=config.brand,
                                  handle=config.social.handle or config.channel_handle)
            print(f"✅ {save_jpeg(card, args.out / f'guide_{guide.slug}_{number}_{key}.jpg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
