#!/usr/bin/env python3
"""Kiwi.com fare scraper - every airline, not just the two with open APIs.

Ryanair and Wizz Air publish their own fare endpoints; almost nobody else does
(easyJet, Norwegian, Vueling, Volotea, Eurowings, Transavia, LOT and friends all
sit behind Akamai/Cloudflare bot management). Kiwi.com's own website API is the
practical way to reach the rest: one GraphQL endpoint, no key, and it returns the
operating carrier for every result.

  POST https://api.skypicker.com/umbrella/v2/graphql
        onewayItineraries(search: SearchOnewayInput, ...)
        returnItineraries(search: SearchReturnInput, ...)   <- has nightsCount
  GET  https://api.skypicker.com/locations?type=dump&location_types=country
        -> 223 countries
  GET  https://api.skypicker.com/locations?type=subentity&term=PL&location_types=airport
        -> airports in a country

Two things make this work at all:

  * the endpoint rejects plain Python clients on TLS fingerprint, so requests go
    through curl_cffi with a Chrome impersonation profile
  * search source/destination take Kiwi ids - "Country:PL", "Station:WAW" -
    so a whole country pair is one request instead of one per airport

Prices are Kiwi's, which can differ from the airline's own site - it sometimes
combines carriers, and `stops > 0` results may be self-transfer. Cross-check the
row's airline before booking.

Usage:
  python kiwi.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-20
  python kiwi.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-20 \
      --round-trip --nights 7 --nights-tol 2
  python kiwi.py airports --country es
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import date

from curl_cffi import requests as cr

GRAPHQL = "https://api.skypicker.com/umbrella/v2/graphql"
LOCATIONS = "https://api.skypicker.com/locations"
IMPERSONATE = "chrome"
SITE = "https://www.kiwi.com"

_SEGMENT = """
  code
  carrier { code name }
  source { localTime station { code name country { code } } }
  destination { localTime station { code name country { code } } }
