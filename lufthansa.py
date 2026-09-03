#!/usr/bin/env python3
"""Lufthansa fare scraper.

Lufthansa's booking engine (shop.lufthansa.com, backed by
api.shop.lufthansa.com/one-booking/v2) sits behind a Cloudflare Managed
Challenge. Three things were tried and ruled out before landing on the
approach below:

  1. Plain curl_cffi (TLS impersonation only) - hard 403, no JS ever runs.
  2. Vanilla Playwright-driven Chrome - Cloudflare fingerprints the CDP
     automation channel and loops the "verifying you are human" page
     forever, even though it is a genuine, headed Chrome.
  3. patchright (a patched Playwright build that hides the CDP tells) to
     mint a cf_clearance cookie + bearer token once, then replay both via
     fast curl_cffi calls - the cookie doesn't help: Cloudflare also binds
     clearance to the TLS/HTTP2 fingerprint of the connection that earned
     it, and curl_cffi's `impersonate="chrome"` doesn't match closely
     enough. Every curl_cffi call still comes back 403.
     Injecting a `fetch()` into the already-cleared page via
     `page.evaluate` doesn't work either - the page's own CSP blocks the
     cross-origin call from that injected context ("Failed to fetch").

What does work: patchright, keeping the SAME browser tab open for the whole
process and driving every search as a real navigation in that tab. The
first navigation pays the ~3-10s Cloudflare cost; every navigation after
that in the same tab is fast (~3-4s) because cf_clearance is a genuine
same-connection cookie by then. So this module keeps one patchright Chrome
tab alive for the process lifetime and serializes all fare lookups through
it (a lock - Playwright's sync API isn't safe to drive from multiple
threads at once).

The actual navigation trick, reverse-engineered from the site's own JS: a
plain `page.goto()` with the search baked into the query string gets stuck
in an infinite challenge loop. What the real frontend does instead is land
on www.lufthansa.com/<market>/flight-search, then auto-submit a same-origin
POST form to shop.lufthansa.com/booking/availability. That works.

  POST /booking/availability          (form-submitted navigation) triggers,
                                       as a side effect, the SPA's own:
  POST /one-booking/v2/search/air-calendars  - price-per-day calendar,
                                                captured off the wire via a
                                                response listener rather
                                                than called directly.

Requires `patchright` (`pip install patchright && patchright install chrome`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

API_CALENDARS_PATH = "/one-booking/v2/search/air-calendars"
SHOP = "https://shop.lufthansa.com"
WWW = "https://www.lufthansa.com"
AIRLINE = "Lufthansa"


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


class Lufthansa:
    def __init__(self, market: str = "pl-pl", currency: str = "PLN"):
        parts = market.split("-")
        self.lang = parts[0].lower()
        self.country = parts[-1].upper()
        self.currency = currency
        # patchright's sync API is thread-affined: the browser must always be
        # driven from the exact thread that created it, but callers here
        # (server.py's SearchRun) dispatch each carrier on a fresh
        # `threading.Thread` per search, so a naive lock isn't enough - a
        # second search from a different thread would still crash with
        # "no running event loop". A dedicated worker thread that owns the
        # browser for the lifetime of the process, with calls marshalled to
        # it through a queue, is what actually fixes that.
        self._task_q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._page = None

    # ---------- worker thread (owns every Playwright object) ----------

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._worker_loop, daemon=True)
                self._worker.start()

    def _worker_loop(self) -> None:
        # Playwright's sync API needs a running event loop on this thread;
        # unlike the main thread, a fresh worker thread doesn't get one for
        # free (Python 3.12+ stopped implicitly creating one on demand).
        asyncio.set_event_loop(asyncio.new_event_loop())
        while True:
            item = self._task_q.get()
            if item is None:
                break
            fn, fut = item
            try:
                fut.set_result(fn())
            except Exception as e:  # noqa: BLE001 - the worker must survive any one job failing
                fut.set_exception(e)

    def _call(self, fn):
        self._ensure_worker()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._task_q.put((fn, fut))
        return fut.result()

    # ---------- browser lifecycle (patchright, real Chrome, one tab) ----------
    # everything below this point only ever runs on the worker thread

    def _ensure_page(self):
        if self._page is not None:
            return
        from patchright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch_persistent_context(
            user_data_dir=f"/tmp/lh_patchright_{uuid.uuid4().hex[:8]}",
            channel="chrome", headless=False,
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._browser.new_page()
        self._page.goto(f"{WWW}/{self.lang}/en/flight-search",
                        wait_until="domcontentloaded", timeout=20000)
        self._page.wait_for_timeout(1200)

    def _close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self._page = self._browser = self._pw = None

    def close(self) -> None:
        if self._worker is not None:
            self._call(self._close)
            self._task_q.put(None)
            self._worker.join(timeout=10)
            self._worker = None

    # ---------- one search = one navigation ----------

    def _search_on_worker(self, body: dict) -> list[dict]:
        """POSTs `body` as the site's own auto-submit form does, and returns
        every air-calendars response the resulting page load triggered."""
        self._ensure_page()
        captured: list[str] = []

        def on_response(resp):
            if resp.request.method == "POST" and API_CALENDARS_PATH in resp.url:
                try:
                    captured.append(resp.text())
                except Exception:  # noqa: BLE001 - response body can race a nav
                    pass

        self._page.on("response", on_response)
        try:
            target = f"{SHOP}/booking/availability?lang=en-GB&portalCountry={self.country}"
            self._page.evaluate(
                """([target, searchVal]) => {
                    const f = document.createElement('form');
                    f.method = 'POST'; f.action = target;
                    const inp = document.createElement('input');
                    inp.type = 'hidden'; inp.name = 'search'; inp.value = searchVal;
                    f.appendChild(inp); document.body.appendChild(f); f.submit();
                }""",
                [target, json.dumps(body)],
            )
            self._page.wait_for_load_state("load", timeout=20000)
            self._page.wait_for_timeout(3000)
        finally:
            self._page.remove_listener("response", on_response)

        out = []
        for text in captured:
            try:
                out += json.loads(text).get("data") or []
            except Exception:  # noqa: BLE001 - one unparsable capture must not drop the rest
                continue
        return out

    def _search(self, body: dict) -> list[dict]:
        return self._call(lambda: self._search_on_worker(body))

    def air_calendars(self, origin: str, dest: str, depart: date,
                      flexibility: int = 3, back: date | None = None) -> list[dict]:
        itineraries = [{
            "departureDateTime": f"{depart.isoformat()}T00:00:00.000",
            "originLocationCode": origin, "destinationLocationCode": dest,
            "flexibility": flexibility, "isRequestedBound": True,
        }]
        if back is not None:
            itineraries.append({
                "departureDateTime": f"{back.isoformat()}T00:00:00.000",
                "originLocationCode": dest, "destinationLocationCode": origin,
                "isRequestedBound": False,
            })
        body = {
            "itineraries": itineraries,
            "travelers": [{"discounts": [], "passengerTypeCode": "ADT"}],
            "cabin": "ECONOMY",
            "portalFacts": {"performance": False, "targeting": False, "functional": False},
        }
        return self._search(body)

    # ---------- fares ----------

    def booking_link(self) -> str:
        # The real results page only opens via a same-origin POST-submitted
        # form (see module docstring) - a plain deep link with the search
        # baked into the query string does not work, so this links to the
        # search page instead of a prefilled result.
        return f"{WWW}/{self.lang}/en/flight-search"

    def one_way(self, origin: str, dest: str, date_from: date, date_to: date,
               max_price: float | None = None) -> list[Fare]:
        out: dict[str, Fare] = {}
        span = max(0, (date_to - date_from).days)
        flex = min(6, max(1, span // 2 or 1))
        anchor = date_from
        while anchor <= date_to:
            for entry in self.air_calendars(origin, dest, anchor, flexibility=flex):
                day = entry.get("departureDate")
                if not day or not (date_from.isoformat() <= day <= date_to.isoformat()):
                    continue
                total = ((entry.get("prices") or {}).get("totalPrices") or [{}])[0]
                price = total.get("total")
                if price is None:
                    continue
                price = price / 100.0
                if max_price is not None and price > max_price:
                    continue
                if day not in out or price < out[day].price:
                    out[day] = Fare(origin=origin, dest=dest, departure=day,
                                    price=price,
                                    currency=total.get("currencyCode", self.currency),
                                    total_price=price, link=self.booking_link())
            anchor += timedelta(days=2 * flex + 1)
        return list(out.values())

    def round_trip(self, origin: str, dest: str, date_from: date, date_to: date,
                   nights: int, nights_tol: int,
                   max_price: float | None = None) -> list[Fare]:
        """Cheapest per outbound day, for each stay length in
        [nights-tol, nights, nights+tol] (air-calendars flexes the outbound
        date around the anchor but keeps the paired return date fixed, so
        each stay length needs its own sweep)."""
        out: dict[str, Fare] = {}
        span = max(0, (date_to - date_from).days)
        flex = min(6, max(1, span // 2 or 1))
        lo, hi = max(0, nights - nights_tol), nights + nights_tol
        for offset in sorted({nights - nights_tol, nights, nights + nights_tol}):
            if offset < 0:
                continue
            anchor = date_from
            while anchor <= date_to:
                back = anchor + timedelta(days=offset)
                for entry in self.air_calendars(origin, dest, anchor,
                                                flexibility=flex, back=back):
                    day = entry.get("departureDate")
                    ret = entry.get("returnDate")
                    if not day or not ret:
                        continue
                    if not (date_from.isoformat() <= day <= date_to.isoformat()):
                        continue
                    actual_nights = (date.fromisoformat(ret) - date.fromisoformat(day)).days
                    if not (lo <= actual_nights <= hi):
                        continue
                    total = ((entry.get("prices") or {}).get("totalPrices") or [{}])[0]
                    price = total.get("total")
                    if price is None:
                        continue
                    price = price / 100.0
                    if max_price is not None and price > max_price:
                        continue
                    if day not in out or price < out[day].price:
                        out[day] = Fare(
                            origin=origin, dest=dest, departure=day, price=price,
                            currency=total.get("currencyCode", self.currency),
                            total_price=price, date_back=ret,
                            nights=(date.fromisoformat(ret) - date.fromisoformat(day)).days,
                            link=self.booking_link())
                anchor += timedelta(days=2 * flex + 1)
        return list(out.values())


# ---------- CLI (smoke test) ----------

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Lufthansa fare scraper smoke test")
    ap.add_argument("--from", dest="origin", default="WAW")
    ap.add_argument("--to", dest="dest", default="BCN")
    ap.add_argument("--date-from", default=(date.today() + timedelta(days=14)).isoformat())
    ap.add_argument("--date-to", default=(date.today() + timedelta(days=21)).isoformat())
    ap.add_argument("--round-trip", action="store_true")
    ap.add_argument("--nights", type=int, default=7)
    ap.add_argument("--nights-tol", type=int, default=2)
    args = ap.parse_args()

    api = Lufthansa()
    d_from, d_to = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    try:
        if args.round_trip:
            fares = api.round_trip(args.origin, args.dest, d_from, d_to,
                                   args.nights, args.nights_tol)
        else:
            fares = api.one_way(args.origin, args.dest, d_from, d_to)
    finally:
        api.close()
    fares.sort(key=lambda f: f.total_price)
    for f in fares:
        print(f"{f.origin}-{f.dest} {f.departure} -> {f.date_back or '-':<10} "
              f"{f.total_price:>8.2f} {f.currency}", file=sys.stderr)
    if not fares:
        print("no fares found", file=sys.stderr)


if __name__ == "__main__":
    main()
