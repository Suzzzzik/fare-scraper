"""Offline unit tests for the pure logic in this project.

Nothing here touches the network. Everything with a socket - the scrapers,
the FX fetch, the browser drivers - is exercised through the CLIs and the web
UI instead; these tests pin down the pieces that decide what a user actually
sees: pairing, deduplication, currency handling, filters, parsing.
"""

from __future__ import annotations

from datetime import date

import pytest

import fx
import ryanair
import server
import stays
import wizzair

# ---------- fx ----------


@pytest.fixture
def rates(monkeypatch):
    """A fixed EUR-based rate table, so tests never depend on frankfurter.app."""
    monkeypatch.setattr(fx.RATES, "_rates", {"EUR": 1.0, "PLN": 4.3, "USD": 1.1})
    monkeypatch.setattr(fx.RATES, "_day", date.today())
    return fx.RATES


def test_convert_same_currency_is_identity(rates):
    assert fx.convert(179.0, "PLN", "PLN") == 179.0


def test_convert_goes_through_eur(rates):
    assert fx.convert(29.99, "EUR", "PLN") == pytest.approx(29.99 * 4.3)
    assert fx.convert(43.0, "PLN", "USD") == pytest.approx(43.0 / 4.3 * 1.1)


def test_convert_unknown_currency_refuses(rates):
    assert fx.convert(10.0, "XXX", "PLN") is None
    assert fx.convert(10.0, "", "PLN") is None


def test_total_mixes_currencies_correctly(rates):
    # the bug that motivated fx.py: 179 PLN + 29.99 EUR is NOT 208.99 PLN
    got = fx.total([(179.0, "PLN"), (29.99, "EUR")], "PLN")
    assert got == pytest.approx(179.0 + 29.99 * 4.3)


def test_total_refuses_if_any_leg_unconvertible(rates):
    assert fx.total([(179.0, "PLN"), (10.0, "XXX")], "PLN") is None


# ---------- date helpers ----------


def test_chunk_ranges_respects_max_window():
    chunks = list(wizzair.chunk_ranges(date(2026, 9, 1), date(2026, 11, 30), size=42))
    assert chunks[0] == (date(2026, 9, 1), date(2026, 10, 12))
    assert all((b - a).days + 1 <= 42 for a, b in chunks)
    assert chunks[-1][1] == date(2026, 11, 30)
    # contiguous, no gaps and no overlap
    for (_, b1), (a2, _) in zip(chunks, chunks[1:], strict=False):
        assert (a2 - b1).days == 1


def test_months_between_yields_first_of_each_month():
    got = list(ryanair.months_between(date(2026, 9, 15), date(2026, 11, 3)))
    assert got == [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]


# ---------- weekday filter ----------


def test_expand_weekdays_wraps_around_the_week():
    # Monday +/-1 must include Sunday: leaving Sunday evening or Monday morning
    assert server.expand_weekdays([0], 1) == {6, 0, 1}


def test_expand_weekdays_no_tolerance_and_empty():
    assert server.expand_weekdays([4], 0) == {4}
    assert server.expand_weekdays([], 2) == set()


# ---------- return pairing ----------


def _leg(day: date, price: float) -> dict:
    return {"day": day, "price": price}


def test_best_return_picks_cheapest_inside_stay_window():
    out = [_leg(date(2026, 9, 1), 100.0)]
    back = [
        _leg(date(2026, 9, 4), 50.0),   # 3 nights: outside 5..9
        _leg(date(2026, 9, 6), 80.0),   # 5 nights: in window
        _leg(date(2026, 9, 8), 60.0),   # 7 nights: in window, cheaper
        _leg(date(2026, 9, 12), 10.0),  # 11 nights: outside
    ]
    pairs = server.best_return_for_each_outbound(out, back, nights=7, tol=2)
    assert len(pairs) == 1
    assert pairs[0][1]["day"] == date(2026, 9, 8)


def test_best_return_drops_outbound_with_no_valid_return():
    out = [_leg(date(2026, 9, 1), 100.0)]
    back = [_leg(date(2026, 9, 2), 10.0)]
    assert server.best_return_for_each_outbound(out, back, nights=7, tol=0) == []


# ---------- dedupe ----------


def _row(**kw) -> dict:
    base = dict(carrier="wizzair", airline="Wizz Air", airline_back="",
                origin="WAW", dest="BCN", dest_back="", price=100.0)
    base.update(kw)
    return base


