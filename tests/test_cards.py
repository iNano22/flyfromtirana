"""The Instagram/Facebook cards: their sizes, the text that must fit, the stand-in for a missing photo."""
from datetime import date

from PIL import Image

from src.cards import INK, LAYOUTS, SUN, draw_card, fit, flight_line, plane_mark, save_cards
from src.deals import Deal
from src.formatter import WORDS
from src.photos import POST_PHOTOS_DIR
from tests.conftest import make_quote

AIRLINES = {"W6": "Wizz Air"}
LOOK = dict(airline_names=AIRLINES, brand="FlyFromTirana", handle="@flyfromtirana", address="flyfromtirana.al")


def make_deal(route, *, median=21.0, return_price=14.0, dates=(date(2026, 10, 26), date(2026, 10, 28))) -> Deal:
    quotes = [make_quote(13.6, day) for day in dates]
    back = make_quote(return_price, date(2026, 11, 2), origin="BGY", destination="TIA") if return_price else None
    return Deal(route, quotes, median=median, reason="median" if median else "threshold", return_quote=back)


def test_save_cards_writes_a_feed_and_a_story_jpeg(route, tmp_path):
    paths = save_cards(make_deal(route), tmp_path, photo=POST_PHOTOS_DIR / "bgy.jpg", **LOOK)

    assert set(paths) == {"feed", "story"}
    assert paths["feed"].name == "bgy_feed.jpg"
    with Image.open(paths["feed"]) as feed:
        assert (feed.format, feed.size) == ("JPEG", (1080, 1350))
    with Image.open(paths["story"]) as story:
        assert (story.format, story.size) == ("JPEG", (1080, 1920))


def test_card_bands_have_the_signage_colours(route):
    layout = LAYOUTS["feed"]
    card = draw_card(make_deal(route), "feed", photo=POST_PHOTOS_DIR / "bgy.jpg", **LOOK)

    assert card.getpixel((layout.width // 2, 8)) == INK                        # the brand bar
    assert card.getpixel((layout.width // 2, layout.sign_top + 8)) == SUN      # the yellow sign
    assert card.getpixel((layout.width // 2, layout.height - 8)) == INK        # the footer


def test_route_without_a_photo_gets_a_dark_board(route):
    layout = LAYOUTS["story"]
    card = draw_card(make_deal(route), "story", photo=None, **LOOK)

    assert card.size == (1080, 1920)
    assert card.getpixel((20, layout.bar + 20)) == (42, 46, 53)


def test_deal_without_usual_price_or_return_flight_still_draws(route):
    card = draw_card(make_deal(route, median=None, return_price=None, dates=(date(2026, 11, 5),)), "feed",
                     photo=None, **LOOK)
    assert card.size == (1080, 1350)


def test_long_city_name_is_shrunk_until_it_fits():
    width = 1080 - 2 * LAYOUTS["feed"].padding
    assert fit("MILAN", width, size=210, weight=800, width_axis=72).size == 210
    long_name = fit("THESSALONIKI", width, size=210, weight=800, width_axis=72)
    assert long_name.size < 210
    assert long_name.getlength("THESSALONIKI") <= width


def test_flight_line_reads_like_the_post(route):
    deal = make_deal(route)
    assert flight_line(deal, AIRLINES, WORDS["sq"]) == "Wizz Air  ·  direkt  ·  1 orë 35 min  ·  kthimi nga €14"
    # An airline the config doesn't name keeps its code; no duration and no return flight: no gaps left.
    deal = make_deal(route, return_price=None)
    deal.quotes[0] = make_quote(13.6, date(2026, 10, 26), airline="XY", transfers=1, duration_min=None)
    assert flight_line(deal, AIRLINES, WORDS["sq"]) == "XY  ·  me ndalesë"


def test_plane_mark_is_a_black_plane_on_yellow():
    mark = plane_mark(64)
    assert mark.size == (64, 64)
    assert mark.getpixel((32, 32))[:3] == INK     # the plane's body crosses the middle
    assert mark.getpixel((5, 32))[:3] == SUN      # the square's edge, beside the plane
    assert mark.getpixel((0, 0))[3] == 0          # the corner is rounded off (transparent)


def test_guide_card_and_top_card_have_the_right_sizes(route):
    from src.cards import draw_guide_card, draw_top_card
    guide = draw_guide_card("feed", city="Rome", iata="FCO", tagline="Koloseu, Vatikani dhe pasta", price=17,
                            page="flyfromtirana.al/rome/", photo=None, brand="FlyFromTirana", handle="@flyfromtirana")
    assert guide.size == (1080, 1350)
    story = draw_guide_card("story", city="Thessaloniki", iata="SKG", tagline="Deti, ushqimi dhe jeta e natës " * 3,
                            price=None, page="flyfromtirana.al/thessaloniki/", photo=None, brand="FlyFromTirana",
                            handle="@flyfromtirana")
    assert story.size == (1080, 1920)
    deals = [make_deal(route) for _ in range(5)]
    cover = draw_top_card(deals, brand="FlyFromTirana", handle="@flyfromtirana", address="flyfromtirana.al")
    assert cover.size == (1080, 1350)
    assert cover.getpixel((540, 8)) == INK and cover.getpixel((20, 300)) == SUN


def test_list_card_leaves_off_what_does_not_fit():
    from src.cards import draw_list_card
    long = ("Një fjali e gjatë që përsëritet shumë herë për ta provuar. " * 6).strip()
    card = draw_list_card("Çfarë të shohësh", "ROME  ·  2/7", [(f"Vendi {n}", long) for n in range(1, 9)],
                          page="flyfromtirana.al/rome/", brand="FlyFromTirana", handle="@flyfromtirana")
    assert card.size == (1080, 1350)
    assert card.getpixel((540, 8)) == INK and card.getpixel((20, 300)) == SUN
    # Tips have no name: still drawn.
    tips = draw_list_card("Këshilla praktike", "ROME  ·  7/7", [(None, "Mbaj një shishe uji."), ("Çmimet", "€1.20")],
                          page="flyfromtirana.al/rome/", brand="FlyFromTirana", handle="@flyfromtirana")
    assert tips.size == (1080, 1350)


def test_intro_posts_file_draws_nine_cards(tmp_path):
    """content/intro_posts.yaml: nine posts, each drawn as a feed card with its captions filled in."""
    import sys
    sys.path.insert(0, "scripts")
    from intro_posts import captions, draw, load_posts
    from src.config import load_config
    config = load_config()
    posts = load_posts()
    assert len(posts) == 9
    for number, post in enumerate(posts, start=1):
        path = draw(post, number, 9, config, tmp_path)
        with Image.open(path) as card:
            assert card.size == (1080, 1350)
        instagram, facebook = captions(post, config)
        assert "{" not in instagram and "{" not in facebook
        assert instagram.endswith(config.social.hashtags) and facebook.endswith(config.social.hashtags)
        assert "https://" not in instagram   # Instagram captions carry no links
    assert "linku në bio" in captions(posts[0], config)[0]
    assert "https://flyfromtirana.al/" in captions(posts[0], config)[1]
    assert "https://t.me/flyfromtirana" in captions(posts[3], config)[1]
