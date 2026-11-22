"""
Every load ("NEW TRIP ... HAS BEEN ASSIGNED") posted in a driver group is tracked here.

Each stop is looked up on the map. A stop counts as done when the truck's GPS reaches it
(or, if the address can't be found, a few hours after its appointment time).

Black-state rule: if the truck's NEXT stop is in a black state (CA, OR, WA, MA, NH by default)
and the truck is not in a black state yet, the bot tags monitoring to "fuel before entering"
once the truck is in a neighboring state or within BLACK_WARN_MILES of the stop, and the tank
is below BLACK_MIN_FUEL_PERCENT. If the tank is still low, it reminds every BLACK_REMIND_MINUTES
(at most BLACK_REMINDERS times) until the truck fuels or enters the state.
"""

import html
import re
from datetime import datetime, timedelta, timezone

from hv_loads import geocode, miles_between

NEIGHBORS = {
    "CA": {"OR", "NV", "AZ"},
    "OR": {"WA", "ID", "NV", "CA"},
    "WA": {"OR", "ID"},
    "MA": {"NY", "VT", "NH", "CT", "RI"},
    "NH": {"VT", "ME", "MA"},
}
TZ_OFFSETS = {"PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6, "CDT": -5, "EST": -5, "EDT": -4}


def stop_state(address):
    m = re.search(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*$", address or "")
    return m.group(1) if m else None


def parse_appt(text):
    """'11/20/2026 14:00 PST' -> UTC datetime, or None."""
    m = re.match(r"\s*(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2})\s*([A-Z]{3})?", text or "")
    if not m:
        return None
    try:
        local = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%m/%d/%Y %H:%M")
    except ValueError:
        return None
    offset = TZ_OFFSETS.get(m.group(3) or "", -6)
    return (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


def start_trip(state, unit, trip, when, geocoder=geocode):
    trips = state.setdefault("trips", {})
    if unit in trips and trips[unit].get("trip") == trip["trip"]:
        return
    stops = []
    for st in trip["stops"]:
        geo = geocoder(st["address"]) if st.get("address") else None
        appt = parse_appt(st.get("appt"))
        stops.append({
            "name": st.get("name", ""),
            "state": stop_state(st.get("address")),
            "appt": st.get("appt", ""),
            "appt_ts": appt.timestamp() if appt else None,
            "geo": list(geo) if geo else None,
            "done": False,
        })
    trips[unit] = {"trip": trip["trip"], "stops": stops, "created": when, "alerted": []}


def trip_check(s, state, truck, now_ts):
    """Mark reached stops, then warn before a black state. Returns [(kind, monitor_text, driver_text)]."""
    trips = state.get("trips", {})
    t = trips.get(truck["unit"])
    if not t:
        return []
    have_gps = truck["lat"] is not None and truck["lon"] is not None

    # Mark stops done (GPS arrival, or 3 h after the appointment if the address wasn't found)
    for st in t["stops"]:
        if st["done"]:
            continue
        if st["geo"] and have_gps:
            if miles_between(truck["lat"], truck["lon"], st["geo"][0], st["geo"][1]) <= st["geo"][2]:
                st["done"] = True
        elif not st["geo"] and st["appt_ts"] and now_ts > st["appt_ts"] + 3 * 3600:
            st["done"] = True
        if not st["done"]:
            break   # only the next open stop can be reached

    remaining = [st for st in t["stops"] if not st["done"]]
    if not remaining or now_ts - t["created"] > 14 * 86400:
        trips.pop(truck["unit"], None)
        return []
    nxt = remaining[0]
    black = nxt["state"]
    if black not in s.never_fuel_states or truck["state"] in s.never_fuel_states or truck["fuel"] is None:
        return []

    dist = None
    if nxt["geo"] and have_gps:
        dist = miles_between(truck["lat"], truck["lon"], nxt["geo"][0], nxt["geo"][1]) * 1.15
    near = truck["state"] in NEIGHBORS.get(black, set()) or (dist is not None and dist <= s.black_warn_miles)
    if not near or truck["fuel"] >= s.black_min_fuel:
        return []

    e = html.escape
    key = f"{t['trip']}:{nxt['name']}"
    total = truck["fuel"] / 100 * s.tank_gallons * s.mpg
    dist_txt = f", ~{dist:.0f} mi to the stop" if dist is not None else ""
    msgs = []
    warned = t.setdefault("warned", {})          # key -> [first_ts, last_ts, reminders]
    w = warned.get(key)
    if w and w[2] < s.black_reminders and now_ts - w[1] >= s.black_remind_minutes * 60:
        w[1], w[2] = now_ts, w[2] + 1
        text = (f"🔴 <b>REMINDER – Unit {e(truck['unit'])} still has only {truck['fuel']:.0f}% fuel "
                f"before {black} (black state)</b>\n"
                f"📍 {e(truck['place'])}\n"
                f"🏁 Next stop: {e(nxt['name'])} ({black}){dist_txt}\n"
                f"📏 Range now: about {total:.0f} mi\n"
                f"⛽ Fuel up NOW, before crossing into {black}.")
        msgs.append(("tag", text, text))
    elif not w:
        warned[key] = [now_ts, now_ts, 0]
        text = (f"⛔ <b>Unit {e(truck['unit'])} is heading to {black} (black state)</b>\n"
                f"🏁 Next stop: {e(nxt['name'])} ({black}){dist_txt}\n"
                f"⛽ Fuel is {truck['fuel']:.0f}% (range about {total:.0f} mi). "
                f"Fill up before entering {black} – fuel there is the most expensive.")
        msgs.append(("tag", text, text))
    return msgs
