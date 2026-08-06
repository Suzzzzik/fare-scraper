#!/usr/bin/env python3
"""Currency conversion, so prices from different markets can be added up.

Carriers price per departure market, not per search: a Wizz Air search for
WAW-MAD comes back in PLN while MAD-GDN on the very same day comes back in
EUR. Anything that sums two legs (a return pairing, a two-ticket combo) or
compares two rows (the cheapest-per-route dedupe, the relative price cap)
is therefore wrong unless the amounts are first put in one currency - the
bug this module exists to fix was a 179 PLN outbound plus a 29.99 EUR
return being reported as "208.99 PLN".

Rates come from frankfurter.app (European Central Bank reference rates, no
API key, no account). They are fetched at most once per day per process and
cached; if the fetch fails, conversion refuses rather than guessing, and
callers fall back to reporting the legs unconverted.

ECB rates are reference rates, not the card rate the airline will actually
bill - treat converted totals as "close enough to compare", not as an exact
quote.
"""

from __future__ import annotations

import threading
from datetime import date

# curl_cffi rather than urllib: this Python's urllib has no usable CA bundle
# here ("CERTIFICATE_VERIFY_FAILED"), and curl_cffi is already a dependency
# of every scraper in this project.
from curl_cffi import requests as cr

API = "https://api.frankfurter.app/latest"
TIMEOUT = 10


class Rates:
    """EUR-based rate table, refreshed at most once a day."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rates: dict[str, float] | None = None
        self._day: date | None = None

    def _load(self) -> dict[str, float] | None:
        today = date.today()
        with self._lock:
            if self._rates is not None and self._day == today:
                return self._rates
            try:
                r = cr.get(f"{API}?base=EUR", timeout=TIMEOUT,
                           impersonate="chrome")
                r.raise_for_status()
                data = r.json()
                rates = {k.upper(): float(v) for k, v in data["rates"].items()}
                rates["EUR"] = 1.0
                self._rates, self._day = rates, today
            except Exception:  # noqa: BLE001 - offline/blocked: caller decides
                # keep any previous (stale) table rather than losing conversion
                # entirely for the rest of the process
                pass
            return self._rates

    def convert(self, amount: float, frm: str, to: str) -> float | None:
        """`amount` in `frm` expressed in `to`, or None if not convertible."""
        frm, to = (frm or "").upper(), (to or "").upper()
        if not frm or not to:
            return None
        if frm == to:
            return float(amount)
        rates = self._load()
        if not rates or frm not in rates or to not in rates:
            return None
        return float(amount) / rates[frm] * rates[to]

    def supported(self, code: str) -> bool:
        rates = self._load()
        return bool(rates) and (code or "").upper() in rates


RATES = Rates()


def convert(amount: float, frm: str, to: str) -> float | None:
    return RATES.convert(amount, frm, to)


def total(legs: list[tuple[float, str]], to: str) -> float | None:
    """Sum legs given as (amount, currency), all expressed in `to`.

    None if any leg cannot be converted - a partial sum would silently
    understate the trip, which is exactly the bug this guards against.
    """
    out = 0.0
    for amount, cur in legs:
        got = convert(amount, cur, to)
        if got is None:
            return None
        out += got
    return out


if __name__ == "__main__":
    print("EUR->PLN 29.99 =", convert(29.99, "EUR", "PLN"))
    print("PLN->PLN 179   =", convert(179, "PLN", "PLN"))
    print("total          =", total([(179, "PLN"), (29.99, "EUR")], "PLN"))
