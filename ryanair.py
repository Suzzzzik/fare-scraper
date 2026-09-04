#!/usr/bin/env python3
"""Ryanair fare scraper - cheap flights between two countries (default PL -> ES).

Uses Ryanair's own public JSON endpoints (the ones www.ryanair.com calls itself):

  GET /api/views/locate/5/airports/{lang}/active
        -> all active airports with country codes
  GET /api/views/locate/searchWidget/routes/{lang}/airport/{IATA}
        -> destinations actually served from that airport
  GET /api/farfnd/v4/oneWayFares
        -> cheapest one-way fare PER DESTINATION in a date range
  GET /api/farfnd/v4/roundTripFares
        -> cheapest round trip PER DESTINATION, filtered by trip duration
  GET /api/farfnd/3/oneWayFares/{orig}/{dest}/cheapestPerDay?outboundMonthOfDate=YYYY-MM-01
        -> cheapest fare for every day of one month on one route

NOTE: /api/booking/v4/{market}/availability (full flight list with seats) is
bot-protected and returns 409 "Availability declined" for plain HTTP clients.
The farfnd endpoints above are not, and cover price hunting fine.

Usage:
  python ryanair.py scan --from pl --to es --date-from 2026-08-10 --date-to 2026-09-10
  python ryanair.py scan --from pl --to es --round-trip --duration 3-10 --max-price 400
  python ryanair.py calendar --from pl --to es --date-from 2026-08-01 --date-to 2026-10-31
  python ryanair.py airports --country es
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.ryanair.com/api"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class RateLimiter:
    """Simple minimum-interval throttle shared across worker threads."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next = now + self.min_interval


class Ryanair:
    def __init__(self, market: str = "pl-pl", currency: str = "PLN",
                 lang: str = "en", rate: float = 0.25, timeout: int = 20):
        self.market = market
        self.currency = currency
        self.lang = lang
        self.timeout = timeout
        self.limiter = RateLimiter(rate)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
            "Referer": "https://www.ryanair.com/",
            "Origin": "https://www.ryanair.com",
        })
        retry = Retry(
            total=4,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry,
                                                   pool_maxsize=32))

    def get(self, path: str, **params):
        self.limiter.wait()
        r = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {r.url} :: {r.text[:200]}")
        return r.json()

    # ---------- reference data ----------

    def airports(self, country: str | None = None) -> list[dict]:
        data = self.get(f"/views/locate/5/airports/{self.lang}/active")
        if country:
            country = country.lower()
            data = [a for a in data if a["country"]["code"].lower() == country]
        return sorted(data, key=lambda a: a["code"])

    def routes_from(self, iata: str) -> list[dict]:
        return self.get(f"/views/locate/searchWidget/routes/{self.lang}/airport/{iata}")

    def destinations_in(self, iata: str, country: str) -> list[str]:
        country = country.lower()
        return sorted({
            r["arrivalAirport"]["code"]
            for r in self.routes_from(iata)
            if r["arrivalAirport"]["country"]["code"].lower() == country
        })

    # ---------- fares ----------

    def one_way_fares(self, origin: str, arrival_country: str,
                      date_from: str, date_to: str, adults: int = 1,
                      max_price: float | None = None) -> list[dict]:
        p = {
            "departureAirportIataCode": origin,
            "arrivalCountryCode": arrival_country.lower(),
            "outboundDepartureDateFrom": date_from,
            "outboundDepartureDateTo": date_to,
            "outboundDepartureTimeFrom": "00:00",
            "outboundDepartureTimeTo": "23:59",
            "market": self.market,
            "adultPaxCount": adults,
            "currency": self.currency,
        }
        if max_price is not None:
            p["priceValueTo"] = max_price
        return self.get("/farfnd/v4/oneWayFares", **p).get("fares") or []

    def round_trip_fares(self, origin: str, arrival_country: str,
                         out_from: str, out_to: str, in_from: str, in_to: str,
                         duration_from: int = 2, duration_to: int = 7,
                         adults: int = 1,
                         max_price: float | None = None) -> list[dict]:
        p = {
            "departureAirportIataCode": origin,
            "arrivalCountryCode": arrival_country.lower(),
            "outboundDepartureDateFrom": out_from,
            "outboundDepartureDateTo": out_to,
            "inboundDepartureDateFrom": in_from,
            "inboundDepartureDateTo": in_to,
            "durationFrom": duration_from,
            "durationTo": duration_to,
            "outboundDepartureTimeFrom": "00:00",
            "outboundDepartureTimeTo": "23:59",
            "inboundDepartureTimeFrom": "00:00",
            "inboundDepartureTimeTo": "23:59",
            "market": self.market,
            "adultPaxCount": adults,
            "currency": self.currency,
        }
        if max_price is not None:
            p["priceValueTo"] = max_price
        return self.get("/farfnd/v4/roundTripFares", **p).get("fares") or []

    def cheapest_per_day(self, origin: str, dest: str, month: date) -> list[dict]:
        data = self.get(
            f"/farfnd/3/oneWayFares/{origin}/{dest}/cheapestPerDay",
            outboundMonthOfDate=month.replace(day=1).isoformat(),
            currency=self.currency,
        )
        return (data.get("outbound") or {}).get("fares") or []


