# Flight fare scrapers

A local search engine over several airlines' own booking backends, plus every other airline via
Kiwi.com, plus accommodation with real prices near the destination — all live-scraped, no
third-party fare API, no API key. `server.py` is the local web UI that drives all of it; each
`*.py` module is also a standalone CLI (`airports` / `scan` / …) sharing a core set of fields:
`origin, dest, departure, price, currency, total_price, link`.

| Script | Covers | How |
|---|---|---|
| `ryanair.py` | Ryanair | the airline's own fare API |
| `wizzair.py` | Wizz Air | the airline's own fare API |
| `lot.py` | LOT Polish Airlines | the airline's own booking-engine API |
| `lufthansa.py` | Lufthansa | the airline's own booking backend, behind a Cloudflare challenge - patchright drives a real Chrome tab through it |
| `china_airlines.py` | China Airlines | same backend shape as Lufthansa, behind three stacked bot-protection vendors instead of one |
| `kiwi.py` | **every remaining airline** — Eurowings, Vueling, Condor, Enter Air, Smartwings … | Kiwi.com's website GraphQL API |
| `stays.py` | hotels and apartments near the destination airport, **with prices** | Airbnb + Booking.com, scraped concurrently |
| `fx.py` | currency conversion, so a Wizz Air PLN leg and a EUR leg add up correctly | ECB reference rates |

Defaults: Poland -> Spain, PLN, `pl-pl` market — freely changeable, nothing is hardcoded.

## Which airlines can be scraped directly, and which cannot

Direct scraping was attempted for every carrier below; this is what each one actually does.

| Airline | Result |
|---|---|
| Ryanair | fare API answers plain HTTP — direct scraper |
| Wizz Air | fare API answers plain HTTP (after the token dance) — direct scraper |
| LOT | booking-engine API answers once `curl_cffi` handles the TLS fingerprint — direct scraper |
| Lufthansa | behind a Cloudflare Managed Challenge that blocks curl_cffi and even a plain Playwright Chrome tab, but `patchright` gets through — direct scraper (see below) |
| China Airlines | same backend shape as Lufthansa, three bot-protection vendors stacked instead of one — direct scraper, flakier than Lufthansa |
| easyJet | not scrapeable at all with the techniques above - routed through Kiwi as its own checkbox instead (see below) |
| Norwegian, Volotea, Eurowings, Transavia, Pegasus, Smartwings, Jet2, Aegean | homepage loads with `curl_cffi`, fare APIs stay behind Akamai/Cloudflare/AWS WAF challenges |

So five airlines are scraped at the source, and everything else goes through Kiwi.com, which
indexes them all.

### easyJet in detail

Its endpoint is real and returns clean per-day fares:
`GET /api/routepricing/v3/searchfares/GetLowestDailyFares?departureAirport=…&arrivalAirport=…&currency=…&departureDateFrom=…&departureDateTo=…`
It works from an ordinary browser. It does not work from anything automatable here:

- plain `requests` — `403` even on the HTML page (TLS fingerprint)
- `curl_cffi` with Chrome impersonation — HTML `200`, but the API answers `429 {"cpr_chlge":"true"}`
- Playwright driving the **system Chrome**, headless *and* headed — `403`

The blocker is Akamai's `_abck` cookie, which only becomes valid after the sensor script runs in a
browser that does not look automated. Rather than leave easyJet out, it gets its own checkbox in the
UI whose rows come from Kiwi filtered to `carriers: U2, EC, DS` (easyJet UK / Europe / Switzerland).
The row's *Źródło* tag says `EASYJET`, the airline column says easyJet, and the price is Kiwi's.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

---

# Web UI (`server.py`)

```bash
.venv/bin/python server.py
```

Then open <http://127.0.0.1:8000>. Flags: `--port`, `--host`, `--market` (the market drives both
the currency and the language of the names — `pl-pl` gives PLN, `es-es` gives EUR).

Nothing is pinned to Poland or Spain. The country dropdowns, the airport list and the route graphs
are all fetched at runtime — **223 countries** now that Kiwi is in the mix (Ryanair and Wizz cover
48 between them; each country is labelled with which sources reach it). Pick origin country,
destination country, dates; optionally narrow to specific origin airports, cap the price, set the
passenger count, allow connections, or switch a source off.

