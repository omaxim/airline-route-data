#!/usr/bin/python
"""Every airport's scheduled departures from flightsfrom.com, assembled into airline_routes.json.

flightsfrom.com was redesigned in October 2026. The /<IATA>/destinations page no longer
embeds its route data (the old `window.airport` script); it shows frequency buckets
("2-4 flights per day") and airline codes only. The departures board does carry the
exact schedule: /<IATA>/departures?departureDate=YYYY-MM-DD lists every departure of
that day with its flight number, operating airline (IATA code) and destination. So:

    python scrape_airline_routes.py day [YYYY-MM-DD ...]
        one board per airport for each date (default: 7 days from today), saved as
        days/<date>.json.gz. Run daily, one date at a time, the load stays at one
        page per airport per day.
    python scrape_airline_routes.py assemble
        the latest 7 consecutive days in days/ -> airline_routes.json: per route the
        flights in that week, per operating carrier, with their flight numbers and
        weekdays. Refuses to write if the days are not 7 consecutive complete ones.
    python scrape_airline_routes.py day 2026-10-14 --only PRG FRA
        a few airports, for testing.

A page that fails is retried a bounded number of times, then skipped and logged; a day
with too many failed airports is not saved, so a site change can no longer hang the job
or leave a partial week behind.
"""
from collections import defaultdict
import datetime as dt
import gzip
import json
import re
import statistics
import sys
import time
from pathlib import Path

from curl_cffi import requests
from geopy.distance import geodesic
import lxml.html

BASE = "https://www.flightsfrom.com"
DAYS = Path("days")
FORMAT = "flightsfrom-departures-2026-10"
MAX_ATTEMPTS = 4          # per page
MAX_FAILED_SHARE = 0.03   # a day with more failed airports than this is not saved
PAUSE = 1.0               # seconds between pages


def clean_coord_str(val):
    if not val:
        return "0.0"
    s = str(val).strip().lower()
    negate = "s" in s or "w" in s
    s = s.replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if match:
        num = match.group(0)
        val_abs = num[1:] if num.startswith(("+", "-")) else num
        return f"-{val_abs}" if (negate or num.startswith("-")) else val_abs
    return "0.0"


def airport_list(session):
    r = session.get(f"{BASE}/airports", timeout=60)
    try:
        airports = json.loads(r.content)["response"]["airports"]
    except (json.decoder.JSONDecodeError, KeyError):
        sys.exit("Failed to load the airport list, page body was: '%s'" % r.content[:500])
    return {a["IATA"]: a for a in airports if a.get("IATA")}


def board(session, iata, date):
    """[(dest, carrier, flight, dep 'HH:MM', arr 'HH:MM', dep_offset, arr_offset)], or None."""
    url = f"{BASE}/{iata}/departures?departureDate={date}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                return []
            if r.status_code == 200:
                root = lxml.html.document_fromstring(r.content)
                if root.xpath('//*[@data-left-list]'):
                    out = []
                    for a in root.xpath('//a[contains(@class,"map-left-dep-item")]'):
                        href = a.get("href") or ""
                        m = re.search(r"#/(\d{4}-\d\d-\d\d)/([A-Z0-9]{3})/([A-Z0-9]+)", href)
                        if not m or m.group(1) != date:
                            continue
                        dep = a.xpath('.//*[contains(@class,"map-left-dep-item__time")]')
                        out.append((m.group(2), a.get("data-airline-code") or "", m.group(3),
                                    dep[0].text_content().strip() if dep else None, a.get("data-arrival-time"),
                                    a.get("data-departure-offset"), a.get("data-arrival-offset")))
                    return out
                print(f"! {iata} {date}: no departures list on the page (attempt {attempt}/{MAX_ATTEMPTS})")
            else:
                print(f"! {iata} {date}: HTTP {r.status_code} (attempt {attempt}/{MAX_ATTEMPTS})")
        except Exception as e:
            print(f"! {iata} {date}: {e} (attempt {attempt}/{MAX_ATTEMPTS})")
        time.sleep(5 * attempt)
    return None