# ---------- normalised output rows ----------

@dataclass
class Fare:
    origin: str
    origin_name: str
    dest: str
    dest_name: str
    dest_country: str
    departure: str
    arrival: str = ""
    flight_number: str = ""
    price: float = 0.0
    currency: str = ""
    return_departure: str = ""
    return_flight_number: str = ""
    return_price: float | None = None
    total_price: float = 0.0
    link: str = ""


def _leg(o: dict) -> dict:
    return {
        "origin": o["departureAirport"]["iataCode"],
        "origin_name": o["departureAirport"]["name"],
        "dest": o["arrivalAirport"]["iataCode"],
        "dest_name": o["arrivalAirport"]["name"],
        "dest_country": o["arrivalAirport"]["city"]["countryCode"],
        "departure": o["departureDate"],
        "arrival": o.get("arrivalDate") or "",
        "flight_number": o.get("flightNumber") or "",
        "price": (o.get("price") or {}).get("value") or 0.0,
        "currency": (o.get("price") or {}).get("currencyCode") or "",
    }


def booking_link(origin: str, dest: str, out_date: str,
                 in_date: str | None = None, adults: int = 1,
                 market: str = "pl-pl") -> str:
    lang = market.split("-")[0]
    country = market.split("-")[1]
    return (
        f"https://www.ryanair.com/{lang}/{country}/trip/flights/select"
        f"?adults={adults}&teens=0&children=0&infants=0"
        f"&dateOut={out_date}&dateIn={in_date or ''}"
        f"&isConnectedFlight=false&discount=0"
        f"&isReturn={'true' if in_date else 'false'}"
        f"&originIata={origin}&destinationIata={dest}"
    )


def fare_from_oneway(f: dict, adults: int, market: str) -> Fare:
    leg = _leg(f["outbound"])
    out_date = leg["departure"][:10]
    return Fare(**leg, total_price=leg["price"],
                link=booking_link(leg["origin"], leg["dest"], out_date,
                                  adults=adults, market=market))


def fare_from_roundtrip(f: dict, adults: int, market: str) -> Fare:
    leg = _leg(f["outbound"])
    inb = f.get("inbound") or {}
    total = ((f.get("summary") or {}).get("price") or {}).get("value") \
        or (leg["price"] + ((inb.get("price") or {}).get("value") or 0.0))
    out_date = leg["departure"][:10]
    in_date = (inb.get("departureDate") or "")[:10] or None
    return Fare(
        **leg,
        return_departure=inb.get("departureDate") or "",
        return_flight_number=inb.get("flightNumber") or "",
        return_price=(inb.get("price") or {}).get("value"),
        total_price=round(total, 2),
        link=booking_link(leg["origin"], leg["dest"], out_date, in_date,
                          adults=adults, market=market),
    )