"""

ONEWAY_QUERY = """
query($search: SearchOnewayInput!, $filter: ItinerariesFilterInput,
      $options: ItinerariesOptionsInput) {
  onewayItineraries(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { error: message }
    ... on Itineraries {
      itineraries {
        __typename
        id
        price { amount }
        ... on ItineraryOneWay {
          sector { sectorSegments { segment { %s } } }
        }
      }
    }
  }
}""" % _SEGMENT

RETURN_QUERY = """
query($search: SearchReturnInput!, $filter: ItinerariesFilterInput,
      $options: ItinerariesOptionsInput) {
  returnItineraries(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { error: message }
    ... on Itineraries {
      itineraries {
        __typename
        id
        price { amount }
        ... on ItineraryReturn {
          outbound { sectorSegments { segment { %s } } }
          inbound  { sectorSegments { segment { %s } } }
        }
      }
    }
  }
}""" % (_SEGMENT, _SEGMENT)


@dataclass
class Fare:
    origin: str
    origin_name: str
    dest: str
    dest_name: str
    departure: str
    departure_times: str
    airline: str
    flight_number: str
    stops: int
    price: float
    currency: str
    date_back: str = ""
    times_back: str = ""
    airline_back: str = ""
    flight_number_back: str = ""
    nights: int | None = None
    total_price: float = 0.0
    link: str = ""


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.min_interval


class Kiwi:
    def __init__(self, market: str = "pl-pl", currency: str = "PLN",
                 rate: float = 1.0, timeout: int = 60):
        self.market = market
        self.currency = currency
        self.timeout = timeout
        self.limiter = RateLimiter(rate)
        self._lock = threading.Lock()
        self._countries: list[dict] | None = None
        self._airports: dict[str, list[dict]] = {}
        self.session = cr.Session(impersonate=IMPERSONATE)
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": SITE,
            "Referer": SITE + "/",
        })

    @property
    def partner_market(self) -> str:
        return self.market.split("-")[-1].lower()

    # ---------- reference data ----------

    def countries(self) -> list[dict]:
        if self._countries is None:
            self.limiter.wait()
            r = self.session.get(
                LOCATIONS, params={"type": "dump", "location_types": "country",
                                   "limit": "500", "locale": "en-US",
                                   "active_only": "true"},
                timeout=self.timeout)
            r.raise_for_status()
            self._countries = sorted(
                ({"code": c["code"].lower(), "name": c["name"]}
                 for c in r.json().get("locations", [])),
                key=lambda c: c["name"])
        return self._countries

    def airports(self, country: str) -> list[dict]:
        country = country.upper()
        with self._lock:
            cached = self._airports.get(country)
        if cached is not None:
            return cached
        self.limiter.wait()
        r = self.session.get(
            LOCATIONS, params={"type": "subentity", "term": country,
                               "location_types": "airport", "limit": "200",
                               "locale": "en-US", "active_only": "true"},
            timeout=self.timeout)
        r.raise_for_status()
        out = sorted(({"code": a["code"], "name": a["name"]}
                      for a in r.json().get("locations", [])),
                     key=lambda a: a["code"])
        with self._lock:
            self._airports[country] = out
        return out

    # ---------- search ----------

    def _post(self, query: str, variables: dict, feature: str) -> dict:
        self.limiter.wait()
        r = self.session.post(f"{GRAPHQL}?featureName={feature}",
                              data=json.dumps({"query": query,
                                               "variables": variables}),
                              timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} :: {r.text[:200]}")
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"GraphQL: {body['errors'][0].get('message')}")
        return body["data"]

    @staticmethod
    def _ids(country: str, airports: list[str] | None) -> dict:
        if airports:
            return {"ids": [f"Station:{a.upper()}" for a in airports]}
        return {"ids": [f"Country:{country.upper()}"]}

    @staticmethod
    def _filter(max_stops: int, limit: int,
                exclude_carriers: list[str] | None = None,
                include_carriers: list[str] | None = None) -> dict:
        f = {"maxStopsCount": max_stops, "limit": limit}
        if include_carriers:
            # restrict to one airline - used to surface a carrier whose own site
            # cannot be scraped (easyJet)
            f["carriers"] = list(dict.fromkeys(include_carriers))
        elif exclude_carriers:
            # keeps Kiwi from repeating what a directly-queried carrier already
            # returned, so it only adds airlines nothing else covers
            f["excludeCarriers"] = list(dict.fromkeys(exclude_carriers))
        return f

    def _options(self) -> dict:
        return {"currency": self.currency.lower(), "locale": "en",
                "partner": "skypicker", "partnerMarket": self.partner_market,
                "sortBy": "PRICE"}

    @staticmethod
    def _sector(sector: dict) -> list[dict]:
        return [s["segment"] for s in (sector or {}).get("sectorSegments", [])]

    def _leg(self, segments: list[dict]) -> dict:
        first, last = segments[0], segments[-1]
        return {
            "origin": first["source"]["station"]["code"],
            "origin_name": first["source"]["station"]["name"],
            "dest": last["destination"]["station"]["code"],
            "dest_name": last["destination"]["station"]["name"],
            "date": first["source"]["localTime"][:10],
            "time": first["source"]["localTime"][11:16],
            "airline": " / ".join(
                dict.fromkeys(s["carrier"]["name"] for s in segments)),
            "flight_number": " / ".join(
                f"{s['carrier']['code']}{s['code']}" for s in segments),
            "stops": len(segments) - 1,
        }

    def search_link(self, origin: str, dest: str, day: str,
                    back: str | None = None) -> str:
        tail = f"/{day}" + (f"/{back}" if back else "")
        return f"{SITE}/en/search/results/{origin}/{dest}{tail}"

    def one_way(self, from_country: str, to_country: str,
                date_from: date, date_to: date, adults: int = 1,
                origins: list[str] | None = None, max_stops: int = 0,
                limit: int = 100, max_price: float | None = None,
                exclude_carriers: list[str] | None = None,
                include_carriers: list[str] | None = None,
                dests: list[str] | None = None) -> list[Fare]:
        variables = {
            "search": {
                "itinerary": {
                    "source": self._ids(from_country, origins),
                    "destination": self._ids(to_country, dests),
                    "outboundDepartureDate": {
                        "start": f"{date_from}T00:00:00",
                        "end": f"{date_to}T23:59:59"},
                },
                "passengers": {"adults": adults},
            },
            "filter": self._filter(max_stops, limit, exclude_carriers,
                                   include_carriers),
            "options": self._options(),
        }
        data = self._post(ONEWAY_QUERY, variables, "SearchOneWayItinerariesQuery")
        res = data["onewayItineraries"]
        if res["__typename"] != "Itineraries":
            raise RuntimeError(res.get("error") or "kiwi returned an error")

        out = []
        for it in res["itineraries"]:
            segs = self._sector(it.get("sector"))
            if not segs:
                continue
            leg = self._leg(segs)
            price = float(it["price"]["amount"])
            if max_price is not None and price > max_price:
                continue
            out.append(Fare(
                origin=leg["origin"], origin_name=leg["origin_name"],
                dest=leg["dest"], dest_name=leg["dest_name"],
                departure=leg["date"], departure_times=leg["time"],
                airline=leg["airline"], flight_number=leg["flight_number"],
                stops=leg["stops"], price=price, currency=self.currency,
                total_price=price,
                link=self.search_link(leg["origin"], leg["dest"], leg["date"]),
            ))
        return out

    def round_trip(self, from_country: str, to_country: str,
                   date_from: date, date_to: date, back_from: date,
                   back_to: date, nights: int, nights_tol: int,
                   adults: int = 1, origins: list[str] | None = None,
                   max_stops: int = 0, limit: int = 100,
                   max_price: float | None = None,
                   exclude_carriers: list[str] | None = None,
                   include_carriers: list[str] | None = None,
                   dests: list[str] | None = None) -> list[Fare]:
        variables = {
            "search": {
                "itinerary": {
                    "source": self._ids(from_country, origins),
                    "destination": self._ids(to_country, dests),
                    "outboundDepartureDate": {
                        "start": f"{date_from}T00:00:00",
                        "end": f"{date_to}T23:59:59"},
                    "inboundDepartureDate": {
                        "start": f"{back_from}T00:00:00",
                        "end": f"{back_to}T23:59:59"},
                    "nightsCount": {"start": max(0, nights - nights_tol),
                                    "end": nights + nights_tol},
                },
                "passengers": {"adults": adults},
            },
            "filter": self._filter(max_stops, limit, exclude_carriers,
                                   include_carriers),
            "options": self._options(),
        }
        data = self._post(RETURN_QUERY, variables, "SearchReturnItinerariesQuery")
        res = data["returnItineraries"]
        if res["__typename"] != "Itineraries":
            raise RuntimeError(res.get("error") or "kiwi returned an error")

        out = []
        for it in res["itineraries"]:
            out_segs = self._sector(it.get("outbound"))
            in_segs = self._sector(it.get("inbound"))
            if not out_segs or not in_segs:
                continue
            o, i = self._leg(out_segs), self._leg(in_segs)
            price = float(it["price"]["amount"])
            if max_price is not None and price > max_price:
                continue
            out.append(Fare(
                origin=o["origin"], origin_name=o["origin_name"],
                dest=o["dest"], dest_name=o["dest_name"],
                departure=o["date"], departure_times=o["time"],
                airline=o["airline"], flight_number=o["flight_number"],
                stops=max(o["stops"], i["stops"]),
                price=price, currency=self.currency, total_price=price,
                date_back=i["date"], times_back=i["time"],
                airline_back=i["airline"], flight_number_back=i["flight_number"],
                nights=(date.fromisoformat(i["date"])
                        - date.fromisoformat(o["date"])).days,
                link=self.search_link(o["origin"], o["dest"], o["date"], i["date"]),
            ))
        return out


# ---------- CLI ----------

def write_output(rows: list[dict], out_path: str | None, fmt: str) -> None:
    if not rows:
        print("no fares found", file=sys.stderr)
        return
    if fmt == "json":
        text = json.dumps(rows, ensure_ascii=False, indent=2)
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        else:
            print(text)
        return
    fh = open(out_path, "w", newline="", encoding="utf-8") if out_path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    finally:
        if out_path:
            fh.close()


def print_table(rows: list[dict], limit: int) -> None:
    if not rows:
        return
    print(f"\n{'route':<12} {'date':<12} {'back':<12} {'price':>12}  airline",
          file=sys.stderr)
    print("-" * 76, file=sys.stderr)
    for r in rows[:limit]:
        print(f"{r['origin'] + '-' + r['dest']:<12} {r['departure']:<12} "
              f"{r.get('date_back') or '-':<12} "
              f"{r['total_price']:.2f} {r['currency']:>4}  {r['airline']}",
              file=sys.stderr)


def cmd_airports(api: Kiwi, args) -> None:
    write_output(api.airports(args.country), args.out, args.format)


def cmd_countries(api: Kiwi, args) -> None:
    write_output(api.countries(), args.out, args.format)


def cmd_scan(api: Kiwi, args) -> None:
    d_from = date.fromisoformat(args.date_from)
    d_to = date.fromisoformat(args.date_to)
    if args.round_trip:
        from datetime import timedelta
        lo = max(0, args.nights - args.nights_tol)
        hi = args.nights + args.nights_tol
        fares = api.round_trip(args.from_country, args.to_country, d_from, d_to,
                               d_from + timedelta(days=lo),
                               d_to + timedelta(days=hi),
                               args.nights, args.nights_tol, args.adults,
                               args.origins, args.max_stops, args.limit,
                               args.max_price)
    else:
        fares = api.one_way(args.from_country, args.to_country, d_from, d_to,
                            args.adults, args.origins, args.max_stops,
                            args.limit, args.max_price)
    fares.sort(key=lambda f: f.total_price)
    rows = [asdict(f) for f in fares]
    if not args.quiet:
        print_table(rows, args.top)
    write_output(rows, args.out, args.format)


def main() -> None:
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--market", default="pl-pl")
    g.add_argument("--currency", default="PLN")
    g.add_argument("--rate", type=float, default=1.0)
    g.add_argument("--format", choices=["csv", "json"], default="csv")
    g.add_argument("--out")
    g.add_argument("--quiet", action="store_true")

    p = argparse.ArgumentParser(description="Kiwi.com cheap-fare scraper",
                                parents=[g])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("countries", help="list countries Kiwi knows",
                       parents=[g])
    c.set_defaults(func=cmd_countries)

    a = sub.add_parser("airports", help="list airports in a country", parents=[g])
    a.add_argument("--country", required=True)
    a.set_defaults(func=cmd_airports)

    s = sub.add_parser("scan", help="cheapest itineraries between two countries",
                       parents=[g])
    s.add_argument("--from", dest="from_country", default="pl")
    s.add_argument("--to", dest="to_country", default="es")
    s.add_argument("--origins", nargs="+")
    s.add_argument("--date-from", required=True)
    s.add_argument("--date-to", required=True)
    s.add_argument("--adults", type=int, default=1)
    s.add_argument("--max-price", type=float)
    s.add_argument("--max-stops", type=int, default=0,
                   help="0 = direct only (default), 1+ allows connections")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--round-trip", action="store_true")
    s.add_argument("--nights", type=int, default=7)
    s.add_argument("--nights-tol", type=int, default=2)
    s.add_argument("--top", type=int, default=25)
    s.set_defaults(func=cmd_scan)

    args = p.parse_args()
    api = Kiwi(market=args.market, currency=args.currency, rate=args.rate)
    args.func(api, args)


if __name__ == "__main__":
    main()