Each source is a checkbox, generated from `/api/carriers`. **Kiwi automatically excludes the group
codes of every directly-scraped airline that is also ticked** (`FR/RK/RR/OE/AL`, `W6/W9/W4/W8/5W`,
`LO`), so it contributes airlines the direct scrapers cannot reach instead of duplicating their
rows. The table separates *Źródło* (which scraper produced the row) from *Linia* (who actually
flies it).

- **Cheapest per route** (default) — one row per route, the best day in the window.
- **Wszystkie dni** — every priced day on every route. Much slower, far more rows.

## Return trips with a flexible stay

Switch **Typ podróży** to *Powrotny* and two fields appear: **Pobyt (nocy)** and **Tolerancja ±**.
Entering 7 nights ± 2 searches every stay from 5 to 9 nights — the outbound date range is what you
picked, and the inbound leg is allowed to depart `nights-tol` … `nights+tol` days later. The
`Noce` column shows what each row actually landed on, and the price cell shows the split
(`out + back`) under the total. Rows sort on the total, so return and one-way results are
comparable.

The two carriers get there differently:

- **Ryanair** prices return trips itself — the stay window maps straight onto
  `durationFrom`/`durationTo` on `farfnd/v4/roundTripFares`, one request per origin airport.
Wizz Air's client used to sit behind a flat 1.5s gap between requests, on the theory that
`be.wizzair.com` throttles hard under concurrency. Measured, it doesn't: 12 timetable calls
four-at-a-time finish in 1.5s with zero errors, versus ~18s serialised — and a 23-route combo
search was spending ~35s asleep in the limiter alone. It now uses a small gap plus six workers and
*reacts* to throttling instead of assuming it: a 429/503 triggers an exponential backoff and
permanently widens that run's interval, so a run that does get pushed back slows down while a run
that doesn't stays fast. Same query end to end: **110s → 42s → 14.6s**, identical results.

- **Wizz Air** has no round-trip endpoint, so `search/timetable` is asked for both legs in one call
  (the inbound leg gets its own date window) and the pairing is done here: for every outbound day,
  the cheapest return inside the stay window. One row per outbound day, not every combination —
  otherwise a 40-day window at ±2 would emit hundreds of near-identical rows per route.
  Because the inbound window is the outbound one stretched by the tolerance, and the API caps any
  window at 42 days, the outbound chunk shrinks to `42 - 2*tolerance` days to keep both legal.

`--max-price` applies to the **total**, so an expensive outbound can still pair into a trip that
fits the budget.

### Combos: two separate tickets as one trip

Ticking **Łącz 2 bilety w jedną podróż** (return trips only) adds a source that no single carrier's
own return search can express, because it books each leg independently:

- **Mixed airlines** — out on Wizz Air, back on Ryanair, if that beats either carrier's own return.
- **Open-jaw** — out of Gdansk, back into Poznan. Tick **Powrót na inne lotnisko** and pick which
  airports the return may land at (empty = any airport in the origin country).

Both fall out of the same mechanism (`SearchRun.combo_rows`): search one-way legs in each direction
independently, in per-day mode, then pair each outbound with the cheapest return that leaves from
where it landed and sits inside the stay window. Rows are tagged **2 BILETY** and carry *two*
booking links (`tam ↗` / `wróć ↗`) — the legs are separate tickets, so each is booked separately,
and a missed connection is not the airline's problem. Deduplication treats the return airport as
part of the route, so BCN→POZ and BCN→GDN on the same outbound stay as distinct rows.

Cost: it re-searches both directions per-day, so it roughly triples the request count of a plain
return search. Both directions are searched concurrently and each runs its carriers in parallel
(the return window is derived from the request, not from the outbound results, which is what lets
the two run at the same time). Query params: `combos=1`, `mixed_carriers=1`, `open_jaw=1`,
`return_dests=POZ,WAW`.

### One currency (`fx.py`)

