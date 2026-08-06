#!/usr/bin/env python3
"""China Airlines fare scraper.

Same digital-retailing backend shape as lufthansa.py (Amadeus/Travelport
"one-booking" platform, just a different tenant: api-des.china-airlines.com
instead of api.shop.lufthansa.com), but fronted by a heavier, three-vendor
bot-protection stack - Akamai (`ak_bmsc`/`bm_sv`/`bm_sz`), Imperva Incapsula
(`incap_ses_*`, the `reese84` token), and DataDome (`datadome`) all at once,
versus Lufthansa's single Cloudflare Managed Challenge.

Two things that work for Lufthansa do NOT work here:

  - Replaying a minted cf_clearance-equivalent via curl_cffi: same problem as
    Lufthansa (TLS/HTTP2 fingerprint mismatch), not re-tested since patchright
    already solves it.
  - Auto-submitting a hidden HTML form via `form.submit()` (Lufthansa's
    trick): China Airlines' own POST to
    bookingportal.china-airlines.com/eRetailPortal/Booking.svc/Booking/Search
    comes back "Access Denied" when triggered that way, even after first
    calling the flightSearchResults endpoint that's supposed to precede it.
    Imperva's `reese84` token scores real behavioural signals (trusted click
    events), and a JS-triggered, non-user-initiated form submit doesn't pass.

What does work: drive the real search widget with genuine Playwright input
(`page.mouse.click` / `.fill()`, which dispatch trusted events), then click
the real "Search Flights" button. Two fragile bits, solved by testing:

  - The date range picker's day cells only respond to trusted mouse clicks
    at their real screen coordinates, AND a plain JS `.click()` on the cell
    updates the DOM but never fires React's handlers (the "Confirm" button
    stays disabled forever). Clicking cells by increasingly specific
    coordinates got fragile fast, so this module skips the calendar UI
    entirely: the date field is a plain text input
    (placeholder `YYYY/MM/DD - YYYY/MM/DD`) that accepts `.fill()` directly
    and updates the same React state - confirmed by checking the resulting
    flightSearchResults request body matches the filled dates exactly.
  - Search dates within a few days of "today" (the widget's own default)
    trigger an extra "close to departure" confirmation gate; searching 2+
    weeks out avoids it, so this module doesn't bother handling that gate.

Like lufthansa.py, one browser tab is kept alive for the whole process and
every search is a real, in-tab interaction - the fare data itself is
captured off the wire via a `page.on("response")` listener watching for
`POST .../v2/search/air-calendars`, the same endpoint shape Lufthansa uses.

Requires `patchright` (`pip install patchright && patchright install chrome`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import re
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

API_CALENDARS_PATH = "/v2/search/air-calendars"
WWW = "https://www.china-airlines.com/us/en"
AIRLINE = "China Airlines"


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


class ChinaAirlines:
    def __init__(self, currency: str = "USD"):
        self.currency = currency
        # See the matching comment in lufthansa.py's Lufthansa.__init__:
        # patchright is thread-affined, and server.py dispatches each
        # carrier on a fresh thread per search, so the browser needs a
        # dedicated worker thread of its own rather than just a lock.
        self._task_q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._page = None
        self._from_shown = ""
        self._from_code = ""

    # ---------- worker thread (owns every Playwright object) --------------

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._worker_loop, daemon=True)
                self._worker.start()

    def _worker_loop(self) -> None:
        # see the matching comment in lufthansa.py's Lufthansa._worker_loop
        asyncio.set_event_loop(asyncio.new_event_loop())
        while True:
            item = self._task_q.get()
            if item is None:
                break
            fn, fut = item
            try:
                fut.set_result(fn())
            except Exception as e:  # noqa: BLE001
                fut.set_exception(e)

    def _call(self, fn):
        self._ensure_worker()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._task_q.put((fn, fut))
        return fut.result()

    # ---------- browser lifecycle (patchright, real Chrome, one tab) ------
    # everything below this point only ever runs on the worker thread

    def _ensure_page(self):
        if self._page is not None:
            return
        from patchright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch_persistent_context(
            user_data_dir=f"/tmp/ci_patchright_{uuid.uuid4().hex[:8]}",
            channel="chrome", headless=False,
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._browser.new_page()
        self._page.goto(WWW, wait_until="domcontentloaded", timeout=20000)
        self._page.wait_for_timeout(1500)
        try:
            self._page.get_by_role("button", name=re.compile("Accept", re.I)).click(timeout=4000)
        except Exception:  # noqa: BLE001 - banner may not appear on a warm profile
            pass
        self._page.wait_for_timeout(1200)
        self._from_shown = "Los Angeles (Los Angeles)"  # the widget's own default label
        self._from_code = "LAX"

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

    # ---------- one search = one real UI-driven interaction ----------------

    def _pick_airport(self, label_text: str, query: str, code: str) -> None:
        page = self._page
        page.get_by_text(label_text, exact=False).first.click()
        page.wait_for_timeout(400)
        page.keyboard.press("Control+A")
        page.keyboard.type(query, delay=60)
        page.wait_for_timeout(1200)
        page.locator('li, [role="option"]').filter(has_text=code).first.click()
        page.wait_for_timeout(500)

    def _search_on_worker(self, origin: str, dest: str, depart: date,
                          back: date | None = None) -> list[dict]:
        """Fills the real search widget and returns every air-calendars
        response the resulting page load triggered."""
        self._ensure_page()
        page = self._page
        captured: list[str] = []

        def on_response(resp):
            if resp.request.method == "POST" and API_CALENDARS_PATH in resp.url:
                try:
                    captured.append(resp.text())
                except Exception:  # noqa: BLE001 - response body can race a nav
                    pass

        page.on("response", on_response)
        try:
            if page.url != WWW:
                page.goto(WWW, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1200)
                try:
                    page.get_by_role("button", name=re.compile("Accept", re.I)).click(timeout=2000)
                except Exception:  # noqa: BLE001
                    pass
                self._from_shown = "Los Angeles (Los Angeles)"
                self._from_code = "LAX"
            if origin.upper() != self._from_code:
                self._pick_airport(self._from_shown, origin, origin)
                self._from_shown = origin
                self._from_code = origin.upper()
            page.get_by_text("To", exact=True).first.click()
            page.wait_for_timeout(400)
            page.keyboard.type(dest, delay=60)
            page.wait_for_timeout(1200)
            page.locator('li, [role="option"]').filter(has_text=dest).first.click()
            page.wait_for_timeout(500)

            date_input = page.locator('input[placeholder*="YYYY/MM/DD"]')
            date_input.click(force=True, timeout=8000)
            page.wait_for_timeout(400)
            popup = page.locator('div:has-text("Select travel dates")').last
            if back is not None:
                # Round Trip is the widget's own default - no tab click needed,
                # and clicking the already-active tab resets the field instead.
                date_str = f"{depart.strftime('%Y/%m/%d')} - {back.strftime('%Y/%m/%d')}"
            else:
                popup.get_by_text("One Way", exact=True).first.click()
                page.wait_for_timeout(300)
                date_input.click(force=True, timeout=8000)
                page.wait_for_timeout(300)
                date_str = depart.strftime("%Y/%m/%d")
            date_input.fill(date_str)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)

            page.get_by_text("Search Flights").click()
            page.wait_for_load_state("load", timeout=20000)
            try:
                page.get_by_role("button", name="Next").click(timeout=4000)
            except Exception:  # noqa: BLE001 - only appears for near-term dates
                pass
            # the trip sometimes routes through Akamai's own interstitial
            # "processing your request" page before the real results page
            for _ in range(6):
                page.wait_for_timeout(5000)
                if captured or "des-portal.china-airlines.com" in page.url:
                    break
        finally:
            page.remove_listener("response", on_response)

        out = []
        for text in captured:
            try:
                out += json.loads(text).get("data") or []
            except Exception:  # noqa: BLE001
                continue
        return out

    def _search(self, origin: str, dest: str, depart: date,
               back: date | None = None) -> list[dict]:
        return self._call(lambda: self._search_on_worker(origin, dest, depart, back))

    # ---------- fares -------------------------------------------------

    def booking_link(self) -> str:
        return WWW

    def _price_of(self, entry: dict) -> tuple[float, str] | None:
        total = ((entry.get("prices") or {}).get("totalPrices") or [{}])[0]
        price = total.get("total")
        if price is None:
            return None
        return price / 100.0, total.get("currencyCode", self.currency)

    def one_way(self, origin: str, dest: str, date_from: date, date_to: date,
               max_price: float | None = None) -> list[Fare]:
        out: dict[str, Fare] = {}
        for entry in self._search(origin, dest, date_from):
            day = entry.get("departureDate")
            if not day or not (date_from.isoformat() <= day <= date_to.isoformat()):
                continue
            priced = self._price_of(entry)
            if priced is None:
                continue
            price, currency = priced
            if max_price is not None and price > max_price:
                continue
            if day not in out or price < out[day].price:
                out[day] = Fare(origin=origin, dest=dest, departure=day, price=price,
                                currency=currency, total_price=price,
                                link=self.booking_link())
        return list(out.values())

    def round_trip(self, origin: str, dest: str, date_from: date, date_to: date,
                   nights: int, nights_tol: int,
                   max_price: float | None = None) -> list[Fare]:
        out: dict[str, Fare] = {}
        lo, hi = max(0, nights - nights_tol), nights + nights_tol
        for offset in sorted({nights - nights_tol, nights, nights + nights_tol}):
            if offset < 0:
                continue
            back = date_from + timedelta(days=offset)
            for entry in self._search(origin, dest, date_from, back):
                day = entry.get("departureDate")
                ret = entry.get("returnDate")
                if not day or not ret:
                    continue
                if not (date_from.isoformat() <= day <= date_to.isoformat()):
                    continue
                actual_nights = (date.fromisoformat(ret) - date.fromisoformat(day)).days
                if not (lo <= actual_nights <= hi):
                    continue
                priced = self._price_of(entry)
                if priced is None:
                    continue
                price, currency = priced
                if max_price is not None and price > max_price:
                    continue
                if day not in out or price < out[day].price:
                    out[day] = Fare(
                        origin=origin, dest=dest, departure=day, price=price,
                        currency=currency, total_price=price, date_back=ret,
                        nights=actual_nights, link=self.booking_link())
        return list(out.values())


# ---------- CLI (smoke test) -------------------------------------------

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="China Airlines fare scraper smoke test")
    ap.add_argument("--from", dest="origin", default="LAX")
    ap.add_argument("--to", dest="dest", default="TPE")
    ap.add_argument("--date-from", default=(date.today() + timedelta(days=30)).isoformat())
    ap.add_argument("--date-to", default=(date.today() + timedelta(days=33)).isoformat())
    ap.add_argument("--round-trip", action="store_true")
    ap.add_argument("--nights", type=int, default=7)
    ap.add_argument("--nights-tol", type=int, default=1)
    args = ap.parse_args()

    api = ChinaAirlines()
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
              f"{f.total_price:>10.2f} {f.currency}", file=sys.stderr)
    if not fares:
        print("no fares found", file=sys.stderr)


if __name__ == "__main__":
    main()
