#!/usr/bin/env python3
"""LOT Polish Airlines fare scraper - straight from lot.com, no aggregator.

LOT's booking engine ("IBE") powers the low fare calendar on lot.com, and its
price endpoint answers directly once the request is shaped the way the Angular
client shapes it:

  POST https://www.lot.com/api/v1/session/start        -> {"id": <sessionId>}
  GET  https://www.lot.com/api/v1/ibe/search/prices
         ?marketCode=pl&originIATA=WAW&destinationIATA=BCN
         &startDate=YYYY-MM-DD&tripType=O|R&fixedDepartureDate=false|true
       headers: channel: 1, remoteIP: 127.0.0.1, language, market,
                sessionId, step: SEARCH, action: DO_SEARCH
  GET  https://www.lot.com/api/pl/pl/lowfarecalendarairports.json
       -> ~1000 origin/destination pairs LOT actually flies

The header values are not guesses - they are the literal arguments the site's
own bundle passes to `ibeAirBestPricesSearch`. Getting them wrong is quietly
fatal: `step`/`action` as numbers validate fine and then return
500 {"code":"INTERNAL_ERROR"}, which looks like the endpoint is broken.

Two more things worth knowing:

  * one call returns ~152 days of prices, so a whole season is one request
  * prices come in minor units (59824 = 598.24 PLN)
  * lot.com rejects plain `requests` on TLS fingerprint, hence curl_cffi

Usage:
  python lot.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-30
  python lot.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-30 \
      --round-trip --nights 7 --nights-tol 2
  python lot.py routes --from pl --to es
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
from datetime import date

from curl_cffi import requests as cr

SITE = "https://www.lot.com"
IMPERSONATE = "chrome"
AIRLINE = "LOT Polish Airlines"


@dataclass
class Fare:
    origin: str
    dest: str
    departure: str
    price: float
    currency: str
    airline: str = AIRLINE
    date_back: str = ""
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


class Lot:
    def __init__(self, market: str = "pl-pl", rate: float = 0.05,
                 timeout: int = 40):
        # `rate` used to be 0.4s, which alone made a return search take ~26s:
        # the seed call plus up to 14 fixed-departure re-queries per route,
        # all serialised behind one global gap. Measured, lot.com does not
        # throttle at this volume - 24 requests eight-at-a-time complete in
        # 1.1s with no 429/503 - so the gap is now nominal and `prices()`
        # backs off only when the server actually pushes back.
        parts = market.split("-")
        self.language = parts[0].lower()
        self.market_code = parts[-1].lower()
        self.timeout = timeout
        self.limiter = RateLimiter(rate)
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._routes: list[dict] | None = None

        self.session = cr.Session(impersonate=IMPERSONATE)
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Language": f"{self.language}-{self.market_code.upper()};q=0.9",
            "Origin": SITE,
            "Referer": f"{SITE}/{self.language}/{self.market_code}",
        })

    # ---------- session ----------

    @property
    def session_id(self) -> str:
        with self._lock:
            if self._session_id is None:
                self.limiter.wait()
                # the HTML load seeds the cookies the session endpoint expects
                self.session.get(f"{SITE}/{self.language}/{self.market_code}",
                                 timeout=self.timeout)
                r = self.session.post(f"{SITE}/api/v1/session/start", data="{}",
                                      headers={"Content-Type": "application/json"},
                                      timeout=self.timeout)
                r.raise_for_status()
                self._session_id = r.json()["id"]
            return self._session_id

    def _headers(self) -> dict:
        return {
            "channel": "1",
            "remoteIP": "127.0.0.1",
            "language": self.language,
            "market": self.market_code,
            "sessionId": self.session_id,
            "step": "SEARCH",
            "action": "DO_SEARCH",
        }

    # ---------- reference data ----------

    def routes(self) -> list[dict]:
        """Origin/destination pairs LOT prices in its low fare calendar."""
        if self._routes is None:
            self.limiter.wait()
            r = self.session.get(
                f"{SITE}/api/{self.language}/{self.market_code}"
                f"/lowfarecalendarairports.json", timeout=90)
            r.raise_for_status()
            seen, out = set(), []
            for b in r.json().get("priceBoxes", []):
                key = (b["originAirportIATA"], b["destinationAirportIATA"])
                if key in seen:
                    continue
                seen.add(key)
                out.append({"origin": key[0], "dest": key[1],
                            "origin_name": b.get("originAirportName", ""),
                            "dest_name": b.get("destinationAirportName", "")})
            self._routes = out
        return self._routes

    def airports(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for r in self.routes():
            names.setdefault(r["origin"], r["origin_name"])
            names.setdefault(r["dest"], r["dest_name"])
        return names

    def routes_between(self, origin_codes: set[str],
                       dest_codes: set[str]) -> list[tuple[str, str]]:
        return sorted({(r["origin"], r["dest"]) for r in self.routes()
                       if r["origin"] in origin_codes and r["dest"] in dest_codes})

    # ---------- prices ----------

    def prices(self, origin: str, dest: str, start: date,
               trip_type: str = "O", fixed_departure: bool = False) -> list[dict]:
        """Raw per-day prices. One call covers roughly 152 days from `start`."""
        self.limiter.wait()
        url = (f"{SITE}/api/v1/ibe/search/prices"
               f"?marketCode={self.market_code}&originIATA={origin}"
               f"&destinationIATA={dest}&startDate={start.isoformat()}"
               f"&tripType={trip_type}"
               f"&fixedDepartureDate={'true' if fixed_departure else 'false'}")
        r = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        # react to throttling rather than pre-empting it (see __init__)
        for attempt in range(3):
            if r.status_code not in (429, 503):
                break
            self.limiter.min_interval = min(5.0, max(self.limiter.min_interval, 2.0 * 2 ** attempt))
            time.sleep(2.0 * 2 ** attempt)
            r = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        # a route with no service on that date is data, not a failure - LOT
        # reports it as 400 {"errors":[{"code":"39360","title":"NO FLIGHT FOUND"}]}
        if r.status_code == 400 and "39360" in r.text:
            return []
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {origin}-{dest} :: "
                               f"{r.text[:160]}")
        data = r.json().get("data") or {}
        bounds = data.get("bounds") or []
        if not bounds:
            return []
        return [{**p, "currency": data.get("currencyCode", "")}
                for p in bounds[0].get("prices") or []]

    def booking_link(self, origin: str, dest: str, day: str,
                     back: str | None = None) -> str:
        return (f"{SITE}/{self.language}/{self.market_code}/book/availability"
                f"/departure?tripType={'R' if back else 'O'}"
                f"&origin={origin}&destination={dest}&departureDate={day}"
                + (f"&returnDate={back}" if back else ""))

    def one_way(self, origin: str, dest: str, date_from: date, date_to: date,
                max_price: float | None = None) -> list[Fare]:
        out = []
        for p in self.prices(origin, dest, date_from):
            day = p["departureDate"]
            if not (date_from.isoformat() <= day <= date_to.isoformat()):
                continue
            price = p["price"] / 100.0          # minor units
            if max_price is not None and price > max_price:
                continue
            out.append(Fare(origin=origin, dest=dest, departure=day,
                            price=price, currency=p["currency"],
                            total_price=price,
                            link=self.booking_link(origin, dest, day)))
        return out

    def round_trip(self, origin: str, dest: str, date_from: date, date_to: date,
                   nights: int, nights_tol: int,
                   max_price: float | None = None,
                   max_departures: int = 14) -> list[Fare]:
        """Cheapest return inside the stay window, per outbound day.

        The cheap first call (`fixedDepartureDate=false`) picks LOT's own return
        date, which ignores the requested stay length. So it is used only to rank
        outbound days; the `max_departures` cheapest then get a second call that
        prices every return date for that departure.
        """
        seed = [p for p in self.prices(origin, dest, date_from, "R")
                if date_from.isoformat() <= p["departureDate"] <= date_to.isoformat()]
        seed.sort(key=lambda p: p["price"])
        lo, hi = max(0, nights - nights_tol), nights + nights_tol

        def best_for(dep: date) -> Fare | None:
            best = None
            for q in self.prices(origin, dest, dep, "R", fixed_departure=True):
                back = q.get("returnDate")
                if not back:
                    continue
                gap = (date.fromisoformat(back) - dep).days
                if lo <= gap <= hi and (best is None or q["price"] < best["price"]):
                    best = q
            if best is None:
                return None
            price = best["price"] / 100.0
            if max_price is not None and price > max_price:
                return None
            back = best["returnDate"]
            return Fare(
                origin=origin, dest=dest, departure=dep.isoformat(),
                price=price, currency=best["currency"], total_price=price,
                date_back=back,
                nights=(date.fromisoformat(back) - dep).days,
                link=self.booking_link(origin, dest, dep.isoformat(), back))

        # the re-queries are independent of each other, so they run together
        # rather than one after another behind the limiter
        deps = [date.fromisoformat(p["departureDate"]) for p in seed[:max_departures]]
        if not deps:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(deps))) as pool:
            return [f for f in pool.map(best_for, deps) if f is not None]


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


COUNTRY_HINT = {
    # the low fare calendar feed has no country field, so the CLI needs a way to
    # turn "pl" into airport codes; the web UI passes explicit codes instead
    "pl": {"WAW", "KRK", "GDN", "POZ", "WRO", "KTW", "RZE", "SZZ", "BZG",
           "LCJ", "LUZ", "SZY", "RDO", "IEG"},
    "es": {"BCN", "MAD", "AGP", "ALC", "PMI", "VLC", "SVQ", "BIO", "TFS",
           "LPA", "IBZ", "XRY", "FUE", "ACE"},
}


def _codes(arg_country: str, explicit: list[str] | None) -> set[str]:
    if explicit:
        return {c.upper() for c in explicit}
    codes = COUNTRY_HINT.get(arg_country.lower())
    if not codes:
        raise SystemExit(f"no airport list for country '{arg_country}' - pass "
                         f"--origins/--dests with IATA codes instead")
    return codes


def cmd_routes(api: Lot, args) -> None:
    pairs = api.routes_between(_codes(args.from_country, args.origins),
                               _codes(args.to_country, args.dests))
    write_output([{"origin": o, "dest": d} for o, d in pairs],
                 args.out, args.format)


def cmd_scan(api: Lot, args) -> None:
    d_from = date.fromisoformat(args.date_from)
    d_to = date.fromisoformat(args.date_to)
    pairs = api.routes_between(_codes(args.from_country, args.origins),
                               _codes(args.to_country, args.dests))
    if not args.quiet:
        print(f"{len(pairs)} LOT routes", file=sys.stderr)

    def work(pair):
        o, d = pair
        if args.round_trip:
            return api.round_trip(o, d, d_from, d_to, args.nights,
                                  args.nights_tol, args.max_price)
        return api.one_way(o, d, d_from, d_to, args.max_price)

    fares: list[Fare] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, p): p for p in pairs}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                fares += fut.result()
            except Exception as e:  # noqa: BLE001 - one failed route must not abort the scan
                print(f"  ! {futs[fut]}: {e}", file=sys.stderr)
            if not args.quiet:
                print(f"\rroutes: {i}/{len(futs)}", end="", file=sys.stderr,
                      flush=True)
    if not args.quiet:
        print(file=sys.stderr)

    fares.sort(key=lambda f: f.total_price)
    rows = [asdict(f) for f in fares]
    if not args.quiet and rows:
        print(f"\n{'route':<10} {'date':<12} {'back':<12} {'price':>12}",
              file=sys.stderr)
        print("-" * 50, file=sys.stderr)
        for r in rows[:args.top]:
            print(f"{r['origin'] + '-' + r['dest']:<10} {r['departure']:<12} "
                  f"{r['date_back'] or '-':<12} "
                  f"{r['total_price']:>8.2f} {r['currency']}", file=sys.stderr)
    write_output(rows, args.out, args.format)


def main() -> None:
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--market", default="pl-pl")
    g.add_argument("--rate", type=float, default=0.05)
    g.add_argument("--workers", type=int, default=4)
    g.add_argument("--format", choices=["csv", "json"], default="csv")
    g.add_argument("--out")
    g.add_argument("--quiet", action="store_true")

    p = argparse.ArgumentParser(description="LOT Polish Airlines fare scraper",
                                parents=[g])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--from", dest="from_country", default="pl")
        sp.add_argument("--to", dest="to_country", default="es")
        sp.add_argument("--origins", nargs="+")
        sp.add_argument("--dests", nargs="+")

    r = sub.add_parser("routes", help="LOT routes between two countries",
                       parents=[g])
    common(r)
    r.set_defaults(func=cmd_routes)

    s = sub.add_parser("scan", help="per-day fares on every matching route",
                       parents=[g])
    common(s)
    s.add_argument("--date-from", required=True)
    s.add_argument("--date-to", required=True)
    s.add_argument("--max-price", type=float)
    s.add_argument("--round-trip", action="store_true")
    s.add_argument("--nights", type=int, default=7)
    s.add_argument("--nights-tol", type=int, default=2)
    s.add_argument("--top", type=int, default=25)
    s.set_defaults(func=cmd_scan)

    args = p.parse_args()
    args.func(Lot(market=args.market, rate=args.rate), args)


if __name__ == "__main__":
    main()
