#!/usr/bin/python
from collections import defaultdict
import json
import re
import sys
import time

from curl_cffi import requests
from geopy.distance import geodesic
import lxml.html


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


if __name__ == "__main__":
    print("Fetching airports list...")

    response = requests.get(
        "https://www.flightsfrom.com/airports", impersonate="chrome"
    )
    try:
        airports_json = json.loads(response.content)
    except json.decoder.JSONDecodeError as e:
        print("Failed to load airport JSON, page body was: '%s'" % response.content)
        sys.exit(1)

    iatas = [airport["IATA"] for airport in airports_json["response"]["airports"]]

    airports = defaultdict(dict)

    while iatas:
        iata = iatas.pop()
        if iata in airports:
            continue

        print("Fetching #%s: %s" % (len(airports), iata))

        skip_iata = False
        while True:
            try:
                response = requests.get(
                    "https://www.flightsfrom.com/%s/destinations" % iata,
                    impersonate="chrome"
                )
                if response.status_code == 404:
                    print("! Airport %s not found (404), skipping" % iata)
                    skip_iata = True
                    break
                if response.status_code != 200:
                    print("! HTTP %s error while fetching %s, retrying after 5s..." % (response.status_code, iata))
                    time.sleep(5)
                    continue

                root = lxml.html.document_fromstring(response.content)
                metadata_nodes = root.xpath('//script[contains(., "window.airport")]')
                if not metadata_nodes:
                    print("! Could not find window.airport metadata on page for %s (status: %s), retrying after 5s..." % (iata, response.status_code))
                    time.sleep(5)
                    continue

                metadata_tag = metadata_nodes[0].text_content()
                metadata_bits = metadata_tag.split("window.")
                break
            except Exception as e:
                status_str = "N/A"
                if 'response' in locals() and response is not None:
                    status_str = str(response.status_code)
                print("! Error while fetching IATA %s: %s (status: %s). Sleeping 5s before retrying..." % (iata, e, status_str))
                time.sleep(5)

        if skip_iata:
            continue

        metadata = {}
        for bit in metadata_bits:
            split = bit.find("=")
            if split != -1:
                metadata[bit[:split].strip()] = json.loads(bit.strip()[split + 2 : -1])

        airport_fields = [
            "city_name",
            "continent",
            "country",
            "country_code",
            "display_name",
            "elevation",
            "IATA",
            "ICAO",
            "latitude",
            "longitude",
            "name",
            "timezone",
        ]
        airport = {
            field.lower(): metadata["airport"][field] for field in airport_fields
        }
        if airport["latitude"] is not None:
            airport["latitude"] = clean_coord_str(airport["latitude"])
        if airport["longitude"] is not None:
            airport["longitude"] = clean_coord_str(airport["longitude"])
        if airport["elevation"]:
            airport["elevation"] = int(airport["elevation"])

        routes = []
        for route in metadata["routes"]:
            carriers = []
            for aroute in route["airlineroutes"]:
                airline = aroute["airline"]
                is_passenger = (
                    str(airline["is_scheduled_passenger"]) == "1"
                    or str(airline["is_nonscheduled_passenger"]) == "1"
                )
                is_active = str(airline["active"]) == "1"
                if is_active and is_passenger:
                    # flightsfrom gives no per-carrier frequency (only the route
                    # total), but it does carry per-carrier ICAO + classification
                    # flags — capture them so carriers are keyed to their
                    # operating ICAO and pre-classified downstream.
                    if str(airline.get("is_staralliance")) == "1":
                        alliance = "staralliance"
                    elif str(airline.get("is_oneworld")) == "1":
                        alliance = "oneworld"
                    elif str(airline.get("is_skyteam")) == "1":
                        alliance = "skyteam"
                    else:
                        alliance = None
                    carriers.append(
                        {
                            "name": airline["name"],
                            "iata": airline["IATA"],
                            "icao": airline.get("ICAO"),
                            "is_lowcost": str(airline.get("is_lowcost")) == "1",
                            "is_cargo": str(airline.get("is_cargo")) == "1",
                            "is_scheduled_passenger":
                                str(airline.get("is_scheduled_passenger")) == "1",
                            "alliance": alliance,
                            "flights_last_24_hours":
                                int(airline.get("flights_last_24_hours") or 0),
                        }
                    )

            orig_ll = (
                float(airport["latitude"] or 0),
                float(airport["longitude"] or 0),
            )
            dest_ll = (
                float(clean_coord_str(route["airport"].get("latitude"))),
                float(clean_coord_str(route["airport"].get("longitude"))),
            )
            distance = int(geodesic(orig_ll, dest_ll).km)

            operating_days = [
                i for i in range(1, 8) if route.get(f"day{i}") == "yes"
            ]

            routes.append(
                {
                    "carriers": carriers,
                    "flights_per_week": int(route.get("flights_per_week") or 0),
                    "passengers_per_day": int(route.get("passengers_per_day") or 0),
                    "km": distance,
                    "min": int(route["common_duration"]),
                    "iata": route["iata_to"],
                    "operating_days": operating_days,
                }
            )

            iatas.append(route["iata_to"])

        airport["routes"] = routes
        airports[iata] = airport

        time.sleep(1)

    with open("airline_routes.json", "w") as f:
        f.write(json.dumps(airports, indent=4, sort_keys=True, separators=(",", ": ")))
