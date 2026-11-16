Global airline routes data
==========================

This is a single JSON file that describes every passenger airport and their outbound airline routes, automatically updated weekly.

The file is indexed by IATA airport code. Routes specify the destination airport IATA code, flight time in minutes, and distance in kilometers.

You probably want to look at [airline_routes.json](airline_routes.json).

A nice map of the data
----------------------
<img src="https://raw.githubusercontent.com/Jonty/airline-route-data/master/map.png">

How it is collected (since October 2026)
-----------------------------------------
flightsfrom.com's redesign removed the route data from its destinations pages. The
scraper now reads each airport's departures board (`/<IATA>/departures?departureDate=`),
one date a day a week ahead (`days/<date>.json.gz`), and on Sundays adds up the latest
7 days: each route's `flights_per_week` is the count of scheduled departures that week,
split by operating carrier (`carriers[].flights_per_week`, with flight numbers), plus
`operating_days` and the median block time `min`. Carriers are IATA codes only (no
ICAO, names or flags); airports carry no ICAO code.

The site also publishes schedules about 11 months ahead. One full sample week per month
(Monday to Sunday, from the first Monday on or after the 8th) is collected for the
largest airports carrying 90% of departures (~890 boards a date), after the near-term
day, and kept refreshing oldest first. Forward boards are release assets
(`forward-boards`); `forward/routes_by_month.csv.gz` (Sundays) has each sample week's
flights per route and carrier, and `forward/completeness.csv` logs every scrape's
totals by lead time, which measures how complete far-out schedules are.

Status (16 November 2026, before unfinished dates were resumed): no date had been completed since the change, so the Sunday
assembly has not run and `airline_routes.json` is still the 4 October file. A date needs
about 3,900 boards; a run stops at 1,500 pages and two or three runs land a day; and a date
is only scraped on the one day it is a week ahead. Every file in `days/` is therefore a
`<date>.partial.json.gz` holding the 1,500 to 3,500 largest airports (85% to 99.7% of
departures). `forward/` is empty for the same reason: forward dates wait for a complete
near-term day. Theia's airline model builds its weekly snapshot from the partial days
itself (AirlineDataModel, `airline_model/board_snapshot.py`), taking each airport's
weekday from the newest board that has it.

Load: the site cut sessions off after ~70 minutes (Oct 2026). Runs are six a day, four
hours apart (GitHub starts about half of them), each at most 1,500 pages or 45 minutes, one page every 1.5 s; any block
(HTTP 403/429) stops the run, progress is saved and resumed by the next run.

Example entry
-------------
```JSON
{
    "LHR": {
        "city_name": "London",
        "continent": "EU",
        "country": "United Kingdom",
        "country_code": "GB",
        "display_name": "London (LHR), United Kingdom",
        "elevation": 80,
        "iata": "LHR",
        "icao": "EGLL",
        "latitude": "51.469603",
        "longitude": "-0.453566",
        "name": "Heathrow",
        "routes": [
            {
                "carriers": [
                    {
                        "iata": "BA",
                        "name": "British Airways"
                    },
                    {
                        "iata": "KL",
                        "name": "KLM"
                    }
                ],
                "iata": "AMS",
                "km": 371,
                "min": 80
            },
            {
                "carriers": [
                    {
                        "iata": "BA",
                        "name": "British Airways"
                    },
                    {
                        "iata": "LH",
                        "name": "Lufthansa"
                    }
                ],
                "iata": "MUC",
                "km": 943,
                "min": 115
            },
            {
                "carriers": [
                    {
                        "iata": "AF",
                        "name": "Air France"
                    },
                    {
                        "iata": "BA",
                        "name": "British Airways"
                    }
                ],
                "iata": "CDG",
                "km": 348,
                "min": 80
            },
            ...
        ],
        "timezone": "Europe/London"
    }
}
```