def test_cheapest_per_route_keeps_lowest_price():
    rows = [_row(price=120.0), _row(price=90.0), _row(price=150.0)]
    kept = server._cheapest_per_route(rows)
    assert len(kept) == 1 and kept[0]["price"] == 90.0


def test_cheapest_per_route_treats_return_airport_as_part_of_route():
    # open-jaw: back into POZ and back into GDN are different trips
    rows = [_row(dest_back="POZ", price=100.0), _row(dest_back="GDN", price=110.0)]
    assert len(server._cheapest_per_route(rows)) == 2


def test_cheapest_per_route_separates_mixed_airline_combos():
    rows = [_row(carrier="combo", airline="Wizz Air", airline_back="Ryanair", price=100.0),
            _row(carrier="combo", airline="Wizz Air", airline_back="Wizz Air", price=100.0)]
    assert len(server._cheapest_per_route(rows)) == 2


# ---------- query parsing ----------


def _qs(**kw) -> dict:
    base = {"date_from": "2026-09-01", "date_to": "2026-09-10"}
    base.update(kw)
    return {k: [v] for k, v in base.items()}


def test_parse_params_defaults():
    p = server.parse_params(_qs(from_="pl") | {"from": ["pl"], "to": ["es"]})
    assert p["trip"] == "oneway"
    assert p["nights"] == 7 and p["nights_tol"] == 2
    assert p["max_ratio"] is None
    assert p["weekdays"] == set()
    assert p["combos"] is False


def test_parse_params_upper_cases_airports_and_expands_weekdays():
    p = server.parse_params(_qs(origins="gdn, krk", dests="mad", weekdays="0", weekday_tol="1"))
    assert p["origins"] == ["GDN", "KRK"]
    assert p["dests"] == ["MAD"]
    assert p["weekdays"] == {6, 0, 1}


def test_parse_params_return_dests_imply_open_jaw():
    p = server.parse_params(_qs(return_dests="POZ"))
    assert p["open_jaw"] is True and p["return_dests"] == ["POZ"]


def test_parse_params_unknown_carrier_is_dropped():
    p = server.parse_params(_qs(carriers="ryanair,nosuch"))
    assert p["carriers"] == ["ryanair"]


# ---------- row shape ----------


def test_row_rounds_prices_and_defaults_optional_fields():
    r = server._row("ryanair", "WAW", "BCN", "2026-09-01", 123.456, "PLN", "https://x")
    assert r["price"] == 123.46
    assert r["price_out"] is None and r["price_back"] is None
    assert r["dest_back"] == "" and r["link_back"] == ""
    assert r["nights"] is None


# ---------- stays: parsing and geometry ----------


@pytest.mark.parametrize("text, expected", [
    # the formats Booking and Airbnb actually emit: comma = thousands separator
    ("zł 8,676 total", 8676.0),
    ("1 234 zł", 1234.0),
    ("PLN 409", 409.0),
    ("409.50 PLN", 409.5),
    ("", None),
])
def test_parse_amount(text, expected):
    assert stays.parse_amount(text) == expected


def test_haversine_known_distance():
    # Warsaw Chopin to Krakow Balice is ~250 km
    assert stays.haversine_km(52.1657, 20.9671, 50.0777, 19.7848) == pytest.approx(250, abs=5)


def test_bounding_box_contains_radius():
    box = stays.bounding_box(41.297, 2.078, 20)   # BCN
    assert box["sw_lat"] < 41.297 < box["ne_lat"]
    assert box["sw_lng"] < 2.078 < box["ne_lng"]
    # a point 20 km due north must be inside the box's latitude span
    north = 41.297 + 20 / 111.0
    assert north <= box["ne_lat"] + 1e-9


def test_price_bands_cover_range_and_tighten_at_cheap_end():
    bands = stays.price_bands(100, 1600, 4)
    assert bands[0][0] == 100 and bands[-1][1] == 1600
    widths = [hi - lo for lo, hi in bands]
    assert widths == sorted(widths)   # each band wider than the last
    for (_, hi1), (lo2, _) in zip(bands, bands[1:], strict=False):
        assert hi1 == lo2


def test_supported_sources_skips_a_source_that_cannot_express_a_filter():
    # "breakfast" is Booking-only, "pool" both
    got = stays.supported_sources(["pool", "breakfast"])
    assert got["ok"] == ["booking"]
    assert "airbnb" in got["dropped"]
    assert stays.supported_sources([])["ok"] == list(stays.SOURCES)
