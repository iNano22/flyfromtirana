from datetime import date, timedelta

import pytest

from src.config import DealRules, Route
from src.deals import Deal, cheapest_return, deal_reason, find_route_deal, price_band, rank_deals
from tests.conftest import NOW, make_quote, seed_history


def find(route, outbound, storage, rules, inbound=()):
    storage.save_quotes(outbound)  # main.py saves this run's prices before detecting deals
    return find_route_deal(route, "TIA", outbound, list(inbound), storage, rules, NOW, "telegram")


# --- the rules themselves ----------------------------------------------------

@pytest.mark.parametrize("price, expected", [(59, "median"), (60, "median"), (61, None), (100, None)])
def test_median_rule_needs_40_percent_discount(price, expected):
    assert deal_reason(price, median=100, threshold=None, discount_pct=40) == expected


def test_threshold_rule_works_without_history():
    assert deal_reason(24, median=None, threshold=25, discount_pct=40) == "threshold"
    assert deal_reason(26, median=None, threshold=25, discount_pct=40) is None


def test_threshold_also_needs_a_real_saving():
    def reason(price, median):
        return deal_reason(price, median=median, threshold=25, discount_pct=40, threshold_min_discount_pct=25)

    assert reason(24, median=20) is None         # pricier than usual
    assert reason(14, median=18) is None         # only 22% cheaper: too weak
    assert reason(15, median=20) == "threshold"  # exactly 25% cheaper
    assert reason(24, median=36) == "threshold"  # 33% cheaper, under €25


def test_no_threshold_no_history_means_no_deal():
    assert deal_reason(5, median=None, threshold=None, discount_pct=40) is None


def test_price_band():
    assert price_band(20, 10) == 2
    assert price_band(29.99, 10) == 2
    assert price_band(30, 10) == 3


# --- find_route_deal ---------------------------------------------------------

def test_median_deal_found(storage, route, rules):
    seed_history(storage, [100] * 10)
    outbound = [
        make_quote(55, date(2026, 10, 20)),
        make_quote(58, date(2026, 10, 22)),
        make_quote(90, date(2026, 10, 25)),
    ]
    deal = find(route, outbound, storage, rules)

    assert deal is not None
    assert deal.reason == "median"
    assert deal.median == 100
    assert deal.best.price == 55
    assert [q.depart_date for q in deal.quotes] == [date(2026, 10, 20), date(2026, 10, 22)]


def test_median_ignored_until_enough_samples(storage, route, rules):
    seed_history(storage, [100, 100])  # 2 + this run's 1 = 3 samples, rules need 5
    deal = find(route, [make_quote(50)], storage, rules)
    assert deal is None  # 50 is half the median, but the median isn't trusted yet (and 50 > €25 threshold)


def test_cold_start_uses_threshold(storage, route, rules):
    deal = find(route, [make_quote(19)], storage, rules)
    assert deal.reason == "threshold"
    assert deal.median is None


def test_old_history_is_outside_median_window(storage, route, rules):
    seed_history(storage, [100] * 10, days_ago=31)
    deal = find(route, [make_quote(50)], storage, rules)
    assert deal is None


def test_extra_dates_must_be_close_to_headline_price(storage, route):
    rules = DealRules(min_samples_for_median=5, date_price_tolerance_pct=10)
    seed_history(storage, [100] * 10)
    outbound = [make_quote(50, date(2026, 10, 20)), make_quote(54, date(2026, 10, 21)),
                make_quote(56, date(2026, 10, 22))]  # 56 > 50 * 1.10
    deal = find(route, outbound, storage, rules)
    assert [q.price for q in deal.quotes] == [50, 54]


def test_max_dates_per_post(storage, route):
    rules = DealRules(min_samples_for_median=100, max_dates_per_post=2)  # threshold-only
    outbound = [make_quote(20, date(2026, 10, 20) + timedelta(days=i)) for i in range(5)]
    deal = find(route, outbound, storage, rules)
    assert len(deal.quotes) == 2


def test_return_flight_is_cheapest_in_window():
    rules = DealRules(return_min_days=2, return_max_days=14)
    outbound = make_quote(20, date(2026, 10, 20))

    def back(price, day):
        return make_quote(price, date(2026, 10, day), origin="BGY", destination="TIA")

    inbound = [back(10, 21), back(40, 23), back(30, 30), back(5, 5)]  # 21st too soon, 5th before departure
    assert cheapest_return(outbound, inbound, rules).price == 30
    assert cheapest_return(outbound, [], rules) is None


def test_deal_includes_return_flight(storage, route, rules):
    inbound = [make_quote(24, date(2026, 10, 27), origin="BGY", destination="TIA")]
    deal = find(route, [make_quote(19, date(2026, 10, 20))], storage, rules, inbound)
    assert deal.return_quote.price == 24


def test_rank_deals_best_first_and_limited():
    def deal(iata, price, median):
        r = Route(iata=iata, city=iata, city_en=iata, flag="")
        return Deal(route=r, quotes=[make_quote(price, destination=iata)], median=median, reason="median")

    # Ranked by saving vs. the median, not by raw price: DDD is cheapest but only 25% off.
    deals = [deal("AAA", 50, 100), deal("BBB", 20, 100), deal("CCC", 30, 100), deal("DDD", 15, 20)]
    ranked = rank_deals(deals, limit=3)
    assert [d.route.iata for d in ranked] == ["BBB", "CCC", "AAA"]
