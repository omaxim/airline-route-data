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

The site also publishes schedules about 11 months ahead. The forward schedule is
sampled one full week (Monday to Sunday, from the first Monday on or after the 8th)
per month and refreshed on a rolling basis, a few dates a day:

    python scrape_airline_routes.py forward-plan [N]
        prints the N forward dates due next: never scraped first, then the oldest
        scrape (state in forward/state.json).
    python scrape_airline_routes.py forward-done DATE [DATE ...]
        records those dates as scraped (after their boards were saved to forward_days/),
        and appends each board's totals to forward/completeness.csv: the same future
        date scraped at shrinking lead times measures how complete far schedules are.
    python scrape_airline_routes.py forward-assemble
        every complete sample week in forward_days/ -> forward/routes_by_month.csv.gz
        (week_start, origin, destination, carrier, flights in that week).

A page that fails is retried a bounded number of times, then skipped and logged; a day
with too many failed airports is not saved, so a site change can no longer hang the job
or leave a partial week behind.
"""
from collections import defaultdict
import datetime as dt
import gzip
import json
import os
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
FORWARD_DAYS = Path("forward_days")   # boards of forward dates (kept out of git: a release asset)
FORWARD = Path("forward")             # state.json, completeness.csv, routes_by_month.csv.gz
HORIZON_DAYS = 330                    # the site's schedules reach ~11 months ahead
FORMAT = "flightsfrom-departures-2026-10"
MAX_ATTEMPTS = 4          # per page
MAX_FAILED_SHARE = 0.03   # a day with more failed airports than this is not saved
PAUSE = 1.0               # seconds between pages, ONE stream only (the pace the weekly scraper
                          # ran at for years); 7 parallel streams (~5 pages/s) got this
                          # scraper's IP blocked on 12 Oct 2026
DEADLINE = float(os.environ.get("SCRAPE_DEADLINE") or 0)   # epoch seconds: stop cleanly before it
CHECKPOINT = 200          # save progress every N airports, so a stop loses nothing


class Blocked(Exception):
    """The site refused us (403/429): stop everything, never retry against a block."""


class OutOfTime(Exception):
    """SCRAPE_DEADLINE reached: save what we have and stop (the next run resumes)."""


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
            if r.status_code in (403, 429):
                raise Blocked(f"{iata} {date}: HTTP {r.status_code}")
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
        except Blocked:
            raise
        except Exception as e:
            print(f"! {iata} {date}: {e} (attempt {attempt}/{MAX_ATTEMPTS})")
        time.sleep(5 * attempt)
    return None


def _save(path, date, flights, failed, partial):
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump({"date": date, "format": FORMAT, "failed": sorted(failed), "flights": flights,
                   **({"partial": True} if partial else {})}, f, separators=(",", ":"))
    tmp.replace(path)


def scrape_day(session, listed, date, only=None, out_dir=None):
    """One date's board for every airport. Resumes from a saved partial file, checkpoints
    every CHECKPOINT airports, and stops at once if the site blocks us (the partial file
    stays, to be resumed by the next run)."""
    out = out_dir or DAYS
    out.mkdir(exist_ok=True)
    path = out / (f"{date}.json.gz" if not only else f"{date}_sample.json.gz")
    part = out / f"{date}.partial.json.gz"
    flights, failed = {}, []
    if part.exists() and not only:
        prev = json.load(gzip.open(part, "rt", encoding="utf-8"))
        flights, failed = prev["flights"], prev["failed"]
        print(f"{date}: resuming, {len(flights)} airports already done")
    seen = set(flights) | set(failed)
    queue = list(only or [i for i, a in listed.items() if a.get("no_routes", 1)])
    for rows in flights.values():
        queue.extend(d for d, *_ in rows)
    blocked = None
    while queue:
        iata = queue.pop()
        if iata in seen:
            continue
        seen.add(iata)
        print(f"{date} #{len(flights)}: {iata}")
        try:
            if DEADLINE and time.time() > DEADLINE:
                raise OutOfTime(f"deadline reached at {iata}")
            rows = board(session, iata, date)
        except (Blocked, OutOfTime) as e:
            seen.discard(iata)
            blocked = e
            break
        if rows is None:
            failed.append(iata)
            continue
        flights[iata] = rows
        if not only:   # destinations the list does not name still get their own board
            queue.extend(d for d, *_ in rows if d not in seen)
        if not only and len(flights) % CHECKPOINT == 0:
            _save(part, date, flights, failed, partial=True)
        time.sleep(PAUSE)
    if blocked:
        if not only:
            _save(part, date, flights, failed, partial=True)
        print(f"{'OUT OF TIME' if isinstance(blocked, OutOfTime) else 'BLOCKED'} ({blocked}): stopping; "
              f"{len(flights)} airports kept in {part.name} for the next run")
        raise blocked
    n = sum(len(v) for v in flights.values())
    print(f"{date}: {len(flights)} airports, {n} departures, {len(failed)} failed")
    if failed:
        print("Failed: " + " ".join(sorted(failed)))
    if not only and len(failed) / max(len(seen), 1) > MAX_FAILED_SHARE:
        print(f"{date}: too many failures ({len(failed)}/{len(seen)}), not saved")
        return False
    _save(path, date, flights, failed, partial=False)
    part.unlink(missing_ok=True)
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


def forward_targets(today=None):
    """One Monday-to-Sunday week per month, from the first Monday on or after the 8th,
    for every month whose week starts beyond the near-term week and within the horizon."""
    today = today or dt.date.today()
    out, y, m = [], today.year, today.month
    for _ in range(13):
        d = dt.date(y, m, 8)
        start = d + dt.timedelta((7 - d.weekday()) % 7)
        if start > today + dt.timedelta(8) and start + dt.timedelta(6) <= today + dt.timedelta(HORIZON_DAYS):
            out += [(start + dt.timedelta(i)).isoformat() for i in range(7)]
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _state():
    p = FORWARD / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def forward_plan(n):
    state = _state()
    targets = forward_targets()
    # never scraped first (nearest first), then the least recently scraped
    return sorted(targets, key=lambda d: (bool(state.get(d)), (state.get(d) or [""])[-1], d))[:n]


def forward_done(dates):
    FORWARD.mkdir(exist_ok=True)
    state, today = _state(), dt.date.today().isoformat()
    log = FORWARD / "completeness.csv"
    new = not log.exists()
    with open(log, "a") as f:
        if new:
            f.write("date,scraped_on,lead_days,airports,departures\n")
        for d in dates:
            p = FORWARD_DAYS / f"{d}.json.gz"
            if not p.exists():
                print(f"! {d}: no board file, not recorded")
                continue
            board = json.load(gzip.open(p, "rt", encoding="utf-8"))
            lead = (dt.date.fromisoformat(d) - dt.date.fromisoformat(today)).days
            f.write(f"{d},{today},{lead},{len(board['flights'])},{sum(len(v) for v in board['flights'].values())}\n")
            state.setdefault(d, []).append(today)
    # forget dates that have fallen out of the window
    keep = set(forward_targets())
    state = {d: v for d, v in sorted(state.items()) if d in keep}
    (FORWARD / "state.json").write_text(json.dumps(state, indent=1, sort_keys=True))


def forward_assemble():
    import csv
    by_week = defaultdict(list)
    for p in sorted(FORWARD_DAYS.glob("????-??-??.json.gz")):
        d = dt.date.fromisoformat(p.name[:10])
        by_week[(d - dt.timedelta(d.weekday())).isoformat()].append(p)
    FORWARD.mkdir(exist_ok=True)
    weeks = 0
    with gzip.open(FORWARD / "routes_by_month.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["week_start", "origin", "destination", "carrier", "flights"])
        for start, files in sorted(by_week.items()):
            if len(files) != 7:
                continue                      # a sample week counts only once all 7 days are in
            weeks += 1
            counts = defaultdict(int)
            for p in files:
                for origin, rows in json.load(gzip.open(p, "rt", encoding="utf-8"))["flights"].items():
                    for dest, carrier, *_ in rows:
                        counts[(origin, dest, carrier)] += 1
            for (o, d, c), n in sorted(counts.items()):
                w.writerow([start, o, d, c, n])
    print(f"forward-assemble: {weeks} complete sample weeks")


if __name__ == "__main__":
    args = sys.argv[1:]
    only = None
    if "--only" in args:
        i = args.index("--only")
        only, args = args[i + 1:], args[:i]
    session = requests.Session(impersonate="chrome")
    listed = airport_list(session)
    cmd = args[0] if args else "day"
    if cmd == "forward-plan":
        print(" ".join(forward_plan(int(args[1]) if len(args) > 1 else 3)))
        sys.exit(0)
    if cmd == "forward-done":
        forward_done(args[1:])
        sys.exit(0)
    if cmd == "forward-assemble":
        forward_assemble()
        sys.exit(0)
    if cmd == "forward-day":
        try:
            ok = all([scrape_day(session, listed, d, only, out_dir=FORWARD_DAYS) for d in args[1:]])
        except Blocked:
            sys.stdout.flush()
            os._exit(3)
        except OutOfTime:
            sys.stdout.flush()
            os._exit(4)
        sys.stdout.flush()
        os._exit(0 if ok else 1)
    if cmd == "day":
        dates = args[1:] or [(dt.date.today() + dt.timedelta(7)).isoformat()]
        try:
            ok = all([scrape_day(session, listed, d, only) for d in dates])
        except Blocked:
            sys.stdout.flush()
            os._exit(3)            # 3 = blocked: the workflow stops, no further dates
        except OutOfTime:
            sys.stdout.flush()
            os._exit(4)            # 4 = out of time: partial saved, resumed next run
        sys.stdout.flush()
        os._exit(0 if ok else 1)   # curl_cffi can hang the interpreter at shutdown
    if cmd == "assemble":
        files = sorted(p for p in DAYS.glob("????-??-??.json.gz"))[-7:]
        days = [json.load(gzip.open(p, "rt", encoding="utf-8")) for p in files]
        airports = assemble(listed, days)
        n_routes = sum(len(a["routes"]) for a in airports.values())
        print(f"assembled {days[0]['date']}..{days[-1]['date']}: {len(airports)} airports, {n_routes} routes, "
              f"{sum(r['flights_per_week'] for a in airports.values() for r in a['routes']):,} flights")
        with open("airline_routes.json", "w") as f:
            f.write(json.dumps(airports, indent=4, sort_keys=True, separators=(",", ": ")))
        sys.stdout.flush()
        os._exit(0)
    sys.exit(__doc__)
