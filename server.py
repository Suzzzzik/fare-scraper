#!/usr/bin/env python3
"""Local web UI for the Ryanair + Wizz Air fare scrapers.

Nothing is hardcoded to Poland or Spain: country lists, airport lists and the
route graphs all come from the carriers' own reference endpoints at runtime, so
any origin country / destination country pair the carriers serve can be picked
in the browser.

  python server.py            # http://127.0.0.1:8000
  python server.py --port 9000 --market pl-pl

Endpoints:
  GET /                        the UI
  GET /api/countries           merged country list (both carriers)
  GET /api/airports?country=pl airports in a country, with carrier flags
  GET /api/search?...          Server-Sent Events: progress + fare rows
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import queue
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import china_airlines as ca
import fx
import kiwi as kw
import lot as lo
import lufthansa as lh
import ryanair as ry
import stays as st
import wizzair as wz

STATIC = Path(__file__).parent / "static"

# Registry - adding an airline source means adding an entry here plus a
# <name>_rows() method on SearchRun.
CARRIERS = {
    "ryanair": {"label": "Ryanair", "direct": True},
    "wizzair": {"label": "Wizz Air", "direct": True},
    "lot": {"label": "LOT", "direct": True},
    "lufthansa": {"label": "Lufthansa", "direct": True},
    "chinaairlines": {"label": "China Airlines", "direct": True},
    "easyjet": {"label": "easyJet", "direct": False},
    "kiwi": {"label": "Pozostałe linie (Kiwi.com)", "direct": False},
}

# easyJet's own fare endpoint sits behind an Akamai challenge that neither
# curl_cffi nor Playwright-driven Chrome gets past, so its rows come from Kiwi
# filtered down to the easyJet group.
#
# Lufthansa and China Airlines looked like the same story at first (both
# behind bot-protection that even a genuine Playwright Chrome tab fails
# automatically), but `patchright` (a patched Playwright build that hides
# the CDP tells) gets a real Chrome tab through both - see lufthansa.py and
# china_airlines.py for the full story, including why the obvious "mint
# once, replay via curl_cffi" shortcut doesn't work (both bind their
# challenge clearance to the TLS/HTTP2 fingerprint of the browser
# connection, not just a cookie, so curl_cffi always gets re-blocked).
# China Airlines stacks three bot-protection vendors (Akamai + Imperva
# Incapsula + DataDome) versus Lufthansa's single Cloudflare challenge, and
# is noticeably flakier as a result - GROUP_CODES keeps it in the Kiwi
# exclusion list either way, so a flaky/failed direct search still doesn't
# duplicate rows that came through Kiwi if the user has both selected.
EASYJET_CODES = ["U2", "EC", "DS"]   # easyJet UK / Europe / Switzerland
LUFTHANSA_CODES = ["LH"]
CHINA_AIRLINES_CODES = ["CI"]

# Kiwi indexes Ryanair and Wizz too. When those are searched directly, their
# codes are excluded from the Kiwi query so it only contributes airlines the
# other two cannot reach.
GROUP_CODES = {
    "ryanair": ["FR", "RK", "RR", "OE", "AL"],   # Ryanair, UK, Buzz, Lauda, Malta Air
    "wizzair": ["W6", "W9", "W4", "W8", "5W"],   # Wizz HU / UK / Malta / Abu Dhabi
    "lot": ["LO"],
    "easyjet": EASYJET_CODES,
    "lufthansa": LUFTHANSA_CODES,
    "chinaairlines": CHINA_AIRLINES_CODES,
}


# ---------------------------------------------------------------- clients

class Clients:
    """Lazily built API clients plus cached reference data."""

    def __init__(self, market: str, currency: str = "PLN"):
        self.market = market
        self.currency = currency
        self._lock = threading.Lock()
        self._ry: ry.Ryanair | None = None
        self._wz: wz.WizzAir | None = None
        self._lo: lo.Lot | None = None
        self._lh: lh.Lufthansa | None = None
        self._ca: ca.ChinaAirlines | None = None
        self._kw: kw.Kiwi | None = None
        self.stays = st.Stays(currency=currency)
        self._countries: list[dict] | None = None
        self._names: dict[str, str] | None = None
        self._airports: dict[str, list[dict]] = {}

    @property
    def ryanair(self) -> ry.Ryanair:
        with self._lock:
            if self._ry is None:
                self._ry = ry.Ryanair(market=self.market, lang="en")
            return self._ry

    @property
    def wizzair(self) -> wz.WizzAir:
        with self._lock:
            if self._wz is None:
                self._wz = wz.WizzAir(market=self.market)
            return self._wz

    @property
    def lot(self) -> lo.Lot:
        with self._lock:
            if self._lo is None:
                self._lo = lo.Lot(market=self.market)
            return self._lo

    @property
    def lufthansa(self) -> lh.Lufthansa:
        with self._lock:
            if self._lh is None:
                self._lh = lh.Lufthansa(market=self.market, currency=self.currency)
            return self._lh

    @property
    def chinaairlines(self) -> ca.ChinaAirlines:
        with self._lock:
            if self._ca is None:
                self._ca = ca.ChinaAirlines(currency="USD")
            return self._ca

    @property
    def kiwi(self) -> kw.Kiwi:
        with self._lock:
            if self._kw is None:
                self._kw = kw.Kiwi(market=self.market, currency=self.currency)
            return self._kw

    # -- reference data ---------------------------------------------------

    def countries(self) -> list[dict]:
        if self._countries is not None:
            return self._countries

        merged: dict[str, dict] = {}
        try:
            for a in self.ryanair.airports():
                c = a["country"]
                e = merged.setdefault(c["code"].lower(),
                                      {"code": c["code"].lower(),
                                       "name": c["name"], "carriers": []})
                if "ryanair" not in e["carriers"]:
                    e["carriers"].append("ryanair")
        except Exception:  # noqa: BLE001 - one carrier down must not blank the UI
            traceback.print_exc()
        try:
            for c in self.wizzair.cities():
                e = merged.setdefault(c["countryCode"].lower(),
                                      {"code": c["countryCode"].lower(),
                                       "name": c["countryName"],
                                       "carriers": []})
                if "wizzair" not in e["carriers"]:
                    e["carriers"].append("wizzair")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        try:
            # Kiwi knows ~223 countries, far more than the two low-cost carriers
            for c in self.kiwi.countries():
                e = merged.setdefault(c["code"],
                                      {"code": c["code"], "name": c["name"],
                                       "carriers": []})
                e["carriers"].append("kiwi")
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        self._countries = sorted(merged.values(), key=lambda x: x["name"])
        return self._countries

    def names(self) -> dict[str, str]:
        """IATA -> human name, so every row can show a city even when the fare
        endpoint that produced it only returns codes."""
        if self._names is not None:
            return self._names
        out: dict[str, str] = {}
        try:
            for c in self.wizzair.cities():
                out[c["iata"]] = c["shortName"]
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        try:
            for a in self.ryanair.airports():
                out[a["code"]] = a["name"]  # Ryanair names win where both exist
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        self._names = out
        return out

    def airports(self, country: str) -> list[dict]:
        # three upstream calls per country, and several carriers plus the combo
        # search all ask for the same two countries in one run - cached per
        # process, since a country's airport list does not change mid-search
        with self._lock:
            hit = self._airports.get(country)
        if hit is not None:
            return hit

        merged: dict[str, dict] = {}
        try:
            for a in self.ryanair.airports(country):
                merged.setdefault(a["code"], {"code": a["code"],
                                              "name": a["name"],
                                              "carriers": []})["carriers"].append("ryanair")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        try:
            for c in self.wizzair.cities(country):
                e = merged.setdefault(c["iata"], {"code": c["iata"],
                                                  "name": c["shortName"],
                                                  "carriers": []})
                e["carriers"].append("wizzair")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        try:
            for a in self.kiwi.airports(country):
                e = merged.setdefault(a["code"], {"code": a["code"],
                                                  "name": a["name"],
                                                  "carriers": []})
                e["carriers"].append("kiwi")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        out = sorted(merged.values(), key=lambda x: x["code"])
        with self._lock:
            self._airports[country] = out
        return out


# ---------------------------------------------------------------- search

def _row(carrier, origin, dest, day, price, currency, link, *,
         origin_name="", dest_name="", times="", flight_number="",
         date_back="", times_back="", flight_number_back="",
         price_out=None, price_back=None, nights=None,
         airline="", airline_back="", stops=0,
         dest_back="", dest_back_name="", link_back="") -> dict:
    """One result row. `price` is always what the trip costs in total, so
    one-way and return rows sort against each other correctly.

    `carrier` is where the row came from, `airline` is who actually flies it -
    they differ for Kiwi rows, which can be any airline.

    `dest_back` is where the return leg lands when that is NOT the airport the
    trip started from (an open-jaw trip, e.g. out of Gdansk but back into
    Poznan); empty means the return lands back at `origin`. `link_back` is a
    second booking link, set only for combo rows where the two legs are
    separate tickets and so have to be booked separately.
    """
    return {
        "carrier": carrier,
        "airline": airline,
        "airline_back": airline_back,
        "stops": stops,
        "origin": origin,
        "origin_name": origin_name,
        "dest": dest,
        "dest_name": dest_name,
        "dest_back": dest_back,
        "dest_back_name": dest_back_name,
        "date": day,
        "times": times,
        "flight_number": flight_number,
        "date_back": date_back,
        "times_back": times_back,
        "flight_number_back": flight_number_back,
        "nights": nights,
        "price_out": None if price_out is None else round(float(price_out), 2),
        "price_back": None if price_back is None else round(float(price_back), 2),
        "price": round(float(price), 2),
        "currency": currency,
        "link": link,
        "link_back": link_back,
    }


def best_return_for_each_outbound(out_legs: list[dict], back_legs: list[dict],
                                  nights: int, tol: int) -> list[tuple[dict, dict]]:
    """Pair every outbound day with its cheapest return inside the stay window.

    A stay of `nights` +/- `tol` means the return leg departs between
    nights-tol and nights+tol days after the outbound one. Emitting one row per
    outbound day (rather than every valid combination) keeps the result set
    readable while still covering the whole window.
    """
    lo, hi = max(0, nights - tol), nights + tol
    by_day: list[tuple[dict, dict]] = []
    for o in out_legs:
        best = None
        for b in back_legs:
            gap = (b["day"] - o["day"]).days
            if lo <= gap <= hi and (best is None or b["price"] < best["price"]):
                best = b
        if best is not None:
            by_day.append((o, best))
    return by_day


def expand_weekdays(picked: list[int], tol: int) -> set[int]:
    """Weekdays acceptable given the picked ones and a +/- day tolerance.

    The week wraps, so Monday (0) with a tolerance of 1 yields Sunday (6),
    Monday and Tuesday - which is the point: someone taking five days off
    around a weekend wants to leave Sunday evening *or* Monday morning,
    whichever is cheaper.
    """
    if not picked:
        return set()
    out: set[int] = set()
    for w in picked:
        for d in range(-tol, tol + 1):
            out.add((w + d) % 7)
    return out


def _cheapest_per_route(rows: list[dict]) -> list[dict]:
    best: dict[tuple, dict] = {}
    for r in rows:
        # the return airport is part of the route for open-jaw combos: BCN-POZ
        # back is a different trip from BCN-GDN back, even on the same outbound
        k = (r["carrier"], r.get("airline", ""), r.get("airline_back", ""),
             r["origin"], r["dest"], r.get("dest_back", ""))
        if k not in best or r["price"] < best[k]["price"]:
            best[k] = r
    return list(best.values())


class SearchRun:
    """One search across the selected carriers, reporting progress as it goes."""

    def __init__(self, clients: Clients, params: dict, emit):
        self.c = clients
        self.p = params
        self.emit = emit
        self._done = 0
        self._total = 0
        self._lock = threading.Lock()

    def _tick(self, n: int = 1) -> None:
        with self._lock:
            self._done += n
            self.emit("progress", {"done": self._done, "total": self._total})

    def _add_total(self, n: int) -> None:
        with self._lock:
            self._total += n
            self.emit("progress", {"done": self._done, "total": self._total})

    # -- shared -----------------------------------------------------------

    @property
    def display_currency(self) -> str:
        """The one currency every row is reported in. Carriers price in the
        departure market's currency, so without this a table mixes PLN and
        EUR rows - which breaks sorting, the cheapest-per-route dedupe and the
        relative price cap, all of which compare prices across rows."""
        return self.c.currency

    @property
    def return_window(self) -> tuple[date, date]:
        """Date range the inbound leg may depart in, derived from the outbound
        window plus the requested stay length and its tolerance."""
        p = self.p
        lo = max(0, p["nights"] - p["nights_tol"])
        hi = p["nights"] + p["nights_tol"]
        return p["date_from"] + timedelta(days=lo), p["date_to"] + timedelta(days=hi)

    # -- Ryanair ----------------------------------------------------------

    def ryanair_rows(self) -> list[dict]:
        api = self.c.ryanair
        p = self.p
        origins = p["origins"] or [a["code"] for a in api.airports(p["from"])]
        rows: list[dict] = []

        if p["trip"] == "return":
            return self.ryanair_return_rows(origins)

        if p["mode"] == "calendar":
            pairs = []
            wanted_dests = set(p["dests"])
            for o in origins:
                try:
                    pairs += [(o, d) for d in api.destinations_in(o, p["to"])
                              if not wanted_dests or d in wanted_dests]
                except Exception as e:  # noqa: BLE001
                    self.emit("warn", {"carrier": "ryanair",
                                       "message": f"routes {o}: {e}"})
            jobs = [(o, d, m) for o, d in pairs
                    for m in ry.months_between(p["date_from"], p["date_to"])]
            self._add_total(len(jobs))

            def work(job):
                origin, dest, month = job
                out = []
                for f in api.cheapest_per_day(origin, dest, month):
                    if f.get("unavailable") or f.get("soldOut") or not f.get("price"):
                        continue
                    day = date.fromisoformat(f["day"])
                    if not (p["date_from"] <= day <= p["date_to"]):
                        continue
                    price = f["price"]["value"]
                    if p["max_price"] is not None and price > p["max_price"]:
                        continue
                    dep = f.get("departureDate") or ""
                    out.append(_row("ryanair", origin, dest, f["day"], price,
                                    f["price"]["currencyCode"],
                                    ry.booking_link(origin, dest, f["day"],
                                                    adults=p["adults"],
                                                    market=self.c.market),
                                    times=dep[11:16], airline="Ryanair"))
                return out
            targets = jobs
        else:
            self._add_total(len(origins))

            def work(origin):
                out = []
                for f in api.one_way_fares(origin, p["to"], p["date_from"].isoformat(),
                                           p["date_to"].isoformat(), p["adults"],
                                           p["max_price"]):
                    o = f["outbound"]
                    day = o["departureDate"][:10]
                    out.append(_row(
                        "ryanair",
                        o["departureAirport"]["iataCode"],
                        o["arrivalAirport"]["iataCode"],
                        day, o["price"]["value"], o["price"]["currencyCode"],
                        ry.booking_link(o["departureAirport"]["iataCode"],
                                        o["arrivalAirport"]["iataCode"], day,
                                        adults=p["adults"],
                                        market=self.c.market),
                        origin_name=o["departureAirport"]["name"],
                        dest_name=o["arrivalAirport"]["name"],
                        times=o["departureDate"][11:16],
                        flight_number=o.get("flightNumber") or "",
                        airline="Ryanair"))
                return out
            targets = origins

        with ThreadPoolExecutor(max_workers=6) as pool:
            for res in pool.map(self._guard(work, "ryanair"), targets):
                rows += res
        return rows

    def ryanair_return_rows(self, origins: list[str]) -> list[dict]:
        """Ryanair prices return trips natively, so the stay window maps
        straight onto durationFrom/durationTo."""
        api = self.c.ryanair
        p = self.p
        in_from, in_to = self.return_window
        lo = max(0, p["nights"] - p["nights_tol"])
        hi = p["nights"] + p["nights_tol"]
        self._add_total(len(origins))

        def work(origin):
            out = []
            for f in api.round_trip_fares(
                    origin, p["to"], p["date_from"].isoformat(),
                    p["date_to"].isoformat(), in_from.isoformat(),
                    in_to.isoformat(), lo, hi, p["adults"], p["max_price"]):
                o, i = f["outbound"], (f.get("inbound") or {})
                day = o["departureDate"][:10]
                back_day = (i.get("departureDate") or "")[:10]
                total = ((f.get("summary") or {}).get("price") or {}).get("value")
                if total is None:
                    total = (o["price"]["value"]
                             + (i.get("price") or {}).get("value", 0.0))
                out.append(_row(
                    "ryanair",
                    o["departureAirport"]["iataCode"],
                    o["arrivalAirport"]["iataCode"],
                    day, total, o["price"]["currencyCode"],
                    ry.booking_link(o["departureAirport"]["iataCode"],
                                    o["arrivalAirport"]["iataCode"], day,
                                    back_day or None, adults=p["adults"],
                                    market=self.c.market),
                    origin_name=o["departureAirport"]["name"],
                    dest_name=o["arrivalAirport"]["name"],
                    times=o["departureDate"][11:16],
                    flight_number=o.get("flightNumber") or "",
                    date_back=back_day,
                    times_back=(i.get("departureDate") or "")[11:16],
                    flight_number_back=i.get("flightNumber") or "",
                    price_out=o["price"]["value"],
                    price_back=(i.get("price") or {}).get("value"),
                    nights=((date.fromisoformat(back_day) - date.fromisoformat(day)).days
                            if back_day else None),
                    airline="Ryanair", airline_back="Ryanair",
                ))
            return out

        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for res in pool.map(self._guard(work, "ryanair"), origins):
                rows += res
        return rows

    # -- Wizz Air ---------------------------------------------------------

    def wizzair_rows(self) -> list[dict]:
        api = self.c.wizzair
        p = self.p
        is_return = p["trip"] == "return"
        pairs = api.routes_between(p["from"], p["to"], p["origins"] or None,
                                   p["dests"] or None)

        # the return leg window is the outbound chunk stretched by the stay
        # length, and the API caps any single window at 42 days - so shrink the
        # outbound chunk by the tolerance spread to keep the inbound one legal
        chunk = wz.MAX_RANGE_DAYS
        if is_return:
            chunk = max(7, wz.MAX_RANGE_DAYS - 2 * p["nights_tol"])
        jobs = [(o, d, a, b) for o, d in pairs
                for a, b in wz.chunk_ranges(p["date_from"], p["date_to"], chunk)]
        self._add_total(len(jobs))

        lo = max(0, p["nights"] - p["nights_tol"])
        hi = p["nights"] + p["nights_tol"]

        def legs(data, key, origin, dest, d_from, d_to):
            """Timetable entries as plain dicts, price filter deferred to the
            total so a pricey outbound can still pair into a cheap trip."""
            out = []
            for f in wz.fares_from_timetable(data, key, origin, dest,
                                             d_from, d_to, None,
                                             p["adults"], api.market):
                out.append({"day": date.fromisoformat(f.departure),
                            "price": f.price, "currency": f.currency,
                            "times": f.departure_times,
                            "origin": f.origin, "dest": f.dest,
                            "link": f.link})
            return out

        def work(job):
            origin, dest, a, b = job
            if not is_return:
                data = api.timetable(origin, dest, a, b, p["adults"])
                fares = wz.fares_from_timetable(
                    data, "outboundFlights", origin, dest,
                    p["date_from"], p["date_to"], p["max_price"],
                    p["adults"], api.market)
                return [_row("wizzair", f.origin, f.dest, f.departure, f.price,
                             f.currency, f.link, times=f.departure_times,
                             airline="Wizz Air")
                        for f in fares]

            back_from, back_to = a + timedelta(days=lo), b + timedelta(days=hi)
            data = api.timetable(origin, dest, a, b, p["adults"],
                                 round_trip=True, return_from=back_from,
                                 return_to=back_to)
            out_legs = legs(data, "outboundFlights", origin, dest, a, b)
            back_legs = legs(data, "returnFlights", dest, origin,
                             back_from, back_to)

            rows = []
            for o, i in best_return_for_each_outbound(out_legs, back_legs,
                                                      p["nights"],
                                                      p["nights_tol"]):
                # Wizz prices each leg in its departure market's currency, so a
                # PL-out/ES-back pairing mixes PLN and EUR - adding the raw
                # numbers reported a 179 PLN + 29.99 EUR trip as "208.99 PLN"
                cur = self.display_currency
                total = fx.total([(o["price"], o["currency"]),
                                  (i["price"], i["currency"])], cur)
                if total is None:
                    continue   # unconvertible: better no row than a wrong one
                price_out = fx.convert(o["price"], o["currency"], cur)
                price_back = fx.convert(i["price"], i["currency"], cur)
                if p["max_price"] is not None and total > p["max_price"]:
                    continue
                rows.append(_row(
                    "wizzair", o["origin"], o["dest"], o["day"].isoformat(),
                    total, cur,
                    wz.booking_link(o["origin"], o["dest"],
                                    o["day"].isoformat(), i["day"].isoformat(),
                                    adults=p["adults"], market=api.market),
                    times=o["times"],
                    date_back=i["day"].isoformat(),
                    times_back=i["times"],
                    price_out=price_out, price_back=price_back,
                    nights=(i["day"] - o["day"]).days,
                    airline="Wizz Air", airline_back="Wizz Air",
                ))
            return rows

        rows: list[dict] = []
        # measured: 12 timetable calls four-at-a-time finish in 1.5s with zero
        # errors, versus ~18s when serialised behind a pre-emptive gap. The
        # client backs off on an actual 429/503 rather than assuming one.
        with ThreadPoolExecutor(max_workers=6) as pool:
            for res in pool.map(self._guard(work, "wizzair"), jobs):
                rows += res
        return rows

    # -- LOT --------------------------------------------------------------

    def lot_rows(self) -> list[dict]:
        api = self.c.lot
        p = self.p
        # LOT's route feed carries no country field, so the country pair is
        # resolved through the merged airport lists the other sources provide
        origin_codes = ({o.upper() for o in p["origins"]} if p["origins"]
                        else {a["code"] for a in self.c.airports(p["from"])})
        dest_codes = ({d.upper() for d in p["dests"]} if p["dests"]
                      else {a["code"] for a in self.c.airports(p["to"])})
        pairs = api.routes_between(origin_codes, dest_codes)
        self._add_total(len(pairs))
        if not pairs:
            return []

        def work(pair):
            origin, dest = pair
            if p["trip"] == "return":
                fares = api.round_trip(origin, dest, p["date_from"], p["date_to"],
                                       p["nights"], p["nights_tol"],
                                       p["max_price"])
            else:
                fares = api.one_way(origin, dest, p["date_from"], p["date_to"],
                                    p["max_price"])
            return [_row(
                "lot", f.origin, f.dest, f.departure, f.total_price, f.currency,
                f.link, times="", date_back=f.date_back, nights=f.nights,
                airline=f.airline,
                airline_back=f.airline if f.date_back else "",
            ) for f in fares]

        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for res in pool.map(self._guard(work, "lot"), pairs):
                rows += res
        return rows

    # -- Lufthansa ----------------------------------------------------------

    def lufthansa_rows(self) -> list[dict]:
        """Each origin-dest pair costs several real browser navigations (see
        lufthansa.py), so - unlike Ryanair/WizzAir/LOT - this does not fan
        out across every airport in the country. It searches the explicit
        origins the user picked (or just the country's primary airport) against
        the destination country's primary airport, capped to a handful of
        pairs so one search can't accidentally queue minutes of navigations."""
        api = self.c.lufthansa
        p = self.p
        origin_codes = ([o.upper() for o in p["origins"]] if p["origins"]
                        else [a["code"] for a in self.c.airports(p["from"])[:2]])
        dest_codes = ([d.upper() for d in p["dests"]] if p["dests"]
                      else [a["code"] for a in self.c.airports(p["to"])[:2]])
        pairs = [(o, d) for o in origin_codes[:2] for d in dest_codes[:2]][:4]
        self._add_total(len(pairs))
        if not pairs:
            return []

        def work(pair):
            origin, dest = pair
            if p["trip"] == "return":
                fares = api.round_trip(origin, dest, p["date_from"], p["date_to"],
                                       p["nights"], p["nights_tol"], p["max_price"])
            else:
                fares = api.one_way(origin, dest, p["date_from"], p["date_to"],
                                    p["max_price"])
            return [_row(
                "lufthansa", f.origin, f.dest, f.departure, f.total_price,
                f.currency, f.link, times="", date_back=f.date_back, nights=f.nights,
                airline=f.airline, airline_back=f.airline if f.date_back else "",
            ) for f in fares]

        rows: list[dict] = []
        # patchright's sync API is thread-affined to whichever thread first
        # drives the browser - a pool of >1 workers hits "no running event
        # loop" the moment a second thread reaches it, even with the lock
        # inside lufthansa.py serializing the actual page interactions. One
        # worker keeps every call on the same thread; `_guard` still streams
        # each pair's rows as they land.
        with ThreadPoolExecutor(max_workers=1) as pool:
            for res in pool.map(self._guard(work, "lufthansa"), pairs):
                rows += res
        return rows

    # -- China Airlines -------------------------------------------------

    def chinaairlines_rows(self) -> list[dict]:
        """Same shape as lufthansa_rows above - a handful of explicit pairs,
        not a full country fan-out, because each pair costs several real
        navigations. China Airlines stacks three bot-protection vendors
        (Akamai + Imperva Incapsula + DataDome) rather than Lufthansa's one,
        and is noticeably flakier - a failed pair just yields no rows for it,
        same as any other `_guard`-wrapped source."""
        api = self.c.chinaairlines
        p = self.p
        origin_codes = ([o.upper() for o in p["origins"]] if p["origins"]
                        else [a["code"] for a in self.c.airports(p["from"])[:2]])
        dest_codes = ([d.upper() for d in p["dests"]] if p["dests"]
                      else [a["code"] for a in self.c.airports(p["to"])[:2]])
        pairs = [(o, d) for o in origin_codes[:2] for d in dest_codes[:2]][:4]
        self._add_total(len(pairs))
        if not pairs:
            return []

        def work(pair):
            origin, dest = pair
            if p["trip"] == "return":
                fares = api.round_trip(origin, dest, p["date_from"], p["date_to"],
                                       p["nights"], p["nights_tol"], p["max_price"])
            else:
                fares = api.one_way(origin, dest, p["date_from"], p["date_to"],
                                    p["max_price"])
            return [_row(
                "chinaairlines", f.origin, f.dest, f.departure, f.total_price,
                f.currency, f.link, times="", date_back=f.date_back, nights=f.nights,
                airline=f.airline, airline_back=f.airline if f.date_back else "",
            ) for f in fares]

        rows: list[dict] = []
        # see the matching comment in lufthansa_rows: patchright's sync API
        # is thread-affined, so this must stay at one worker.
        with ThreadPoolExecutor(max_workers=1) as pool:
            for res in pool.map(self._guard(work, "chinaairlines"), pairs):
                rows += res
        return rows

    # -- Kiwi.com (everything else) ---------------------------------------

    def easyjet_rows(self) -> list[dict]:
        return self._kiwi_backed("easyjet", include=EASYJET_CODES)

    def kiwi_rows(self) -> list[dict]:
        exclude = []
        for name in self.p["carriers"]:
            if name in GROUP_CODES:
                exclude += GROUP_CODES[name]
        return self._kiwi_backed("kiwi", exclude=exclude)

    def _kiwi_backed(self, carrier: str, include: list[str] | None = None,
                     exclude: list[str] | None = None) -> list[dict]:
        api = self.c.kiwi
        p = self.p
        # one request covers a whole country pair, so this is a single job
        self._add_total(1)

        def work(_):
            if p["trip"] == "return":
                back_from, back_to = self.return_window
                fares = api.round_trip(
                    p["from"], p["to"], p["date_from"], p["date_to"],
                    back_from, back_to, p["nights"], p["nights_tol"],
                    p["adults"], p["origins"] or None, p["max_stops"],
                    p["limit"], p["max_price"], exclude_carriers=exclude,
                    include_carriers=include, dests=p["dests"] or None)
            else:
                fares = api.one_way(
                    p["from"], p["to"], p["date_from"], p["date_to"],
                    p["adults"], p["origins"] or None, p["max_stops"],
                    p["limit"], p["max_price"], exclude_carriers=exclude,
                    include_carriers=include, dests=p["dests"] or None)
            return [_row(
                carrier, f.origin, f.dest, f.departure, f.total_price,
                f.currency, f.link,
                origin_name=f.origin_name, dest_name=f.dest_name,
                times=f.departure_times, flight_number=f.flight_number,
                date_back=f.date_back, times_back=f.times_back,
                flight_number_back=f.flight_number_back, nights=f.nights,
                airline=f.airline, airline_back=f.airline_back, stops=f.stops,
            ) for f in fares]

        return self._guard(work, carrier)(None)

    # -- combos: two separate one-way tickets stitched into one trip --------

    def _legs_in_direction(self, frm: str, to: str, origins: list[str],
                           dests: list[str],
                           window: tuple[date, date] | None = None,
                           weekdays: set[int] | None = None) -> list[dict]:
        """Every one-way leg the selected carriers price for frm -> to.

        Runs each carrier's own `*_rows()` against a sub-run whose params are
        the requested direction in one-way, per-day mode - per-day because
        pairing an outbound with a return needs a price for each date, not one
        cheapest-per-route summary. `origins`/`dests` are per-direction: on the
        way back they are the mirror image of the outbound ones, so the
        airport narrowing has to be passed in rather than inherited. `window`
        likewise - the return leg departs a stay-length later than the
        outbound one, so searching it over the outbound's own dates finds
        nothing to pair with.
        """
        d_from, d_to = window or (self.p["date_from"], self.p["date_to"])
        sub_params = {**self.p, "from": frm, "to": to, "origins": origins,
                      "dests": dests, "trip": "oneway", "mode": "calendar",
                      "date_from": d_from, "date_to": d_to,
                      "weekdays": self.p["weekdays"] if weekdays is None
                                  else weekdays}
        # a sub-run that reports progress into this run's counters, and never
        # streams its raw one-way legs as if they were finished results
        sub = SearchRun(self.c, sub_params, lambda *a, **k: None)
        sub._add_total = self._add_total   # type: ignore[method-assign]
        sub._tick = self._tick             # type: ignore[method-assign]

        # carriers in parallel, exactly as the main search path runs them - a
        # sequential loop here made the combo search take as long as every
        # carrier's slowest route added together, per direction
        rows: list[dict] = []
        names = list(self.p["carriers"])

        def one(name):
            try:
                return getattr(sub, f"{name}_rows")()
            except Exception as e:  # noqa: BLE001 - one dead carrier is not fatal
                self.emit("warn", {"carrier": f"{name} ({frm}-{to})",
                                   "message": str(e)[:200]})
                return []

        if not names:
            return rows
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            for res in pool.map(one, names):
                rows += res
        return rows

    def combo_rows(self) -> list[dict]:
        """Trips built from two independently-booked one-way tickets.

        This is what makes two things possible that a single carrier's own
        return search cannot express:

          * mixing airlines - fly out on Wizz Air, come back on Ryanair
          * open-jaw - fly out of Gdansk, fly back into Poznan

        Both fall out of the same mechanism: search one-way legs in each
        direction independently, then pair them up by stay length. The two legs
        are separate tickets, so the row carries both booking links and the
        caller is responsible for booking each.
        """
        p = self.p
        if p["trip"] != "return":
            return []   # a combo is by definition a return trip

        # The return leg mirrors the outbound: it departs from wherever the
        # outbound lands and comes back to the requested return airports (or,
        # without open-jaw, where the trip started). Both are known from the
        # request itself rather than from the outbound results, which is what
        # lets the two directions be searched at the same time instead of one
        # after the other. An empty list means "anywhere in that country" -
        # legs that pair with nothing are dropped by the pairing below anyway.
        back_origins = p["dests"]
        if p["return_dests"]:
            back_dests = p["return_dests"]
        elif p["open_jaw"]:
            back_dests = []                       # any airport in the origin country
        else:
            back_dests = p["origins"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_out = pool.submit(self._legs_in_direction, p["from"], p["to"],
                                  p["origins"], p["dests"])
            # the weekday criterion is about when the trip *starts*, so it
            # constrains the outbound only - the return lands wherever the
            # stay length puts it
            fut_back = pool.submit(self._legs_in_direction, p["to"], p["from"],
                                   back_origins, back_dests,
                                   self.return_window, set())
            out_rows, back_rows = fut_out.result(), fut_back.result()
        if not out_rows or not back_rows:
            return []

        if not p["open_jaw"] and not p["return_dests"] and not p["origins"]:
            # no explicit airports anywhere: the return still has to land where
            # the trip actually started, which is only known now
            starts = {r["origin"] for r in out_rows}
            back_rows = [r for r in back_rows if r["dest"] in starts]
            if not back_rows:
                return []

        lo, hi = max(0, p["nights"] - p["nights_tol"]), p["nights"] + p["nights_tol"]
        # group returns by the city they leave from, so an outbound only pairs
        # with returns that actually depart where it landed
        by_from: dict[str, list[dict]] = {}
        for r in back_rows:
            by_from.setdefault(r["origin"], []).append(r)

        best: dict[tuple, dict] = {}
        for o in out_rows:
            for b in by_from.get(o["dest"], []):
                gap = (date.fromisoformat(b["date"]) - date.fromisoformat(o["date"])).days
                if not (lo <= gap <= hi):
                    continue
                if not p["mixed_carriers"] and b["carrier"] != o["carrier"]:
                    continue
                # the two legs are priced in their own departure markets'
                # currencies (PL out in PLN, ES back in EUR), so they have to
                # be converted before they can be added at all
                cur = self.display_currency
                total = fx.total([(o["price"], o["currency"]),
                                  (b["price"], b["currency"])], cur)
                if total is None:
                    continue   # unconvertible: better no row than a wrong one
                if p["max_price"] is not None and total > p["max_price"]:
                    continue
                # one row per (route, return airport, outbound day) - the
                # cheapest return inside the window wins, same shape as the
                # single-carrier return searches produce
                key = (o["origin"], o["dest"], b["dest"], o["date"])
                if key in best and best[key]["price"] <= total:
                    continue
                best[key] = _row(
                    "combo", o["origin"], o["dest"], o["date"], total,
                    cur, o["link"],
                    origin_name=o["origin_name"], dest_name=o["dest_name"],
                    times=o["times"], flight_number=o["flight_number"],
                    date_back=b["date"], times_back=b["times"],
                    flight_number_back=b["flight_number"],
                    price_out=fx.convert(o["price"], o["currency"], cur),
                    price_back=fx.convert(b["price"], b["currency"], cur),
                    nights=gap,
                    airline=o["airline"] or o["carrier"],
                    airline_back=b["airline"] or b["carrier"],
                    stops=max(o.get("stops", 0), b.get("stops", 0)),
                    dest_back=b["dest"] if b["dest"] != o["origin"] else "",
                    dest_back_name=b["dest_name"] if b["dest"] != o["origin"] else "",
                    link_back=b["link"],
                )

        rows = sorted(best.values(), key=lambda r: r["price"])[:p["limit"]]
        if rows:
            self.emit("rows", {"rows": rows})
        return rows

    # -- plumbing ---------------------------------------------------------

    def _to_display(self, r: dict) -> bool:
        """Restate one row in `display_currency` in place. False (and the row
        gets dropped) if the rate isn't available - a row whose price silently
        means a different currency than the rest of the table is worse than a
        missing row."""
        cur = self.display_currency
        if (r.get("currency") or "").upper() == cur.upper():
            return True
        got = fx.convert(r["price"], r.get("currency", ""), cur)
        if got is None:
            return False
        r["price"] = round(got, 2)
        for k in ("price_out", "price_back"):
            if r.get(k) is not None:
                part = fx.convert(r[k], r["currency"], cur)
                r[k] = None if part is None else round(part, 2)
        r["currency"] = cur
        return True

    def _guard(self, fn, carrier):
        def wrapped(job):
            try:
                res = fn(job)
                # not every carrier's fare endpoint can be told "Madrid only"
                # (several only take a destination country), so the narrowing
                # is enforced here as well - one choke point every carrier's
                # rows pass through, rather than per-carrier
                if res and self.p["dests"]:
                    wanted = set(self.p["dests"])
                    res = [r for r in res if r["dest"] in wanted]
                if res and self.p["weekdays"]:
                    days = self.p["weekdays"]
                    res = [r for r in res
                           if date.fromisoformat(r["date"]).weekday() in days]
                # every row in one currency, so sorting, the cheapest-per-route
                # dedupe and the relative price cap - all of which compare
                # prices across rows - are comparing like with like
                res = [r for r in res if self._to_display(r)]
                if res:
                    names = self.c.names()
                    for r in res:
                        r["origin_name"] = r["origin_name"] or names.get(r["origin"], "")
                        r["dest_name"] = r["dest_name"] or names.get(r["dest"], "")
                    # stream partial hits so the fast carrier shows up straight
                    # away instead of waiting for the slow one
                    self.emit("rows", {"rows": res})
                return res
            except Exception as e:  # noqa: BLE001 - a dead route must not kill the run
                self.emit("warn", {"carrier": carrier, "message": str(e)[:200]})
                return []
            finally:
                self._tick()
        return wrapped

    def run(self) -> None:
        rows: list[dict] = []
        threads = []
        results: dict[str, list[dict]] = {}

        def runner(name, fn):
            try:
                results[name] = fn()
            except Exception as e:  # noqa: BLE001
                results[name] = []
                self.emit("warn", {"carrier": name, "message": str(e)[:300]})

        sources = list(self.p["carriers"])
        if self.p["combos"] and self.p["trip"] == "return":
            # combos re-search both directions themselves, so they run as their
            # own source rather than as one of the per-carrier ones
            sources.append("combo")

        for name in sources:
            fn = getattr(self, f"{name}_rows")
            t = threading.Thread(target=runner, args=(name, fn), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        for name in sources:
            rows += results.get(name, [])
        if self.p["mode"] != "calendar":
            rows = _cheapest_per_route(rows)
        names = self.c.names()
        for r in rows:
            r["origin_name"] = r["origin_name"] or names.get(r["origin"], "")
            r["dest_name"] = r["dest_name"] or names.get(r["dest"], "")
            if r.get("dest_back"):
                r["dest_back_name"] = (r.get("dest_back_name")
                                       or names.get(r["dest_back"], ""))
        rows.sort(key=lambda r: r["price"])
        # relative price cap, applied only now that every source has reported:
        # what counts as an outlier is defined against the cheapest fare found,
        # which isn't known while rows are still streaming in
        dropped = 0
        if rows and self.p["max_ratio"]:
            cap = rows[0]["price"] * self.p["max_ratio"]
            kept = [r for r in rows if r["price"] <= cap]
            dropped = len(rows) - len(kept)
            rows = kept
        self.emit("results", {"rows": rows})
        self.emit("done", {"count": len(rows), "dropped_over_ratio": dropped})


# ---------------------------------------------------------------- HTTP

def parse_params(qs: dict) -> dict:
    def one(k, default=None):
        v = qs.get(k, [default])
        return v[0] if v else default

    def flag(k):
        return str(one(k, "")).lower() in ("1", "true", "yes", "on")

    carriers = [c for c in (one("carriers", ",".join(CARRIERS)) or "").split(",")
                if c in CARRIERS]
    origins = [o.strip().upper() for o in (one("origins", "") or "").split(",")
               if o.strip()]
    dests = [d.strip().upper() for d in (one("dests", "") or "").split(",")
             if d.strip()]
    return_dests = [o.strip().upper()
                    for o in (one("return_dests", "") or "").split(",") if o.strip()]
    # 0 = Monday .. 6 = Sunday, matching date.weekday()
    weekdays = sorted({int(d) for d in (one("weekdays", "") or "").split(",")
                       if d.strip().isdigit() and 0 <= int(d) <= 6})
    weekday_tol = max(0, min(3, int(one("weekday_tol", "0") or 0)))
    max_price = one("max_price")
    max_ratio = one("max_ratio")
    return {
        "from": (one("from") or "").lower(),
        "to": (one("to") or "").lower(),
        "origins": origins,
        "dests": dests,
        # which weekday the outbound may leave on. The tolerance widens the set
        # rather than the dates: picking Monday with +/-1 also accepts Sunday
        # and Tuesday, which is how people actually plan around a weekend.
        "weekdays": expand_weekdays(weekdays, weekday_tol),
        "weekdays_picked": weekdays,
        "weekday_tol": weekday_tol,
        # drop anything priced above cheapest * this, so one 1400 zl outlier
        # doesn't sit in a list of 174 zl fares. None = keep everything.
        "max_ratio": float(max_ratio) if max_ratio else None,
        "return_dests": return_dests,
        # naming an explicit return airport implies open-jaw is wanted
        "open_jaw": flag("open_jaw") or bool(return_dests),
        "combos": flag("combos"),
        "mixed_carriers": flag("mixed_carriers"),
        "date_from": date.fromisoformat(one("date_from")),
        "date_to": date.fromisoformat(one("date_to")),
        "adults": int(one("adults", "1")),
        "max_price": float(max_price) if max_price else None,
        "carriers": carriers or list(CARRIERS),
        "mode": one("mode", "cheapest"),
        "trip": "return" if one("trip") == "return" else "oneway",
        "nights": max(0, int(one("nights", "7") or 7)),
        "nights_tol": max(0, int(one("nights_tol", "2") or 0)),
        "max_stops": max(0, int(one("max_stops", "0") or 0)),
        "limit": min(500, max(10, int(one("limit", "150") or 150))),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    clients: Clients = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ----------------------------------------------------------

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes -----------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if u.path in ("/noclegi", "/noclegi.html"):
                return self._file(STATIC / "noclegi.html")
            if u.path == "/app.css":
                # shared stylesheet, so the flights page and the stays page
                # cannot drift apart visually
                return self._file(STATIC / "app.css")
            if u.path == "/api/carriers":
                return self._json([{"key": k, **v} for k, v in CARRIERS.items()])
            if u.path == "/api/countries":
                return self._json(self.clients.countries())
            if u.path == "/api/airports":
                country = (qs.get("country", [""])[0] or "").lower()
                if not country:
                    return self._json({"error": "country required"}, 400)
                return self._json(self.clients.airports(country))
            if u.path == "/api/stay-filters":
                return self._json({
                    "sources": list(st.SOURCES),
                    "filters": [{"key": k, "label": v["label"],
                                 "sources": [s for s in st.SOURCES if v.get(s)]}
                                for k, v in st.FILTERS.items()],
                })
            if u.path == "/api/stays":
                return self._json(self.stays(qs))
            if u.path == "/api/search":
                return self.sse_search(qs)
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._json({"error": str(e)}, 500)
            except Exception:  # noqa: BLE001 - client already gone
                pass

    def stays(self, qs: dict) -> dict:
        def one(k, default=None):
            v = qs.get(k, [default])
            return v[0] if v else default

        def flag(k):
            return str(one(k, "")).lower() in ("1", "true", "yes", "on")

        bedrooms = one("bedrooms")
        pmin, pmax = one("min_night"), one("max_night")
        sources = tuple(s for s in (one("sources", ",".join(st.SOURCES)) or "").split(",")
                        if s in st.SOURCES) or st.SOURCES
        filters = [f for f in (one("filters", "") or "").split(",") if f]
        return self.clients.stays.for_flight(
            dest_iata=(one("dest") or "").upper(),
            checkin=one("checkin"),
            checkout=one("checkout"),
            radius_km=float(one("radius_km", "30") or 30),
            adults=int(one("adults", "2") or 2),
            bedrooms=int(bedrooms) if bedrooms else None,
            prop_type=one("type", "any") or "any",
            filters=filters,
            price_min=float(pmin) if pmin else None,
            price_max=float(pmax) if pmax else None,
            limit=int(one("limit", "24") or 24),
            sources=sources,
            bands=int(one("bands", "4") or 4),
        )

    def sse_search(self, qs: dict) -> None:
        try:
            params = parse_params(qs)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"bad params: {e}"}, 400)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        q: queue.Queue = queue.Queue()

        def emit(event: str, data: dict) -> None:
            q.put((event, data))

        def worker():
            try:
                SearchRun(self.clients, params, emit).run()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                emit("error", {"message": str(e)})
            finally:
                q.put(None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = q.get()
            if item is None:
                break
            event, data = item
            chunk = (f"event: {event}\n"
                     f"data: {json.dumps(data, ensure_ascii=False)}\n\n")
            try:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break  # browser navigated away mid-search


def main() -> None:
    ap = argparse.ArgumentParser(description="Fare scraper web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--market", default="pl-pl",
                    help="carrier market/locale, drives currency and language")
    ap.add_argument("--currency", default="PLN",
                    help="currency for Kiwi results (the two low-cost carriers "
                         "price in their own market currency)")
    args = ap.parse_args()

    Handler.clients = Clients(args.market, args.currency)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fare scraper UI on http://{args.host}:{args.port}  "
          f"(market {args.market})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)


if __name__ == "__main__":
    main()
