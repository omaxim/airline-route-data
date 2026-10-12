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

The site also publishes schedules about 11 months ahead. The daily run refreshes four
forward dates as well, so one full sample week per month (Monday to Sunday, from the
first Monday on or after the 8th) fills in over a few weeks and then keeps refreshing,
oldest first. Forward boards are release assets (`forward-boards`, replaced on each
refresh); `forward/routes_by_month.csv.gz` (assembled Sundays) has each sample week's
flights per route and carrier, and `forward/completeness.csv` logs every scrape's
totals by lead time, which measures how complete far-out schedules are.

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