Carriers price per **departure market**, not per search: Wizz Air quotes WAW-MAD in PLN and
MAD-GDN in EUR on the same day. Anything that adds two legs or compares two rows is wrong unless
they're first put in one currency — a 179 PLN outbound plus a 29.99 EUR return was being reported
as "208.99 PLN" when the real total is ~308 PLN.

So every row is restated in one display currency (`--currency`, default PLN) before it leaves the
server: leg sums in `wizzair_rows` and `combo_rows` convert first, and `SearchRun._to_display`
catches everything else at the same `_guard` choke point that enforces the destination filter.
Rates are ECB reference rates from `api.frankfurter.app`, fetched at most once a day per process.
If a rate isn't available the row is **dropped** rather than shown with a price that silently
means a different currency. Converted totals are for comparison, not an exact quote — the airline
bills at its own card rate.

This also fixes sorting, the cheapest-per-route dedupe and the relative price cap, all of which
compare prices across rows and were previously comparing PLN against EUR.

Results stream in as they arrive over Server-Sent Events, so Ryanair rows show up within seconds
while Wizz Air is still being polled; the final event replaces them with the deduplicated, sorted
set. Table sorts on any column, filters as you type, and exports the merged result to CSV. Every
row links straight to the carrier's own booking page for that date.

Backend is stdlib only (`http.server` + `ThreadingHTTPServer`), importing `ryanair.py` and
`wizzair.py` as libraries; frontend is one dependency-free HTML file in `static/`.

API, if you want to drive it directly:

| Endpoint | Purpose |
|---|---|
| `GET /api/countries` | merged country list with per-country carrier coverage |
| `GET /api/airports?country=pl` | airports in a country, flagged by carrier |
| `GET /api/search?from=pl&to=es&date_from=…&date_to=…&carriers=…&mode=…` | SSE stream: `progress`, `rows`, `warn`, `results`, `done` |

## Narrowing the search

The three airport pickers are chip lists, not `<select multiple>`: multi-select in a native
multi-select needs ctrl/cmd-click and gives no sign that it does, so anyone who doesn't already
know the trick can only ever pick one airport — and silently replaces it on every click. Chips
make the current selection obvious (highlighted, plus a `wybrano: GDN, KRK` summary next to the
label and a **wyczyść** button), and a plain click toggles.

**Lotniska docelowe** narrows the destination the same way **Lotniska wylotu** narrows the origin —
pick MAD and you get Madrid rather than all of Spain. Carriers that can express this natively are
told directly (Wizz Air's route map, LOT's route pairs, Kiwi's `Station:` ids, Ryanair's
per-origin destination list), which cuts the request count; the rest only take a destination
*country*, so the narrowing is also enforced in `SearchRun._guard` — the one choke point every
carrier's rows pass through.

**Dzień wylotu** restricts which weekday the outbound may leave on, with a `±` tolerance that
widens the *set of weekdays* rather than the dates. Picking Monday with ±1 accepts Sunday, Monday
and Tuesday — the week wraps, which is the whole point: someone taking five days off around a
weekend wants to leave Sunday evening *or* Monday morning, whichever is cheaper. It constrains the
outbound only; the return lands wherever the stay length puts it. Query params: `weekdays=0,4`
(0 = Monday … 6 = Sunday), `weekday_tol=1`.

**Tylko najtańsze do N%** drops anything priced above `cheapest * N`. Without it a single search can
return a 174 zł fare next to a 1400 zł one, which makes the table useless to skim. It's applied
only in the final `results` event, not while rows stream in — "what counts as an outlier" is
defined against the cheapest fare found, which isn't known until every source has reported. The
`done` event carries `dropped_over_ratio` so the UI can say how many were cut. Query params:
`dests=MAD`, `max_ratio=1.5`.

Note it caps against the cheapest fare *in the whole result set*, not per route — a search covering
both a 2-hour hop and a long-haul will cut the long-haul. Narrow by destination first if that
matters, or raise the ratio.

## Stays: real listings with prices

Every result row has a **noclegi** button. It opens a panel under the flight with the trip dates
pre-filled (check-out = the return date, or `+7` nights for a one-way) and these controls:

