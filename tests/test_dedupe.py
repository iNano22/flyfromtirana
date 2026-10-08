"""Don't repost the same route + date + price band within the cooldown."""
from datetime import date, timedelta

from src.deals import find_route_deal, price_band
from tests.conftest import NOW, make_quote


def posted(storage, price, *, days_ago, depart=date(2026, 10, 20), channel="telegram"):
    quote = make_quote(price, depart)
    storage.record_post(channel, quote, price_band(price, 10), posted_at=NOW - timedelta(days=days_ago))


def find(route, outbound, storage, rules):
    return find_route_deal(route, "TIA", outbound, [], storage, rules, NOW, "telegram")


def test_same_band_within_cooldown_is_skipped(storage, route, rules):
    posted(storage, 19, days_ago=2)
    assert find(route, [make_quote(18)], storage, rules) is None  # 18 and 19 are both in the €10-19 band


def test_reposted_after_cooldown(storage, route, rules):
    posted(storage, 19, days_ago=8)  # cooldown is 7 days
    assert find(route, [make_quote(19)], storage, rules) is not None


def test_cheaper_band_is_news(storage, route, rules):
    posted(storage, 24, days_ago=1)  # band 2
    deal = find(route, [make_quote(15)], storage, rules)  # band 1
    assert deal is not None and deal.best.price == 15


def test_pricier_band_is_not_reposted(storage, route, rules):
    posted(storage, 15, days_ago=1)  # band 1
    assert find(route, [make_quote(24)], storage, rules) is None  # band 2: got more expensive


def test_other_dates_on_same_route_still_post(storage, route, rules):
    posted(storage, 19, days_ago=1, depart=date(2026, 10, 20))
    deal = find(route, [make_quote(19, date(2026, 10, 20)), make_quote(20, date(2026, 10, 22))], storage, rules)
    assert [q.depart_date for q in deal.quotes] == [date(2026, 10, 22)]


def test_other_channel_does_not_block(storage, route, rules):
    posted(storage, 19, days_ago=1, channel="instagram")
    assert find(route, [make_quote(19)], storage, rules) is not None
