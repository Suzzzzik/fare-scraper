#!/usr/bin/env python3
"""Stays with real prices near a destination airport - Airbnb, Booking, Google.

Three sources, one filter model, prices for the whole stay:

  Airbnb   - the search page ships results as JSON inside
             <script id="data-deferred-state-0">, so one curl_cffi request is
             enough. Radius is exact: the airport coordinates plus the requested
             km become a map bounding box (ne_lat/ne_lng/sw_lat/sw_lng), and
             because a box is square every hit is re-checked against the true
             haversine distance.
  Booking  - answers a scripted client with 202 + an AWS WAF challenge.
             Playwright opens booking.com once with the system Chrome, the
             browser solves it, and the resulting aws-waf-token cookie is reused
             for curl_cffi fetches, which come back server-rendered with prices.
             Booking ignores `offset`, so depth comes from walking price bands
             (`nflt=price=CUR-min-max-1`) with `order=price` - each band returns
             its own cheapest 25, which is how the genuinely cheap listings get
             found instead of only the first relevance page.
  Google   - google.com/travel/search, via Playwright (consent wall has to be
             rejected first). No URL-addressable filters, so its results are
             filtered on this side by price only.

FILTERS below is the shared vocabulary. Each entry says how a source expresses
that filter, or None when it cannot. A source that cannot express every selected
filter is skipped rather than silently returning unfiltered results - so ticking
something only Airbnb supports gives Airbnb-only results, by design.

  pip install playwright        # optional, uses system Chrome, no download

Usage:
  python stays.py --airport BCN --checkin 2026-09-10 --checkout 2026-09-17 \
      --radius-km 20 --bedrooms 1 --adults 1 --filters pool --max-night 250
  python stays.py --airport PMI --checkin 2026-09-08 --checkout 2026-09-15 \
      --sources airbnb,google --limit 30
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import math
import re
import sys
import threading
from datetime import date
from urllib.parse import quote, urlencode

from curl_cffi import requests as cr

KIWI_LOCATIONS = "https://api.skypicker.com/locations"
SOURCES = ("airbnb", "booking", "google")

# Shared filter vocabulary. Booking ids were read off its live filter panel;
# the Airbnb amenity ids are the ones verified to actually change results.
FILTERS: dict[str, dict] = {
    "pool":         {"label": "Basen",                "booking": "hotelfacility=433", "airbnb": "amenities%5B%5D=7",  "google": None},
    "private_pool": {"label": "Basen prywatny",       "booking": "roomfacility=93",   "airbnb": None,                 "google": None},
    "hot_tub":      {"label": "Jacuzzi / spa",        "booking": "hotelfacility=54",  "airbnb": "amenities%5B%5D=25", "google": None},
    "parking":      {"label": "Parking",              "booking": "hotelfacility=2",   "airbnb": None,                 "google": None},
    "gym":          {"label": "Siłownia",             "booking": "hotelfacility=11",  "airbnb": None,                 "google": None},
    "wifi":         {"label": "Wi-Fi",                "booking": "hotelfacility=107", "airbnb": None,                 "google": None},
    "aircon":       {"label": "Klimatyzacja",         "booking": "roomfacility=11",   "airbnb": None,                 "google": None},
    "balcony":      {"label": "Balkon",               "booking": "roomfacility=17",   "airbnb": None,                 "google": None},
    "sea_view":     {"label": "Widok na morze",       "booking": "roomfacility=108",  "airbnb": None,                 "google": None},
    "breakfast":    {"label": "Śniadanie",            "booking": "mealplan=1",        "airbnb": None,                 "google": None},
    "free_cancel":  {"label": "Bezpłatne odwołanie",  "booking": "fc=2",              "airbnb": None,                 "google": None},
    "entire":       {"label": "Całe mieszkanie",      "booking": "privacy_type=3",
                     "airbnb": "room_types%5B%5D=" + quote("Entire home/apt", safe=""), "google": None},
    "rating8":      {"label": "Ocena 8+ / 4,5+",      "booking": "review_score=80",   "airbnb": None,                 "google": None},
}

# soft preferences: applied where a source understands them, never a reason to
# drop a source
BOOKING_TYPE = {"hotel": "ht_id=204", "apartment": "ht_id=201",
                "hostel": "ht_id=235", "house": "ht_id=220"}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def bounding_box(lat: float, lon: float, radius_km: float) -> dict:
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return {"sw_lat": lat - dlat, "sw_lng": lon - dlon,
            "ne_lat": lat + dlat, "ne_lng": lon + dlon}


def parse_amount(text: str) -> float | None:
    """'zł 8,676 total' / '1 234 zł' / 'PLN 409' -> 8676.0 / 1234.0 / 409.0"""
    if not text:
        return None
    best = None
    for chunk in re.findall(r"\d[\d  ,.]*", text.replace(" ", " ")):
        digits = chunk.replace(" ", "").replace(" ", "").replace(",", "")
        if digits.count(".") == 1 and len(digits.split(".")[1]) == 2:
            value = float(digits)
        else:
            value = float(digits.replace(".", ""))
        if best is None or value > best:
            best = value
    return best


def supported_sources(filters: list[str]) -> dict[str, list[str]]:
    """Which sources can express every one of these filters, and which cannot."""
    ok, dropped = [], {}
    for src in SOURCES:
        missing = [k for k in filters if k in FILTERS and not FILTERS[k].get(src)]
        if missing:
            dropped[src] = [FILTERS[k]["label"] for k in missing]
        else:
            ok.append(src)
    return {"ok": ok, "dropped": dropped}


def price_bands(pmin: float, pmax: float, count: int) -> list[tuple[float, float]]:
    """Split a nightly price range into bands, tighter at the cheap end where
    listings cluster."""
    pmin = max(0.0, pmin)
    if pmax <= pmin:
        return [(pmin, 0)]
    lo = max(pmin, 1.0)
    ratio = (pmax / lo) ** (1.0 / count)
    bands, edge = [], pmin
    for i in range(count):
        nxt = lo * (ratio ** (i + 1))
        bands.append((round(edge), round(nxt)))
        edge = nxt
    bands[-1] = (bands[-1][0], round(pmax))
    return bands


class Stays:
    def __init__(self, timeout: int = 60, currency: str = "PLN"):
        self.timeout = timeout
        self.currency = currency
        self._lock = threading.Lock()
        self._airports: dict[str, dict] = {}
        self._bk_cookies: dict | None = None
        self._kiwi = cr.Session(impersonate="chrome")
        self._kiwi.headers.update({"Accept": "application/json",
                                   "Origin": "https://www.kiwi.com",
                                   "Referer": "https://www.kiwi.com/"})

    # ---------- where is the airport ----------

    def airport(self, iata: str) -> dict:
        iata = iata.upper()
        with self._lock:
            hit = self._airports.get(iata)
        if hit:
            return hit
        r = self._kiwi.get(KIWI_LOCATIONS,
                           params={"term": iata, "locale": "en-US",
                                   "location_types": "airport", "limit": "5",
                                   "active_only": "true"},
                           timeout=self.timeout)
        r.raise_for_status()
        for loc in r.json().get("locations", []):
            if loc.get("code", "").upper() != iata:
                continue
            city = loc.get("city") or {}
            out = {"code": iata, "name": loc.get("name", iata),
                   "city": city.get("name", ""),
                   "country": (city.get("country") or {}).get("name", ""),
                   "lat": (loc.get("location") or {}).get("lat"),
                   "lon": (loc.get("location") or {}).get("lon")}
            with self._lock:
                self._airports[iata] = out
            return out
        raise RuntimeError(f"unknown airport {iata}")

    # ---------- Airbnb ----------

    def airbnb_url(self, city: str, country: str, checkin: str, checkout: str,
                   adults: int = 2, bedrooms: int | None = None,
                   filters: list[str] | None = None,
                   price_min: float | None = None,
                   price_max: float | None = None,
                   box: dict | None = None) -> str:
        where = quote(f"{city}--{country}".replace(" ", "-")) if country \
            else quote(city.replace(" ", "-"))
        parts = [f"checkin={checkin}", f"checkout={checkout}",
                 f"adults={adults}", f"currency={self.currency}"]
        if bedrooms:
            parts.append(f"min_bedrooms={bedrooms}")
        for key in filters or []:
            expr = FILTERS.get(key, {}).get("airbnb")
            if expr:
                parts.append(expr)
        if price_min:
            parts.append(f"price_min={int(price_min)}")
        if price_max:
            parts.append(f"price_max={int(price_max)}")
        if box:
            parts.append("search_by_map=true")
            parts += [f"{k}={v:.5f}" for k, v in box.items()]
        return f"https://www.airbnb.com/s/{where}/homes?" + "&".join(parts)

    def airbnb_search(self, url: str, origin: tuple[float, float] | None = None,
                      limit: int = 24,
                      radius_km: float | None = None) -> list[dict]:
        r = cr.get(url, impersonate="chrome", timeout=self.timeout,
                   headers={"Accept-Language": "en-US,en;q=0.9"})
        r.raise_for_status()
        m = re.search(r'id="data-deferred-state-0"[^>]*>(.*?)</script>',
                      r.text, re.S)
        if not m:
            return []
        blob = json.loads(m.group(1))

        results: list[dict] = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("__typename") == "StaySearchResult":
                    results.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(blob)

        out, seen = [], set()
        for item in results:
            # the blob carries each result twice, once for the list and once for
            # the map layer
            listing = item.get("demandStayListing") or {}
            coord = ((listing.get("location") or {}).get("coordinate") or {})
            label = (((item.get("structuredDisplayPrice") or {})
                      .get("primaryLine") or {}).get("accessibilityLabel") or "")
            lat, lon = coord.get("latitude"), coord.get("longitude")
            distance = (round(haversine_km(origin[0], origin[1], lat, lon), 1)
                        if origin and lat and lon else None)
            if radius_km and distance is not None and distance > radius_km:
                continue
            listing_id = None
            if listing.get("id"):
                try:
                    import base64
                    listing_id = base64.b64decode(
                        listing["id"]).decode().split(":")[-1]
                except Exception:  # noqa: BLE001 - id shape changed, link degrades
                    listing_id = None
            name = ((listing.get("description") or {}).get("name") or {}) \
                .get("localizedStringWithTranslationPreference")
            key = listing_id or f"{name}|{lat}|{lon}"
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source": "airbnb",
                "name": name or item.get("title"),
                "kind": item.get("title", ""),
                "price_total": parse_amount(label),
                "price_label": label,
                "currency": self.currency,
                "rating": item.get("avgRatingLocalized"),
                "lat": lat, "lon": lon, "distance_km": distance,
                "link": (f"https://www.airbnb.com/rooms/{listing_id}"
                         if listing_id else url),
            })
            if len(out) >= limit:
                break
        return out

    # ---------- Booking.com ----------

    def _booking_cookies(self, force: bool = False) -> dict:
        with self._lock:
            if not force and self._bk_cookies:
                return self._bk_cookies
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Booking needs a browser for its AWS WAF challenge - "
                "run `pip install playwright` (uses your system Chrome)") from e

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(locale="en-GB")
            page = ctx.new_page()
            page.goto("https://www.booking.com/", wait_until="domcontentloaded",
                      timeout=self.timeout * 1000)
            page.wait_for_timeout(5000)
            cookies = {c["name"]: c["value"]
                       for c in ctx.cookies("https://www.booking.com")}
            browser.close()
        if "aws-waf-token" not in cookies:
            raise RuntimeError("booking.com did not hand out a WAF token")
        with self._lock:
            self._bk_cookies = cookies
        return cookies

    def booking_url(self, city: str, checkin: str, checkout: str,
                    adults: int = 2, rooms: int = 1,
                    bedrooms: int | None = None, prop_type: str = "any",
                    filters: list[str] | None = None,
                    price_min: float | None = None,
                    price_max: float | None = None,
                    lat: float | None = None, lon: float | None = None) -> str:
        nflt = []
        if prop_type in BOOKING_TYPE:
            nflt.append(BOOKING_TYPE[prop_type])
        for key in filters or []:
            expr = FILTERS.get(key, {}).get("booking")
            if expr:
                nflt.append(expr)
        if bedrooms:
            nflt.append(f"entire_place_bedroom_count={bedrooms}")
        if price_min or price_max:
            lo = int(price_min) if price_min else "min"
            hi = int(price_max) if price_max else "max"
            nflt.append(f"price={self.currency}-{lo}-{hi}-1")

        params = {"ss": city, "checkin": checkin, "checkout": checkout,
                  "group_adults": adults, "no_rooms": rooms,
                  "group_children": 0, "selected_currency": self.currency,
                  "order": "price"}
        if lat is not None and lon is not None:
            params.update({"latitude": lat, "longitude": lon})
        url = "https://www.booking.com/searchresults.html?" + urlencode(params)
        if nflt:
            url += "&nflt=" + quote(";".join(nflt), safe="")
        return url

    @staticmethod
    def _card_field(card: str, tid: str, block: int = 700) -> str:
        m = re.search(r'data-testid="%s"[^>]*>(.{0,%d})'
                      % (re.escape(tid), block), card, re.S)
        if not m:
            return ""
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        # the fixed-size window above can end mid-tag (e.g. `<h4 role="link"
        # tab`, no closing '>' within the slice), which `<[^>]+>` can't match
        # since it requires the closing bracket - that leftover fragment would
        # otherwise reach the browser as live, unclosed markup. Strip any
        # surviving angle bracket outright; this is text, not real HTML.
        text = text.replace("<", " ").replace(">", " ")
        return html_mod.unescape(re.sub(r"\s+", " ", text)).strip()

    @staticmethod
    def _card_text(card: str, tid: str) -> str:
        """The element's immediate text, nothing past its first child tag.

        Booking's cards routinely follow the real content with a decorative
        icon (`<svg>...<path d="...">`) as a sibling, still inside the same
        testid element. _card_field's fixed-size window can end mid-attribute
        of that icon (no closing '>' inside the slice), leaking raw markup
        text like `svg xmlns="..."` into the field. Stopping at the first '<'
        by construction makes that leak impossible - correct for every field
        here whose real value sits before any nested tag (price, distance,
        address, title). Fields with the value nested a level deeper
        (review-score, recommended-units) need their own extraction instead.
        """
        m = re.search(r'data-testid="%s"[^>]*>([^<]*)' % re.escape(tid), card)
        if not m:
            return ""
        return html_mod.unescape(re.sub(r"\s+", " ", m.group(1))).strip()

    @staticmethod
    def _card_room_kind(card: str) -> str:
        """Room type name, e.g. "Bed in 16-Bed Mixed Dormitory Room".

        The room name sits inside an <h4> nested three divs below the
        recommended-units testid - a fixed character window (what
        _card_field uses for every other field) lands mid-attribute
        (`<h4 role="link" tab`) long before reaching it. Go straight for the
        <h4>...</h4> pair instead of counting characters.
        """
        m = re.search(r'data-testid="recommended-units".{0,400}?<h4[^>]*>(.*?)</h4>',
                      card, re.S)
        if not m:
            return ""
        text = re.sub(r"<[^>]+>", " ", m.group(1)).replace("<", " ").replace(">", " ")
        return html_mod.unescape(re.sub(r"\s+", " ", text)).strip()

    def _booking_fetch(self, url: str) -> str:
        for attempt in (0, 1):
            cookies = self._booking_cookies(force=bool(attempt))
            s = cr.Session(impersonate="chrome")
            for k, v in cookies.items():
                s.cookies.set(k, v, domain=".booking.com")
            r = s.get(url, headers={"Accept-Language": "en-GB,en;q=0.9"},
                      timeout=self.timeout)
            if r.status_code == 200 and "property-card" in r.text:
                return r.text
            with self._lock:
                self._bk_cookies = None
        raise RuntimeError("booking.com kept returning the WAF challenge")

    def _booking_parse(self, html: str, limit: int) -> list[dict]:
        starts = [m.start() for m in
                  re.finditer(r'data-testid="property-card"', html)]
        out = []
        for i, start in enumerate(starts[:limit]):
            end = starts[i + 1] if i + 1 < len(starts) else start + 24000
            card = html[start:end]
            name = self._card_text(card, "title").strip()
            if not name:
                continue
            price_txt = self._card_text(card, "price-and-discounted-price")
            rating_txt = self._card_field(card, "review-score", 120)
            score = re.search(r"Scored\s+([\d.,]+)", rating_txt)
            dist = re.search(r"([\d.,]+)\s*km from",
                             self._card_text(card, "distance"))
            link = re.search(r'href="(https://www\.booking\.com/hotel/[^"?]+)',
                             card)
            out.append({
                "source": "booking",
                "name": name,
                "kind": self._card_room_kind(card)[:60],
                "price_total": parse_amount(price_txt),
                "price_label": price_txt[:40],
                "currency": self.currency,
                "rating": score.group(1) if score else None,
                "address": self._card_text(card, "address-link").strip() or None,
                "distance_center_km": (float(dist.group(1).replace(",", "."))
                                       if dist else None),
                "lat": None, "lon": None, "distance_km": None,
                "link": (link.group(1).replace("&amp;", "&") if link else None),
            })
        return out

    def booking_search(self, make_url, limit: int = 24,
                       price_min: float | None = None,
                       price_max: float | None = None,
                       bands: int = 4) -> list[dict]:
        """Booking ignores `offset`, so depth comes from price bands: each band
        is its own price-sorted query returning its own cheapest results."""
        ranges: list[tuple[float | None, float | None]]
        if bands > 1:
            ranges = [(lo or None, hi or None) for lo, hi in
                      price_bands(price_min or 0,
                                  price_max or 2000, bands)]
        else:
            ranges = [(price_min, price_max)]

        seen: dict[str, dict] = {}
        errors = []
        for lo, hi in ranges:
            try:
                html = self._booking_fetch(make_url(lo, hi))
            except Exception as e:  # noqa: BLE001 - one band failing is survivable
                errors.append(str(e)[:120])
                continue
            for row in self._booking_parse(html, limit):
                key = row["link"] or row["name"]
                if key not in seen:
                    seen[key] = row
        if not seen and errors:
            raise RuntimeError(errors[0])
        return list(seen.values())

    # ---------- Google ----------

    def google_url(self, city: str, checkin: str, checkout: str,
                   adults: int = 2) -> str:
        q = quote(f"hotels near {city}")
        return (f"https://www.google.com/travel/search?q={q}"
                f"&hl=en&gl=us&currency={self.currency}"
                f"&checkin={checkin}&checkout={checkout}&adults={adults}")

    def google_search(self, url: str, limit: int = 24) -> list[dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError("Google needs `pip install playwright`") from e

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context(locale="en-US",
                                      viewport={"width": 1400, "height": 950})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=self.timeout * 1000)
                page.wait_for_timeout(2500)
                for label in ("Reject all", "Odrzuć wszystko"):
                    try:
                        page.get_by_role("button", name=label).first.click(
                            timeout=3500)
                        break
                    except Exception:  # noqa: BLE001 - consent wall may be absent
                        pass
                page.wait_for_timeout(5000)
                for _ in range(3):
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(800)
                cards = page.evaluate("""() => [...document.querySelectorAll('.MY0II')]
                    .map(el => {
                      const a = el.closest('a') || el.querySelector('a');
                      return { text: el.innerText, href: a ? a.href : null };
                    })""")
            finally:
                browser.close()

        out, seen = [], set()
        for c in cards:
            lines = [l.strip() for l in (c.get("text") or "").split("\n")
                     if l.strip()]
            if len(lines) < 2:
                continue
            name = lines[0]
            if name in seen:
                continue
            seen.add(name)
            price = next((parse_amount(l) for l in lines
                          if re.search(r"PLN|zł|€|\$|£", l)), None)
            rating = next((l.split("/")[0] for l in lines
                           if re.match(r"^\d[.,]\d/5$", l)), None)
            provider = next((l for l in lines
                             if l in ("Booking.com", "Expedia", "Agoda",
                                      "Hotels.com", "Trip.com")), None)
            out.append({
                "source": "google",
                "name": name,
                "kind": " · ".join(l for l in lines[1:]
                                   if re.search(r"star|hotel|apartment", l, re.I))[:60],
                "price_total": None,
                "price_per_night": price,
                "price_label": f"{price:.0f} {self.currency}/noc" if price else "",
                "currency": self.currency,
                "rating": rating,
                "provider": provider,
                "lat": None, "lon": None, "distance_km": None,
                "link": c.get("href") or url,
            })
            if len(out) >= limit:
                break
        return out

    # ---------- one call for the UI ----------

    def for_flight(self, dest_iata: str, checkin: str, checkout: str,
                   radius_km: float = 30, adults: int = 2,
                   bedrooms: int | None = None, prop_type: str = "any",
                   filters: list[str] | None = None,
                   price_min: float | None = None,
                   price_max: float | None = None,
                   limit: int = 24,
                   sources: tuple[str, ...] = SOURCES,
                   bands: int = 4) -> dict:
        filters = [f for f in (filters or []) if f in FILTERS]
        ap = self.airport(dest_iata)
        box = bounding_box(ap["lat"], ap["lon"], radius_km)
        city = ap["city"] or ap["name"]
        nights = max(1, (date.fromisoformat(checkout)
                         - date.fromisoformat(checkin)).days)

        support = supported_sources(filters)
        chosen = [s for s in sources if s in support["ok"]]
        skipped = {s: support["dropped"][s] for s in sources
                   if s in support["dropped"]}

        airbnb_url = self.airbnb_url(city, ap["country"], checkin, checkout,
                                     adults, bedrooms, filters, price_min,
                                     price_max, box)

        def booking_url_for(lo, hi):
            return self.booking_url(city, checkin, checkout, adults,
                                    bedrooms=bedrooms, prop_type=prop_type,
                                    filters=filters, price_min=lo,
                                    price_max=hi, lat=ap["lat"], lon=ap["lon"])

        booking_url = booking_url_for(price_min, price_max)
        google_url = self.google_url(city, checkin, checkout, adults)

        listings: list[dict] = []
        errors: dict[str, str] = {}
        if "airbnb" in chosen:
            try:
                listings += self.airbnb_search(airbnb_url,
                                               (ap["lat"], ap["lon"]), limit,
                                               radius_km)
            except Exception as e:  # noqa: BLE001 - one source down is not fatal
                errors["airbnb"] = str(e)[:200]
        if "booking" in chosen:
            try:
                listings += self.booking_search(booking_url_for, limit,
                                                price_min, price_max, bands)
            except Exception as e:  # noqa: BLE001
                errors["booking"] = str(e)[:200]
        if "google" in chosen:
            try:
                listings += self.google_search(google_url, limit)
            except Exception as e:  # noqa: BLE001
                errors["google"] = str(e)[:200]

        # normalise both price views, then apply the nightly range everywhere -
        # Google has no server-side price filter at all
        kept = []
        for s in listings:
            per_night = s.get("price_per_night")
            if per_night is None and s.get("price_total") is not None:
                per_night = round(s["price_total"] / nights, 2)
            if s.get("price_total") is None and per_night is not None:
                s["price_total"] = round(per_night * nights, 2)
            s["price_per_night"] = per_night
            # Airbnb and Booking filter on price server-side, and their notion of
            # a nightly rate excludes fees that the displayed total includes -
            # re-filtering here would throw away rows they already accepted.
            # Google has no price filter at all, so it gets filtered here.
            if s["source"] == "google" and per_night is not None:
                if price_min and per_night < price_min:
                    continue
                if price_max and per_night > price_max:
                    continue
            # Booking reports distance from the city centre rather than the
            # airport, so the radius is applied against that as an approximation
            if (s.get("distance_center_km") is not None
                    and s["distance_center_km"] > radius_km):
                continue
            kept.append(s)

        kept.sort(key=lambda x: (x["price_total"] is None,
                                 x["price_total"] or 0))
        return {
            "airport": ap, "radius_km": radius_km, "nights": nights,
            "checkin": checkin, "checkout": checkout,
            "filters": filters, "sources_used": chosen,
            "sources_skipped": skipped,
            "listings": kept, "errors": errors,
            "links": {"booking": booking_url, "airbnb": airbnb_url,
                      "google": google_url},
        }


def main() -> None:
    p = argparse.ArgumentParser(description="Stays with prices near an airport")
    p.add_argument("--airport", required=True)
    p.add_argument("--checkin", required=True)
    p.add_argument("--checkout", required=True)
    p.add_argument("--radius-km", type=float, default=30)
    p.add_argument("--adults", type=int, default=2)
    p.add_argument("--bedrooms", type=int)
    p.add_argument("--type", dest="prop_type", default="any",
                   choices=["any", "hotel", "apartment", "hostel", "house"])
    p.add_argument("--filters", default="",
                   help="comma separated: " + ",".join(FILTERS))
    p.add_argument("--min-night", type=float, help="min price per night")
    p.add_argument("--max-night", type=float, help="max price per night")
    p.add_argument("--bands", type=int, default=4,
                   help="Booking price bands to walk (depth)")
    p.add_argument("--limit", type=int, default=24)
    p.add_argument("--currency", default="PLN")
    p.add_argument("--sources", default="airbnb,booking,google")
    p.add_argument("--format", choices=["text", "json"], default="text")
    args = p.parse_args()

    date.fromisoformat(args.checkin), date.fromisoformat(args.checkout)
    sources = tuple(s for s in args.sources.split(",") if s in SOURCES)
    filters = [f for f in args.filters.split(",") if f]

    data = Stays(currency=args.currency).for_flight(
        args.airport, args.checkin, args.checkout, args.radius_km, args.adults,
        args.bedrooms, args.prop_type, filters, args.min_night, args.max_night,
        args.limit, sources, args.bands)

    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    ap = data["airport"]
    print(f"{ap['code']} {ap['name']} - {ap['city']}, {ap['country']}  "
          f"({data['checkin']} .. {data['checkout']}, {data['nights']} nights, "
          f"{data['radius_km']} km)", file=sys.stderr)
    print(f"sources: {', '.join(data['sources_used']) or 'none'}",
          file=sys.stderr)
    for src, why in data["sources_skipped"].items():
        print(f"  - {src} skipped, cannot filter: {', '.join(why)}",
              file=sys.stderr)
    for src, err in data["errors"].items():
        print(f"  ! {src}: {err}", file=sys.stderr)

    print(f"\n{'src':<8} {'total':>10} {'/night':>9}  {'rating':<7} {'km':>5}  name")
    print("-" * 88)
    for s in data["listings"]:
        total = f"{s['price_total']:.0f}" if s["price_total"] else "-"
        night = f"{s['price_per_night']:.0f}" if s.get("price_per_night") else "-"
        km = (f"{s['distance_km']:.1f}" if s.get("distance_km") is not None
              else (f"~{s['distance_center_km']:.1f}"
                    if s.get("distance_center_km") is not None else "-"))
        print(f"{s['source']:<8} {total:>10} {night:>9}  "
              f"{(s['rating'] or '-'):<7} {km:>5}  {(s['name'] or '')[:44]}")


if __name__ == "__main__":
    main()