- **Promień (km)** — how far from the arrival airport to look, default 30
- adults, bedrooms, property type (any / apartment / hotel / hostel)
- **cena/noc od** and **do**
- 13 amenity filters (pool, private pool, jacuzzi/spa, parking, gym, wifi, air conditioning,
  balcony, sea view, breakfast, free cancellation, entire place, rating 8+)
- which sources to hit: **Airbnb** and **Booking**, both ticked by default

The two are searched **at the same time**, so a search costs whichever is slower rather than the sum
of both — measured on Barcelona/25 km: Airbnb alone 1.2s, Booking alone 30.1s, both together 30.1s.
It returns **actual listings, priced for the whole stay and per night**, merged from both and
sorted cheapest first: name, total, per-night, rating, distance, and a link straight to that
property's page with the dates in it.

### One filter vocabulary, two sources

`FILTERS` in [stays.py](stays.py) is the shared vocabulary. Every entry says how each source
expresses that filter — or `None` when it cannot:

| filter | Booking | Airbnb |
|---|---|---|
| pool | `hotelfacility=433` | `amenities[]=7` |
| jacuzzi / spa | `hotelfacility=54` (spa & wellness) | `amenities[]=25` (hot tub) |
| entire place | `privacy_type=3` | `room_types[]=Entire home/apt` |
| private pool, parking, gym, wifi, air conditioning, balcony, sea view, breakfast, free cancellation, rating 8+ | yes | — |

**A source that cannot express every selected filter is skipped, not queried unfiltered.** Tick
something only Airbnb has and you get Airbnb-only results; tick "breakfast" and only Booking runs.
The panel says which sources will run before you search and which were dropped and why. The Booking
ids were read off its live filter panel and the Airbnb amenity ids were verified to actually change
results — neither set is guessed.

Property type and bedrooms are soft: applied where a source understands them, never a reason to drop
a source.

### Getting past "only the first page"

Booking's search ignores `offset`, so a naive scrape only ever sees the first 25 results by
relevance — which is why genuinely cheap options were missing. Two fixes:

- `order=price` so each query returns the cheapest first
- **price-band walking**: the nightly range is split into bands
  (`nflt=price=PLN-min-max-1`), each queried separately, and the results merged and
  deduplicated. Four bands give roughly four times the depth, concentrated at the cheap end.

The effect on the case that prompted this (Barcelona, 1 adult, pool, 20 km): cheapest found went
from **3838 PLN** to **819 PLN** total.

Airbnb's own `price_min`/`price_max` and Booking's `price=` filter are applied server-side. Their
nightly rate excludes fees that the displayed total includes, so those rows are *not* re-filtered
here — that would throw away rows the sites already accepted.

### How each site is scraped

**Airbnb** — results ship as JSON inside `<script id="data-deferred-state-0">`; one `curl_cffi`
request, no browser. Radius is exact: the airport coordinates plus the requested km become a map
bounding box (`search_by_map=true` + `ne_lat`/`ne_lng`/`sw_lat`/`sw_lng`), and because a box is
square, every listing is re-checked against the true haversine distance. The blob lists each result
twice (list layer and map layer), so rows are deduplicated by listing id.

**Booking.com** — answers a scripted client with `202` and an AWS WAF JS challenge, and its cards
are lazy-rendered, so neither `curl_cffi` alone nor scrolling in Playwright is reliable. The hybrid
that works: **Playwright opens booking.com once with the system Chrome, the browser solves the
challenge, and the resulting `aws-waf-token` cookie is reused for `curl_cffi` fetches** — which come
back fully server-rendered with prices, no scrolling. The browser starts at most once per process; a
stale token triggers exactly one re-mint. Booking reports distance from the **city centre**, not the
airport, so the radius is applied against that as an approximation (shown as `~ km od centrum`).

Google Hotels used to be a third source and was dropped: it exposed no URL-addressable filters at
all, so every filter had to be re-applied locally after the fact, and it cost a second Playwright
browser run for results Booking already covers.

```bash
pip install playwright        # no browser download, uses your system Chrome
```