# ---------- helpers ----------

def parse_duration(s: str) -> tuple[int, int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return int(a), int(b)
    return int(s), int(s)


def months_between(d1: date, d2: date):
    cur = d1.replace(day=1)
    last = d2.replace(day=1)
    while cur <= last:
        yield cur
        cur = (cur + timedelta(days=32)).replace(day=1)


def parallel(fn, items, workers: int, label: str, quiet: bool):
    out, errors = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fn, it): it for it in items}
        for i, fut in enumerate(as_completed(futs), 1):
            it = futs[fut]
            try:
                out.extend(fut.result())
            except Exception as e:  # noqa: BLE001 - one bad route must not kill the run
                errors.append((it, str(e)))
            if not quiet:
                print(f"\r{label}: {i}/{len(futs)}", end="", file=sys.stderr,
                      flush=True)
    if not quiet:
        print(file=sys.stderr)
    for it, e in errors:
        print(f"  ! {it}: {e}", file=sys.stderr)
    return out


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

    cols = list(rows[0].keys())
    fh = open(out_path, "w", newline="", encoding="utf-8") if out_path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    finally:
        if out_path:
            fh.close()


def print_table(rows: list[dict], limit: int, price_key: str) -> None:
    if not rows:
        return
    print(f"\n{'route':<12} {'departure':<17} {'return':<17} "
          f"{'price':>10}  flight", file=sys.stderr)
    print("-" * 78, file=sys.stderr)
    for r in rows[:limit]:
        route = f"{r['origin']}->{r['dest']}"
        dep = (r.get("departure") or "").replace("T", " ")[:16]
        ret = (r.get("return_departure") or "").replace("T", " ")[:16] or "-"
        price = f"{r.get(price_key, 0):.2f} {r.get('currency', '')}"
        print(f"{route:<12} {dep:<17} {ret:<17} {price:>10}  "
              f"{r.get('flight_number', '')}", file=sys.stderr)


# ---------- commands ----------

def cmd_airports(api: Ryanair, args) -> None:
    rows = [
        {
            "code": a["code"],
            "name": a["name"],
            "city": a["city"]["name"],
            "country": a["country"]["name"],
            "country_code": a["country"]["code"],
            "base": a["base"],
            "lat": a["coordinates"]["latitude"],
            "lon": a["coordinates"]["longitude"],
        }
        for a in api.airports(args.country)
    ]
    write_output(rows, args.out, args.format)


def cmd_scan(api: Ryanair, args) -> None:
    origins = args.origins or [a["code"] for a in api.airports(args.from_country)]
    if not args.quiet:
        print(f"origins ({len(origins)}): {', '.join(origins)}", file=sys.stderr)

    if args.round_trip:
        dfrom, dto = parse_duration(args.duration)
        in_from = (date.fromisoformat(args.date_from) + timedelta(days=dfrom)).isoformat()
        in_to = (date.fromisoformat(args.date_to) + timedelta(days=dto)).isoformat()

        def work(origin: str):
            fares = api.round_trip_fares(
                origin, args.to_country, args.date_from, args.date_to,
                in_from, in_to, dfrom, dto, args.adults, args.max_price)
            return [fare_from_roundtrip(f, args.adults, api.market) for f in fares]
    else:
        def work(origin: str):
            fares = api.one_way_fares(
                origin, args.to_country, args.date_from, args.date_to,
                args.adults, args.max_price)
            return [fare_from_oneway(f, args.adults, api.market) for f in fares]

    fares = parallel(work, origins, args.workers, "scanning", args.quiet)
    fares.sort(key=lambda f: f.total_price)
    rows = [asdict(f) for f in fares]
    if not args.quiet:
        print_table(rows, args.top, "total_price")
    write_output(rows, args.out, args.format)


