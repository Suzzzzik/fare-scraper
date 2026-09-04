#!/usr/bin/env python3
"""Wizz Air fare scraper - cheap flights between two countries (default PL -> ES).

Uses Wizz Air's own backend (Navitaire New Skies) at be.wizzair.com. The API is
versioned; the version is read live from the booking page instead of hardcoded:

  GET  https://www.wizzair.com/{market}/booking/select-flight/...
        -> HTML containing  apiUrl:"https://be.wizzair.com/<VERSION>/Api"

  GET  {api}/asset/map?languageCode={market}
        -> every city, its country, and its direct connections (the route graph)
  POST {api}/search/timetable
        -> cheapest fare for every day in a date range, one route
           (max range 42 days, else 400 {"validationCodes":["InvalidTimeDateRange"]})
  POST {api}/asset/farechart
        -> cheapest fare for date +/- dayInterval days, one route
  GET  {api}/asset/currencies
        -> supported currency codes

Two things that make this API confusing to talk to:

  * after the first response, every request must echo the RequestVerificationToken
    cookie back in an X-RequestVerificationToken header, else the API answers
    400 {"handlerError":"InvalidProtocol"}
  * airports sharing a metropolitan code in asset/map (KRK+KTW = SPQ,
    WAW+WMI = WSW) are searched together, so results can name a neighbouring
    airport as departureStation

NOTE: POST {api}/search/search (full flight list with fare classes and seats)
answers 429 for plain HTTP clients - it needs a real booking session. The
timetable/farechart endpoints are not protected and cover price hunting.

Usage:
  python wizzair.py scan --from pl --to es --date-from 2026-08-10 --date-to 2026-09-10
  python wizzair.py calendar --from pl --to es --date-from 2026-08-01 --date-to 2026-10-31
  python wizzair.py airports --country es
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SITE = "https://www.wizzair.com"
FALLBACK_API = "https://be.wizzair.com/29.9.0/Api"
MAX_RANGE_DAYS = 42  # server rejects wider timetable windows
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


RETRIES = 3          # attempts after a 429/503 before giving up
BACKOFF = 2.0        # seconds, doubled each retry


class RateLimiter:
    """Minimum-interval throttle shared across worker threads.

    `penalise` widens the interval after the server pushes back, so a run that
    does trip the throttle slows down for the rest of its life instead of
    hammering; a run that never trips it stays fast.
    """

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

    def penalise(self, seconds: float) -> None:
        with self._lock:
            self.min_interval = min(5.0, max(self.min_interval, seconds))


class WizzAir:
    def __init__(self, market: str = "pl-pl", rate: float = 0.15,
                 timeout: int = 25, api_url: str | None = None):
        self.market = market
        self.timeout = timeout
        self.limiter = RateLimiter(rate)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
            "Referer": f"{SITE}/",
            "Origin": SITE,
        })
        # be.wizzair.com throttles hard: 429/503 come fast under concurrency
        retry = Retry(
            total=6,
            backoff_factor=3.0,
            respect_retry_after_header=True,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry,
                                                   pool_maxsize=32))
        self.api = api_url or self._discover_api()
        self._map_cache: dict | None = None

    def _discover_api(self) -> str:
        """Read apiUrl out of the booking page so the version stays current."""
        url = (f"{SITE}/{self.market}/booking/select-flight"
               f"/WAW/BCN/{date.today().isoformat()}/null/1/0/0/null")
        try:
            r = self.session.get(url, timeout=self.timeout,
                                 headers={"Accept": "text/html"})
            m = re.search(r'apiUrl:"(https://be\.wizzair\.com/[\d.]+/Api)"', r.text)
            if m:
                return m.group(1)
        except requests.RequestException:
            pass
        print(f"warn: API version discovery failed, using {FALLBACK_API}",
              file=sys.stderr)
        return FALLBACK_API

    def _request(self, method: str, path: str, payload: dict | None = None,
                 **params):
        self.limiter.wait()
        url = f"{self.api}/{path}"
        headers = {}
        # The first response sets a RequestVerificationToken cookie; every later
        # call must echo it back in this header or the API answers
        # 400 {"handlerError":"InvalidProtocol"}.
        token = self.session.cookies.get("RequestVerificationToken")
        if token:
            headers["X-RequestVerificationToken"] = token
        r = self.session.request(method, url, params=params or None,
                                 json=payload, headers=headers or None,
                                 timeout=self.timeout)
        if r.status_code == 400 and "InvalidProtocol" in r.text:
            # token raced with another thread, or expired - retry once with the
            # value the server just handed back
            token = self.session.cookies.get("RequestVerificationToken")
            if token:
                self.limiter.wait()
                r = self.session.request(
                    method, url, params=params or None, json=payload,
                    headers={"X-RequestVerificationToken": token},
                    timeout=self.timeout)
        # Throttling is handled by reacting to it, not by crawling: a flat
        # pre-emptive gap big enough to never trip the limiter made a 23-route
        # search spend ~35s asleep, while the same requests four-at-a-time
        # finished in 1.5s with no errors. Back off only when actually told to.
        for attempt in range(RETRIES):
            if r.status_code not in (429, 503):
                break
            self.limiter.penalise(BACKOFF * (2 ** attempt))
            time.sleep(BACKOFF * (2 ** attempt))
            r = self.session.request(method, url, params=params or None,
                                     json=payload, headers=headers or None,
                                     timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {url} :: {r.text[:200]}")
        return r.json()

    # ---------- reference data ----------

    def route_map(self) -> dict:
        if self._map_cache is None:
            self._map_cache = self._request("GET", "asset/map",
                                            languageCode=self.market)
        return self._map_cache

    def cities(self, country: str | None = None) -> list[dict]:
        data = self.route_map()["cities"]
        if country:
            country = country.upper()
            data = [c for c in data if c["countryCode"] == country]
        return sorted(data, key=lambda c: c["iata"])

    def currencies(self) -> list[str]:
        return self._request("GET", "asset/currencies")["currencies"]

    def routes_between(self, from_country: str, to_country: str,
                       origins: list[str] | None = None,
                       dests: list[str] | None = None) -> list[tuple[str, str]]:
        """Direct routes from one country to another, from the live route map.

        `dests` narrows the destination side the same way `origins` narrows the
        source side - e.g. Madrid only, rather than all of Spain.
        """
        dest_codes = {c["iata"] for c in self.cities(to_country)}
        if dests:
            dest_codes &= {d.upper() for d in dests}
        src = self.cities(from_country)
        if origins:
            wanted = {o.upper() for o in origins}
            src = [c for c in src if c["iata"] in wanted]
        pairs = []
        for c in src:
            for conn in c["connections"]:
                if conn["iata"] in dest_codes and conn.get("isDirectFlight", True):
                    pairs.append((c["iata"], conn["iata"]))
        return sorted(set(pairs))

    # ---------- fares ----------

    def timetable(self, origin: str, dest: str, date_from: date, date_to: date,
                  adults: int = 1, children: int = 0, infants: int = 0,
                  round_trip: bool = False,
                  return_from: date | None = None,
                  return_to: date | None = None) -> dict:
        """Per-day prices for one route. With round_trip the response also
        carries returnFlights; the return leg can use its own date window, which
        is what a "stay N nights" search needs."""
        flights = [{"departureStation": origin, "arrivalStation": dest,
                    "from": date_from.isoformat(), "to": date_to.isoformat()}]
        if round_trip:
            flights.append({"departureStation": dest, "arrivalStation": origin,
                            "from": (return_from or date_from).isoformat(),
                            "to": (return_to or date_to).isoformat()})
        return self._request("POST", "search/timetable", {
            "flightList": flights,
            "priceType": "regular",
            "adultCount": adults,
            "childCount": children,
            "infantCount": infants,
        })

    def farechart(self, origin: str, dest: str, day: date,
                  day_interval: int = 3, adults: int = 1,
                  children: int = 0) -> dict:
        return self._request("POST", "asset/farechart", {
            "isRescueFare": False,
            "adultCount": adults,
            "childCount": children,
            "dayInterval": day_interval,
            "wdc": True,
            "isFlightChange": False,
            "flightList": [{"departureStation": origin,
                            "arrivalStation": dest,
                            "date": day.isoformat()}],
        })


# ---------- normalised output rows ----------

@dataclass
class Fare:
    origin: str
    dest: str
    departure: str
    departure_times: str = ""
    price: float = 0.0
    currency: str = ""
    price_type: str = ""
    total_price: float = 0.0
    link: str = ""


def booking_link(origin: str, dest: str, out_date: str,
                 in_date: str | None = None, adults: int = 1,
                 market: str = "pl-pl") -> str:
    return (f"{SITE}/{market}/booking/select-flight/{origin}/{dest}/"
            f"{out_date}/{in_date or 'null'}/{adults}/0/0/null")


def fares_from_timetable(data: dict, key: str, origin: str, dest: str,
                         d_from: date, d_to: date, max_price: float | None,
                         adults: int, market: str,
                         include_unpriced: bool = False) -> list[Fare]:
    out = []
    for f in data.get(key) or []:
        price = (f.get("price") or {}).get("amount")
        ptype = f.get("priceType", "")
        # "checkPrice" means the API withheld the fare and returns amount 0.0
        if price is None or (ptype != "price" and not include_unpriced):
            continue
        day = f["departureDate"][:10]
        if not (d_from.isoformat() <= day <= d_to.isoformat()):
            continue
        if max_price is not None and price > max_price:
            continue
        o = f.get("departureStation", origin)
        d = f.get("arrivalStation", dest)
        out.append(Fare(
            origin=o, dest=d, departure=day,
            departure_times=",".join(t[11:16] for t in f.get("departureDates") or []),
            price=price, currency=f["price"]["currencyCode"], price_type=ptype,
            total_price=price,
            link=booking_link(o, d, day, adults=adults, market=market),
        ))
    return out


# ---------- helpers ----------

def chunk_ranges(d_from: date, d_to: date, size: int = MAX_RANGE_DAYS):
    cur = d_from
    while cur <= d_to:
        end = min(cur + timedelta(days=size - 1), d_to)
        yield cur, end
        cur = end + timedelta(days=1)


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
    print(f"\n{'route':<12} {'date':<12} {'price':>12}  times", file=sys.stderr)
    print("-" * 66, file=sys.stderr)
    for r in rows[:limit]:
        route = f"{r['origin']}-{r['dest']}"
        price = f"{r['total_price']:.2f} {r['currency']}"
        print(f"{route:<12} {r['departure']:<12} {price:>12}  "
              f"{r.get('departure_times', '')}", file=sys.stderr)


# ---------- commands ----------

def cmd_airports(api: WizzAir, args) -> None:
    rows = [
        {
            "code": c["iata"],
            "name": c["shortName"],
            "country": c["countryName"],
            "country_code": c["countryCode"],
            "currency": c["currencyCode"],
            "lat": c["latitude"],
            "lon": c["longitude"],
            "connections": len(c["connections"]),
        }
        for c in api.cities(args.country)
    ]
    write_output(rows, args.out, args.format)


def _collect(api: WizzAir, args, per_route_cheapest: bool) -> list[dict]:
    d_from = date.fromisoformat(args.date_from)
    d_to = date.fromisoformat(args.date_to)
    pairs = api.routes_between(args.from_country, args.to_country, args.origins)
    if not args.quiet:
        print(f"{len(pairs)} direct routes "
              f"{args.from_country.upper()}-{args.to_country.upper()}",
              file=sys.stderr)
    if not pairs:
        return []

    jobs = [(o, d, a, b) for o, d in pairs
            for a, b in chunk_ranges(d_from, d_to)]

    def work(job):
        origin, dest, a, b = job
        data = api.timetable(origin, dest, a, b, args.adults,
                             round_trip=args.round_trip)
        rows = fares_from_timetable(data, "outboundFlights", origin, dest,
                                    d_from, d_to, args.max_price,
                                    args.adults, api.market,
                                    args.include_unpriced)
        if args.round_trip:
            rows += fares_from_timetable(data, "returnFlights", dest, origin,
                                         d_from, d_to, args.max_price,
                                         args.adults, api.market,
                                         args.include_unpriced)
        return rows

    fares = parallel(work, jobs, args.workers, "timetable", args.quiet)

    if per_route_cheapest:
        best: dict[tuple[str, str], Fare] = {}
        for f in fares:
            k = (f.origin, f.dest)
            if k not in best or f.total_price < best[k].total_price:
                best[k] = f
        fares = list(best.values())

    fares.sort(key=lambda f: f.total_price)
    return [asdict(f) for f in fares]


def cmd_scan(api: WizzAir, args) -> None:
    rows = _collect(api, args, per_route_cheapest=True)
    if not args.quiet:
        print_table(rows, args.top)
    write_output(rows, args.out, args.format)


def cmd_calendar(api: WizzAir, args) -> None:
    rows = _collect(api, args, per_route_cheapest=False)
    if not args.quiet:
        print_table(rows, args.top)
    write_output(rows, args.out, args.format)


def main() -> None:
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--market", default="pl-pl")
    g.add_argument("--api-url", help="override be.wizzair.com API base")
    g.add_argument("--rate", type=float, default=0.15,
                   help="min seconds between requests (default 1.5 - the API "
                        "throttles hard, lower it at your own risk)")
    g.add_argument("--workers", type=int, default=2)
    g.add_argument("--format", choices=["csv", "json"], default="csv")
    g.add_argument("--out", help="output file (default: stdout)")
    g.add_argument("--quiet", action="store_true")

    p = argparse.ArgumentParser(description="Wizz Air cheap-fare scraper",
                                parents=[g])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("airports", help="list cities in the route map",
                       parents=[g])
    a.add_argument("--country", help="ISO2 country code filter, e.g. pl")
    a.set_defaults(func=cmd_airports)

    def common(sp):
        sp.add_argument("--from", dest="from_country", default="pl")
        sp.add_argument("--to", dest="to_country", default="es")
        sp.add_argument("--origins", nargs="+",
                        help="explicit origin IATA codes, subset of --from")
        sp.add_argument("--date-from", required=True)
        sp.add_argument("--date-to", required=True)
        sp.add_argument("--adults", type=int, default=1)
        sp.add_argument("--max-price", type=float)
        sp.add_argument("--round-trip", action="store_true",
                        help="also fetch the return leg prices")
        sp.add_argument("--include-unpriced", action="store_true",
                        help='keep priceType="checkPrice" rows (amount 0.0)')
        sp.add_argument("--top", type=int, default=25)

    s = sub.add_parser("scan", help="cheapest fare per route", parents=[g])
    common(s)
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("calendar", help="fare for every day on every route",
                       parents=[g])
    common(c)
    c.set_defaults(func=cmd_calendar)

    args = p.parse_args()
    api = WizzAir(market=args.market, rate=args.rate, api_url=args.api_url)
    if not args.quiet:
        print(f"api: {api.api}", file=sys.stderr)
    args.func(api, args)


if __name__ == "__main__":
    main()