Playwright is optional and deliberately not in `requirements.txt`. Without it Airbnb still works and
Booking reports why it could not run.

### Lufthansa (`lufthansa.py`)

Its booking engine sits behind a Cloudflare Managed Challenge that a plain `curl_cffi` request
can't solve, and that even a genuine Playwright-driven Chrome fails automatically — Cloudflare
fingerprints the CDP automation channel and loops the "verifying you are human" page forever.
`patchright` (a patched Playwright build that hides the CDP tells) gets a real Chrome tab through
it. The obvious next move — mint once, replay the `cf_clearance` cookie via fast `curl_cffi`
calls, same trick as Booking.com above — does **not** work here: Cloudflare also binds clearance
to the TLS/HTTP2 fingerprint of the connection that earned it, and `curl_cffi`'s
`impersonate="chrome"` doesn't match closely enough, so every replayed call still 403s.

What works instead: keep **one** patchright Chrome tab open for the whole process and drive every
search as a real navigation in that tab. The first navigation pays the Cloudflare cost (~10s);
every navigation after that in the same tab is fast (~3-4s) because `cf_clearance` is a genuine
same-connection cookie by then. The navigation itself has to match the site's own flow — landing on
`www.lufthansa.com/<market>/flight-search` and auto-submitting a same-origin POST form to
`shop.lufthansa.com/booking/availability`, not a plain `page.goto` with the search baked into the
query string (that gets stuck in an infinite challenge loop). The fare data itself is never fetched
directly; it's captured off the wire via a `page.on("response")` listener watching for
`POST .../one-booking/v2/search/air-calendars`, which the SPA calls on its own as a side effect of
the navigation.