def cmd_calendar(api: Ryanair, args) -> None:
    origins = args.origins or [a["code"] for a in api.airports(args.from_country)]
    d_from = date.fromisoformat(args.date_from)
    d_to = date.fromisoformat(args.date_to)

    if not args.quiet:
        print(f"resolving routes from {len(origins)} airports...", file=sys.stderr)
    pairs: list[tuple[str, str]] = []
    for o in origins:
        try:
            pairs += [(o, d) for d in api.destinations_in(o, args.to_country)]
        except Exception as e:  # noqa: BLE001 - one origin with no routes must not abort the scan
            print(f"  ! routes {o}: {e}", file=sys.stderr)
    if not args.quiet:
        print(f"{len(pairs)} routes to {args.to_country.upper()}", file=sys.stderr)

    jobs = [(o, d, m) for o, d in pairs for m in months_between(d_from, d_to)]

    def work(job):
        origin, dest, month = job
        out = []
        for f in api.cheapest_per_day(origin, dest, month):
            if f.get("unavailable") or f.get("soldOut") or not f.get("price"):
                continue
            day = date.fromisoformat(f["day"])
            if not (d_from <= day <= d_to):
                continue
            price = f["price"]["value"]
            if args.max_price is not None and price > args.max_price:
                continue
            out.append(Fare(
                origin=origin, origin_name="", dest=dest, dest_name="",
                dest_country=args.to_country.lower(),
                departure=f.get("departureDate") or f["day"],
                arrival=f.get("arrivalDate") or "",
                price=price, currency=f["price"]["currencyCode"],
                total_price=price,
                link=booking_link(origin, dest, f["day"],
                                  adults=args.adults, market=api.market),
            ))
        return out

    fares = parallel(work, jobs, args.workers, "days", args.quiet)
    fares.sort(key=lambda f: f.total_price)
    rows = [asdict(f) for f in fares]
    if not args.quiet:
        print_table(rows, args.top, "total_price")
    write_output(rows, args.out, args.format)


def main() -> None:
    # shared options, accepted both before and after the subcommand
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--market", default="pl-pl")
    g.add_argument("--currency", default="PLN")
    g.add_argument("--rate", type=float, default=0.25,
                   help="min seconds between requests (default 0.25)")
    g.add_argument("--workers", type=int, default=6)
    g.add_argument("--format", choices=["csv", "json"], default="csv")
    g.add_argument("--out", help="output file (default: stdout)")
    g.add_argument("--quiet", action="store_true")

    p = argparse.ArgumentParser(description="Ryanair cheap-fare scraper",
                                parents=[g])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("airports", help="list active airports", parents=[g])
    a.add_argument("--country", help="ISO2 country code filter, e.g. pl")
    a.set_defaults(func=cmd_airports)

    def common(sp):
        sp.add_argument("--from", dest="from_country", default="pl",
                        help="origin country ISO2 (default pl)")
        sp.add_argument("--to", dest="to_country", default="es",
                        help="destination country ISO2 (default es)")
        sp.add_argument("--origins", nargs="+",
                        help="explicit origin IATA codes, overrides --from")
        sp.add_argument("--date-from", required=True)
        sp.add_argument("--date-to", required=True)
        sp.add_argument("--adults", type=int, default=1)
        sp.add_argument("--max-price", type=float)
        sp.add_argument("--top", type=int, default=25,
                        help="rows shown in the summary table")

    s = sub.add_parser("scan", help="cheapest fare per destination (fast)",
                       parents=[g])
    common(s)
    s.add_argument("--round-trip", action="store_true")
    s.add_argument("--duration", default="2-7",
                   help="round-trip length in nights, e.g. 3-10")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("calendar",
                       help="cheapest fare for every day on every route (slow)",
                       parents=[g])
    common(c)
    c.set_defaults(func=cmd_calendar)

    args = p.parse_args()
    api = Ryanair(market=args.market, currency=args.currency, rate=args.rate)
    args.func(api, args)


if __name__ == "__main__":
    main()