def scrape_day(session, listed, date, only=None):
    queue = list(only or [i for i, a in listed.items() if a.get("no_routes", 1)])
    flights, failed, seen = {}, [], set()
    while queue:
        iata = queue.pop()
        if iata in seen:
            continue
        seen.add(iata)
        print(f"{date} #{len(flights)}: {iata}")
        rows = board(session, iata, date)
        if rows is None:
            failed.append(iata)
            continue
        flights[iata] = rows
        if not only:   # destinations the list does not name still get their own board
            queue.extend(d for d, *_ in rows if d not in seen)
        time.sleep(PAUSE)
    n = sum(len(v) for v in flights.values())
    print(f"{date}: {len(flights)} airports, {n} departures, {len(failed)} failed")
    if failed:
        print("Failed: " + " ".join(sorted(failed)))
    if not only and len(failed) / max(len(seen), 1) > MAX_FAILED_SHARE:
        print(f"{date}: too many failures ({len(failed)}/{len(seen)}), not saved")
        return False
    DAYS.mkdir(exist_ok=True)
    path = DAYS / (f"{date}.json.gz" if not only else f"{date}_sample.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"date": date, "format": FORMAT, "failed": sorted(failed), "flights": flights}, f,
                  separators=(",", ":"))
    return True


def _minutes(hhmm, offset):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m - int(offset or 0)


def assemble(listed, days):
    """7 consecutive day files -> airline_routes.json (one record per airport, routes per destination)."""
    dates = [d["date"] for d in days]
    first = dt.date.fromisoformat(dates[0])
    want = [(first + dt.timedelta(i)).isoformat() for i in range(7)]
    if dates != want:
        sys.exit(f"assemble needs 7 consecutive days, have {dates}")
    per = defaultdict(lambda: defaultdict(lambda: {"carriers": defaultdict(lambda: {"n": 0, "nums": set()}),
                                                    "weekdays": set(), "block": []}))
    for d in days:
        wd = dt.date.fromisoformat(d["date"]).isoweekday()
        for origin, rows in d["flights"].items():
            for dest, carrier, num, dep, arr, dep_off, arr_off in rows:
                r = per[origin][dest]
                c = r["carriers"][carrier]
                c["n"] += 1
                c["nums"].add(num)
                r["weekdays"].add(wd)
                if dep and arr:
                    r["block"].append((_minutes(arr, arr_off) - _minutes(dep, dep_off)) % 1440)
    airports = {}
    for origin, dests in per.items():
        meta = listed.get(origin, {})
        lat, lon = clean_coord_str(meta.get("latitude")), clean_coord_str(meta.get("longitude"))
        routes = []
        for dest, r in sorted(dests.items()):
            dmeta = listed.get(dest, {})
            dll = (float(clean_coord_str(dmeta.get("latitude"))), float(clean_coord_str(dmeta.get("longitude"))))
            oll = (float(lat), float(lon))
            carriers = [{"iata": code, "flights_per_week": c["n"], "flight_numbers": sorted(c["nums"])}
                        for code, c in sorted(r["carriers"].items(), key=lambda kv: -kv[1]["n"])]
            routes.append({
                "iata": dest,
                "carriers": carriers,
                "flights_per_week": sum(c["flights_per_week"] for c in carriers),
                "frequency_basis": "departures_7d",
                "operating_days": sorted(r["weekdays"]),
                "km": int(geodesic(oll, dll).km) if (0.0, 0.0) not in (oll, dll) else None,
                "min": int(statistics.median(r["block"])) if r["block"] else None,
            })
        airports[origin] = {"iata": origin, "name": meta.get("name"), "city_name": meta.get("city_name"),
                            "country": meta.get("country"), "country_code": meta.get("country_code"),
                            "latitude": lat, "longitude": lon, "format": FORMAT,
                            "week": [want[0], want[-1]], "routes": routes}
    return airports


if __name__ == "__main__":
    args = sys.argv[1:]
    only = None
    if "--only" in args:
        i = args.index("--only")
        only, args = args[i + 1:], args[:i]
    session = requests.Session(impersonate="chrome")
    listed = airport_list(session)
    cmd = args[0] if args else "day"
    if cmd == "day":
        dates = args[1:] or [(dt.date.today() + dt.timedelta(7)).isoformat()]
        ok = all([scrape_day(session, listed, d, only) for d in dates])
        sys.exit(0 if ok else 1)
    if cmd == "assemble":
        files = sorted(p for p in DAYS.glob("????-??-??.json.gz"))[-7:]
        days = [json.load(gzip.open(p, "rt", encoding="utf-8")) for p in files]
        airports = assemble(listed, days)
        n_routes = sum(len(a["routes"]) for a in airports.values())
        print(f"assembled {days[0]['date']}..{days[-1]['date']}: {len(airports)} airports, {n_routes} routes, "
              f"{sum(r['flights_per_week'] for a in airports.values() for r in a['routes']):,} flights")
        with open("airline_routes.json", "w") as f:
            f.write(json.dumps(airports, indent=4, sort_keys=True, separators=(",", ": ")))
        sys.exit(0)
    sys.exit(__doc__)