Because every unique origin/destination pair costs several real navigations, `server.py` doesn't
fan Lufthansa out across a whole country's airports the way Ryanair/WizzAir/LOT do — it only
searches the explicit origins the user picked (or the country's primary airport), capped to a
handful of pairs.

```bash
pip install patchright && patchright install chrome
```

### China Airlines (`china_airlines.py`)

Same backend shape as Lufthansa (the same Amadeus/Travelport "one-booking" platform, just a
different tenant — `api-des.china-airlines.com` instead of `api.shop.lufthansa.com`), but fronted
by three bot-protection vendors stacked at once — Akamai, Imperva Incapsula (the `reese84` token),
and DataDome — versus Lufthansa's single Cloudflare challenge. Two things that work for Lufthansa
don't work here: replaying cookies via `curl_cffi` (same TLS-fingerprint problem), and
auto-submitting a hidden form (`form.submit()`) to reach the results host — that comes back
"Access Denied" every time, because Imperva's `reese84` token scores real behavioural signals
(trusted click events), and a JS-triggered, non-user-initiated submit doesn't pass.

What works: drive the real search widget with genuine Playwright input (`page.mouse.click` /
`.fill()`, which dispatch trusted events) and click the real "Search Flights" button. The date
range picker's day cells only respond to trusted clicks at their exact screen coordinates *and* a
plain JS `.click()` updates the DOM without ever firing React's handlers — so this module skips
the calendar UI entirely and types straight into its date field (`.fill()` on the
`YYYY/MM/DD - YYYY/MM/DD` text input works and updates the same state, confirmed by checking the
resulting request body). Search dates within a few days of "today" trigger an extra "close to
departure" gate; searching 2+ weeks out avoids it.

This target is noticeably flakier than Lufthansa — it sometimes routes through Akamai's own
"processing your request" interstitial before the real results page, and repeated rapid testing
from the same browser profile appeared to escalate scrutiny. `server.py` retries the interstitial
with a bounded wait loop, but a failed pair here just yields no rows for it rather than raising.

Both this module and `lufthansa.py` pin their patchright browser to one dedicated worker thread for
the life of the process (`_task_q` / `_worker_loop`) — Playwright's sync API is thread-affined, and
`server.py` dispatches each carrier on a fresh `threading.Thread` per search, so a plain lock isn't
enough (a second search from a different thread crashes with "no running event loop"). The worker
thread also has to create its own asyncio event loop explicitly (`asyncio.set_event_loop`) since
Python 3.12+ stopped doing that implicitly for non-main threads.

```bash
pip install patchright && patchright install chrome
```

### Standalone

```bash
.venv/bin/python stays.py --airport BCN --checkin 2026-09-10 --checkout 2026-09-17 --radius-km 20 --adults 1 --filters pool --max-night 400
```

```bash
.venv/bin/python stays.py --airport PMI --checkin 2026-09-08 --checkout 2026-09-15 --sources airbnb --limit 30
```

`GET /api/stay-filters` lists the vocabulary and which sources support each entry.

`GET /api/stays?dest=BCN&checkin=…&checkout=…&radius_km=20&adults=1&filters=pool,entire&min_night=100&max_night=400&sources=airbnb,booking&bands=4`

---

Search parameters: `from`, `to`, `origins` (comma-separated IATA), `date_from`, `date_to`,
`adults`, `max_price`, `carriers`, `mode` (`cheapest` \| `calendar`), `trip` (`oneway` \| `return`),
`nights`, `nights_tol`, `max_stops`, `limit`.

`GET /api/carriers` lists the registered sources. Adding a fourth means one entry in the `CARRIERS`
dict in [server.py](server.py) plus a matching `<name>_rows()` method on `SearchRun` — the UI builds
its checkboxes from that endpoint, so no frontend change is needed.

---

# Ryanair (`ryanair.py`)

## Use

Cheapest one-way per destination, all 13 Polish airports:

```bash
.venv/bin/python ryanair.py scan --from pl --to es --date-from 2026-08-10 --date-to 2026-09-30 --out fares.csv
```

Round trip, 5-10 nights:

```bash
.venv/bin/python ryanair.py scan --from pl --to es --round-trip --duration 5-10 --date-from 2026-09-01 --date-to 2026-09-20
```

Day-by-day prices on every PL->ES route (slower, one request per route per month):

```bash
.venv/bin/python ryanair.py calendar --from pl --to es --date-from 2026-09-01 --date-to 2026-10-31 --max-price 200 --out calendar.csv
```

Only some origins:

```bash
.venv/bin/python ryanair.py calendar --origins KRK WAW WMI --to es --date-from 2026-09-01 --date-to 2026-09-30
```

Airport list:

```bash
.venv/bin/python ryanair.py airports --country es
```

Options: `--adults`, `--max-price`, `--format csv|json`, `--out FILE`, `--currency`, `--market`,
`--workers`, `--rate` (min seconds between requests), `--top` (rows in the stderr summary table),
`--quiet`. Data goes to `--out` / stdout; progress and the summary table go to stderr, so piping
stays clean.

## Endpoints used

Ryanair's own public JSON API — the same calls www.ryanair.com makes. No key, no cookies needed.

| Endpoint | Purpose |
|---|---|
| `GET /api/views/locate/5/airports/{lang}/active` | all active airports + country codes |
| `GET /api/views/locate/searchWidget/routes/{lang}/airport/{IATA}` | destinations served from an airport |
| `GET /api/farfnd/v4/oneWayFares` | cheapest one-way **per destination** in a date range |
| `GET /api/farfnd/v4/roundTripFares` | cheapest round trip per destination, filtered by nights |
| `GET /api/farfnd/3/oneWayFares/{orig}/{dest}/cheapestPerDay?outboundMonthOfDate=YYYY-MM-01` | cheapest fare per day, one route, one month |

Key parameter gotchas found while inspecting the live site:

- `farfnd/v4` wants **`departureAirportIataCode`**, not `departureAirportIso`. Wrong name gives
  a misleading `400 ValidationError: "Any departure filter has to be provided"`.
- `oneWayFares` returns **one cheapest fare per destination airport**, not every flight.
  `limit`/`offset` are accepted but ignored. Use `calendar` when per-day granularity is needed.
- Date range params are required as a pair: `outboundDepartureDateFrom` + `outboundDepartureDateTo`.
- `priceValueTo` + `currency` filter server-side.

## Limitation

`GET /api/booking/v4/{market}/availability` (full flight list, seat counts, fare classes) is
bot-protected and returns `409 {"message":"Availability declined"}` for plain HTTP clients —
including from inside a browser without a valid booking session. Getting it needs a real browser
session (Playwright driving ryanair.com and reusing its cookies). The `farfnd` endpoints above are
not protected and are enough for price hunting.

## Politeness

Default `--rate 0.25` = max 4 req/s across 6 worker threads, with retry/backoff on 429 and 5xx.
Raise `--rate` if you scan wide date ranges repeatedly.

---

# Wizz Air (`wizzair.py`)

Same subcommands. CSV adds `departure_times` (all departure times that day) and `price_type`;
it has no `flight_number` — Wizz's timetable endpoint does not return flight numbers.

```bash
.venv/bin/python wizzair.py scan --from pl --to es --date-from 2026-08-10 --date-to 2026-09-30 --out wizz.csv
```

```bash
.venv/bin/python wizzair.py calendar --origins WAW KRK --to es --date-from 2026-08-01 --date-to 2026-10-31 --max-price 400
```

```bash
.venv/bin/python wizzair.py airports --country es
```

`scan` keeps only the cheapest day per route; `calendar` keeps every priced day. `--round-trip`
adds the return leg as extra rows. Extra flags: `--include-unpriced`, `--api-url`.

## Endpoints used

Wizz runs on Navitaire New Skies behind `be.wizzair.com`. The API path is **version-pinned**, and
the version is read live from the booking page rather than hardcoded — so it survives Wizz bumping
it. `FALLBACK_API` in the script is only used if that scrape fails.

| Endpoint | Purpose |
|---|---|
| `GET {site}/{market}/booking/select-flight/...` | HTML containing `apiUrl:"https://be.wizzair.com/<VERSION>/Api"` |
| `GET {api}/asset/map?languageCode={market}` | every city, country, and its direct connections (full route graph) |
| `POST {api}/search/timetable` | cheapest fare for each day in a range, one route |
| `POST {api}/asset/farechart` | cheapest fare for a date ± `dayInterval` days |
| `GET {api}/asset/currencies` | supported currency codes |

Gotchas found while inspecting the live site:

- **`X-RequestVerificationToken` is mandatory after the first call.** The first response sets a
  `RequestVerificationToken` cookie; every later request must echo that value in the header of the
  same name, or the API answers `400 {"handlerError":"InvalidProtocol"}`. A fresh session works
  once and then fails — which is what makes this one confusing to debug.
- `search/timetable` rejects ranges wider than **42 days** with
  `400 {"validationCodes":["InvalidTimeDateRange"]}`. The script chunks longer ranges automatically.
- Rows with `priceType: "checkPrice"` carry `amount: 0.0` — the fare is withheld, not free. They are
  dropped unless `--include-unpriced` is passed.
- `timetable` and `farechart` are POST only; GET returns `405`.
- Route discovery comes from `asset/map`, so only routes Wizz actually flies get queried.
- **Metropolitan-area codes leak into results.** Airports sharing a `mac` in `asset/map`
  (`KRK`+`KTW` = `SPQ`, `WAW`+`WMI` = `WSW`) are searched together, so asking for `--origins KRK`
  also returns `KTW` departures. Rows report the real `departureStation` from the response, so the
  data is correct — just wider than requested.

## Limitation

`POST {api}/search/search` (full flight list with fare classes and seat counts) answers **429** for
plain HTTP clients — it needs a real booking session. The Wizz SPA is also behind Akamai bot
management: driving `wizzair.com` with an automated browser lands on `Critical error` on the
select-flight page. `timetable` + `farechart` are unprotected and cover price hunting.

## Politeness

Default `--rate 1.5` with 2 workers. `be.wizzair.com` throttles much harder than Ryanair — dropping
the rate produces 503 storms. Retries use `backoff_factor=3.0` and honour `Retry-After`.

---

# LOT (`lot.py`)

```bash
.venv/bin/python lot.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-30
```

```bash
.venv/bin/python lot.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-20 --round-trip --nights 7 --nights-tol 2
```

```bash
.venv/bin/python lot.py routes --from pl --to es
```

The CLI resolves `--from pl` through a small built-in airport list (`COUNTRY_HINT`), because LOT's
route feed has no country field; for anything else pass `--origins`/`--dests` with IATA codes. The
web UI does not need it — it resolves the country pair through the other sources' airport lists.

## Endpoints used

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/session/start` | returns the `sessionId` every later call must carry |
| `GET /api/v1/ibe/search/prices` | per-day prices for one route (`tripType=O` or `R`) |
| `GET /api/{lang}/{market}/lowfarecalendarairports.json` | ~1000 origin/destination pairs LOT flies |

The `prices` call needs these headers, and the values are not negotiable — they are the literal
arguments lot.com's own bundle passes to `ibeAirBestPricesSearch`:

```
channel: 1        remoteIP: 127.0.0.1     language: pl      market: pl
sessionId: <id>   step: SEARCH            action: DO_SEARCH
```

Gotchas found while wiring it up:

- **`step` and `action` are strings, not numbers.** Numeric values pass validation and then return
  `500 {"code":"INTERNAL_ERROR"}`, which reads like a broken endpoint rather than a bad request.
- `marketCode` must match `^[a-zA-Z]{2}$` and `startDate` must be `YYYY-MM-DD`; both give clear 400s.
- **One call returns ~152 days**, so a whole season is a single request — this is the fastest of the
  four scrapers per route.
- Prices are in **minor units**: `59824` means `598.24 PLN`.
- `tripType=R&fixedDepartureDate=false` returns one row per departure day with LOT's *own* choice of
  return date, ignoring any requested stay length. To honour `--nights`, the scraper ranks departure
  days with that cheap call, then re-queries the `--max-departures` cheapest with
  `fixedDepartureDate=true`, which prices every return date for that departure.
- lot.com rejects plain `requests` on TLS fingerprint, so this uses `curl_cffi` too.

---

# Everything else (`kiwi.py`)

```bash
.venv/bin/python kiwi.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-20
```

```bash
.venv/bin/python kiwi.py scan --from de --to es --date-from 2026-09-01 --date-to 2026-09-20 --max-stops 1 --limit 200
```

```bash
.venv/bin/python kiwi.py scan --from pl --to es --date-from 2026-09-01 --date-to 2026-09-20 --round-trip --nights 7 --nights-tol 2
```

Also `kiwi.py countries` and `kiwi.py airports --country es`.

CSV adds `airline`, `airline_back` and `stops`; `--max-stops 0` (the default) keeps it to direct
flights so results are comparable with the other two scrapers.

## Endpoints used

| Endpoint | Purpose |
|---|---|
| `POST https://api.skypicker.com/umbrella/v2/graphql` → `onewayItineraries` | cheapest one-way itineraries between two countries |
| same → `returnItineraries` | return itineraries, with a `nightsCount` range |
| `GET https://api.skypicker.com/locations?type=dump&location_types=country` | 223 countries in one request |
| `GET https://api.skypicker.com/locations?type=subentity&term=PL&location_types=airport` | airports in a country |

Notes from wiring it up:

- **The endpoint rejects Python's `requests` on TLS fingerprint, not on headers** — even the plain
  HTML page 403s. `curl_cffi` with `impersonate="chrome"` fixes it; that is the only reason
  `curl_cffi` is a dependency.
- Locations are ids, not codes: `Country:PL`, `Station:WAW`. A whole country pair is therefore
  **one request**, not one per airport — Kiwi is by far the fastest of the three.
- `filter.limit` caps results (the search is price-sorted, so a low limit returns only
  ultra-low-cost carriers); `filter.excludeCarriers` takes IATA codes and is what keeps Kiwi from
  repeating Ryanair/Wizz rows.
- `filter.maxStopsCount` is the only way to get connections at all — neither airline API offers them.
- GraphQL introspection is enabled, which is how the query shapes above were derived.

## Caveat

Prices are **Kiwi's**, not the airline's, and can differ from booking direct. Results with
`stops > 0` may be self-transfer combinations across airlines rather than a single ticket. The row
names the operating airline — check it before booking.
