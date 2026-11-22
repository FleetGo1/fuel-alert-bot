"""
High-value (HV) load rules, driven by the drivers' Telegram group chats.

The bot is a member of every driver group. It reads new messages and looks for:
  * "NEW TRIP ... HAS BEEN ASSIGNED" -> trip #, pickup stop name, address, appointment
  * "HIGH-VALUE LOAD"                -> marks that trip as HV
  * "loaded", "picked up", "PU done", "BOL" ... -> pickup confirmed by the driver

HV rules (from the HV message):
  1. Fuel must be ABOVE 75% before pickup      -> alert + tag if not
  2. After pickup roll 300 miles NONSTOP       -> miles are counted from GPS;
     low-fuel alerts say "no stop until mile 300", and the bot alerts if the truck stops.

Pickup is detected either by the driver's message or by GPS: the truck was at the
shipper (address looked up with the free US Census geocoder) and then drove away.
"""

import html
import json
import math
import re
import time
import urllib.parse
import urllib.request

HV_PATTERN = re.compile(r"HIGH[\s-]*VALUE|\bHV\s+LOAD\b", re.I)
TRIP_PATTERN = re.compile(r"NEW TRIP\s+#?(\w+)\s+HAS BEEN ASSIGNED", re.I)
PICKUP_DONE_PATTERN = re.compile(
    r"\b(loaded(?!\s+distance)|picked\s*up|pu\s*(done|completed|complete)|got\s+loaded|bol)\b", re.I)
LINK_WINDOW = 6 * 3600          # HV message and trip message must be within 6 h of each other


# ------------------------------------------------------------------ parsing ---

def parse_trip(text):
    """Parse a 'NEW TRIP ... ASSIGNED' message. Returns dict or None."""
    m = TRIP_PATTERN.search(text or "")
    if not m:
        return None
    trip = {"trip": m.group(1), "stops": []}
    for block in re.split(r"🏁\s*STOP", text)[1:]:
        name = re.match(r"\s*\d+\s*:\s*(.+)", block)
        addr = re.search(r"ADDRESS:\s*\n?\s*(.+)", block)
        appt = re.search(r"APPT:\s*(.+)", block)
        typ = re.search(r"TYPE:\s*(.+)", block)
        trip["stops"].append({
            "name": name.group(1).strip() if name else "",
            "address": addr.group(1).strip() if addr else "",
            "appt": appt.group(1).strip() if appt else "",
            "type": typ.group(1).strip() if typ else "",
        })
    pickups = [st for st in trip["stops"] if "pick" in st["type"].lower()] or trip["stops"][:1]
    trip["pickup"] = pickups[0] if pickups else None
    loaded = re.search(r"LOADED DISTANCE:\s*([\d,\.]+)", text)
    trip["loaded_miles"] = float(loaded.group(1).replace(",", "")) if loaded else None
    return trip


def is_hv(text):
    return bool(HV_PATTERN.search(text or ""))


def is_pickup_done(text):
    return bool(PICKUP_DONE_PATTERN.search(text or ""))


# --------------------------------------------------------------- geography ---

def miles_between(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fuel-alert-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def geocode(address):
    """Return (lat, lon, radius_miles) or None. Full address first, then ZIP code only."""
    if not address:
        return None
    try:
        q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
        data = _get_json(f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?{q}")
        matches = data.get("result", {}).get("addressMatches", [])
        if matches:
            c = matches[0]["coordinates"]
            return (c["y"], c["x"], 2.0)
    except Exception as exc:
        print("Geocode (address) failed:", exc)
    zip_m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", address)
    if zip_m:
        try:
            q = urllib.parse.urlencode({"postalcode": zip_m.group(1), "country": "us", "format": "json", "limit": 1})
            data = _get_json(f"https://nominatim.openstreetmap.org/search?{q}")
            if data:
                return (float(data[0]["lat"]), float(data[0]["lon"]), 6.0)
        except Exception as exc:
            print("Geocode (zip) failed:", exc)
    return None


# ------------------------------------------------------- telegram reading ---

def fetch_group_messages(s, state):
    """Read new messages the bot received since last time (Telegram getUpdates)."""
    if not s.telegram_token:
        return []
    offset = state.get("tg_offset", 0)
    q = urllib.parse.urlencode({"offset": offset, "timeout": 0,
                                "allowed_updates": json.dumps(["message", "channel_post"])})
    data = _get_json(f"https://api.telegram.org/bot{s.telegram_token}/getUpdates?{q}")
    messages = []
    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat", {})
        messages.append({
            "chat_id": str(chat.get("id")),
            "title": chat.get("title") or "",
            "text": msg.get("text") or msg.get("caption") or "",
            "date": msg.get("date") or time.time(),
        })
    return messages


def driver_type(text):
    """'2304 Scotty (C/D)' -> 'C/D'. Company driver C/D, owner-operator O/O, lease operator L/O."""
    m = re.search(r"\b([CLO])\s*/\s*([DO])\b", (text or "").upper())
    if not m:
        return None
    value = f"{m.group(1)}/{m.group(2)}"
    return value if value in ("C/D", "O/O", "L/O") else None


def unit_for_group(title, units):
    """Find a fleet unit number inside a group title, e.g. 'Unit 2304 - Scotty' -> '2304'."""
    tokens = re.findall(r"[A-Za-z]?\d+", title or "")
    if not units:                       # unit list unknown (test mode): take the number in the title
        return max(tokens, key=len) if tokens else None
    hits = [t for t in tokens if t in units] + [t.upper() for t in tokens if t.upper() in units]
    return max(hits, key=len) if hits else None


def process_messages(s, state, messages, units, geocoder=geocode):
    """Update trips / HV loads from driver group messages. Returns notes for the log."""
    groups = state.setdefault("groups", {})
    hv = state.setdefault("hv", {})
    for m in messages:
        if m["chat_id"] == str(s.telegram_chat_id):
            continue  # the monitoring group itself
        g = groups.setdefault(m["chat_id"], {})
        g["title"] = m["title"]
        text = m["text"]
        dtype = driver_type(m["title"])
        if dtype:
            g["type"] = dtype

        # Manual link: "/unit 2304" or "/unit 2304 C/D" typed in a driver group
        cmd = re.match(r"^/unit(?:@\w+)?\s+(\S+)(.*)$", text.strip(), re.I)
        if cmd:
            g["unit"] = cmd.group(1)
            if driver_type(cmd.group(2)):
                g["type"] = driver_type(cmd.group(2))
            continue
        unit = g.get("unit") or unit_for_group(m["title"], units)
        if not unit:
            continue
        g["unit"] = unit

        trip = parse_trip(text)
        if trip:
            g["last_trip"] = trip
            g["last_trip_at"] = m["date"]
            from trips import start_trip      # every load is tracked (black-state warnings)
            start_trip(state, unit, trip, m["date"], geocoder)
            if is_hv(text) or m["date"] - g.get("last_hv_at", 0) <= LINK_WINDOW:
                start_hv(state, unit, trip, m["date"], geocoder)
            continue

        if is_hv(text):
            g["last_hv_at"] = m["date"]
            if g.get("last_trip") and m["date"] - g.get("last_trip_at", 0) <= LINK_WINDOW:
                start_hv(state, unit, g["last_trip"], m["date"], geocoder)
            continue

        if is_pickup_done(text) and unit in hv and hv[unit]["status"] in ("assigned", "at_shipper"):
            hv[unit]["status"] = "picked_up_msg"   # GPS start point is set on the next truck check


def start_hv(state, unit, trip, when, geocoder):
    hv = state.setdefault("hv", {})
    if unit in hv and hv[unit].get("trip") == trip["trip"]:
        return
    pickup = trip.get("pickup") or {}
    geo = geocoder(pickup.get("address")) if pickup else None
    hv[unit] = {
        "trip": trip["trip"],
        "pickup_name": pickup.get("name", ""),
        "pickup_appt": pickup.get("appt", ""),
        "pickup_geo": list(geo) if geo else None,
        "status": "assigned",        # assigned -> at_shipper -> rolling -> (done)
        "created": when,
        "alerted": [],
    }


# ---------------------------------------------------------- HV truck check ---

def fuel_needed_pct(s, miles):
    # conservative: assume a hard road (hills / heavy load) so the truck never runs short
    return miles / (s.mpg * s.hard_road_factor) / s.tank_gallons * 100


def hv_check(s, state, truck, now_ts):
    """Advance the HV load for this truck. Returns (messages, nonstop_info or None).

    messages = [(kind, monitoring_text, driver_text_or_None)], kind = "tag" or "info".

    nonstop_info = {"miles_left": x} while the truck is inside the 300-mile nonstop window.
    """
    hv = state.get("hv", {})
    load = hv.get(truck["unit"])
    if not load:
        return [], None
    msgs = []
    unit = truck["unit"]
    head = f"Unit {unit} · HV trip {load['trip']}"
    have_gps = truck["lat"] is not None and truck["lon"] is not None

    # Drop very old loads that never started
    if load["status"] in ("assigned", "at_shipper") and now_ts - load["created"] > 5 * 86400:
        hv.pop(unit, None)
        return [], None

    dist = None
    if have_gps and load.get("pickup_geo"):
        plat, plon, radius = load["pickup_geo"]
        dist = miles_between(truck["lat"], truck["lon"], plat, plon)

    # ---- before pickup -----------------------------------------------------
    if load["status"] in ("assigned", "at_shipper"):
        if dist is not None and dist <= load["pickup_geo"][2]:
            load["status"] = "at_shipper"
        elif load["status"] == "at_shipper" and dist is not None and dist > load["pickup_geo"][2] + 1:
            load["status"] = "rolling_start"

        low = truck["fuel"] is not None and truck["fuel"] < s.hv_min_fuel
        pickup_name = html.escape(load["pickup_name"])
        if low and "low_assigned" not in load["alerted"]:
            load["alerted"].append("low_assigned")
            where = f" ({dist:.0f} mi away)" if dist is not None else ""
            msgs.append(("tag",
                         f"⚠️ <b>HV LOAD – fuel {truck['fuel']:.0f}%, must be above {s.hv_min_fuel:.0f}% "
                         f"before pickup</b>\n{head}\n🏁 Pickup: {pickup_name}{where}"
                         f"\n🕑 Appt: {html.escape(load['pickup_appt'])}\n⛽ Fuel BEFORE arriving at the shipper.",
                         f"⚠️ <b>HIGH-VALUE LOAD: your fuel is {truck['fuel']:.0f}%.</b>\n"
                         f"It must be ABOVE {s.hv_min_fuel:.0f}% before pickup at {pickup_name}.\n"
                         f"⛽ Please fuel up BEFORE you arrive at the shipper."))
        if low and dist is not None and dist <= s.hv_near_miles and "low_near" not in load["alerted"]:
            load["alerted"].append("low_near")
            msgs.append(("tag",
                         f"🔴 <b>URGENT HV: {dist:.0f} mi from shipper with only {truck['fuel']:.0f}% fuel</b>"
                         f"\n{head}\nMust be above {s.hv_min_fuel:.0f}% before pickup.",
                         f"🔴 <b>URGENT: you are {dist:.0f} mi from the shipper with {truck['fuel']:.0f}% fuel.</b>\n"
                         f"HIGH-VALUE LOAD: fuel must be ABOVE {s.hv_min_fuel:.0f}% before pickup. "
                         f"Fuel up at the next stop before the shipper."))

    if load["status"] in ("picked_up_msg", "rolling_start"):
        load.update(status="rolling", miles=0.0, still=0, stop_alerted=False,
                    last=[truck["lat"], truck["lon"]] if have_gps else None, pu_ts=now_ts)
        enough = truck["fuel"] is None or truck["fuel"] >= fuel_needed_pct(s, s.hv_nonstop_miles) + s.reserve_pct
        msgs.append(("tag" if not enough else "info",
                     f"🚚 <b>HV picked up – {s.hv_nonstop_miles:.0f} mi NONSTOP starts</b>\n{head}"
                     + (f"\n⛽ Fuel {truck['fuel']:.0f}%" if truck["fuel"] is not None else "")
                     + ("" if enough else "\n🔴 Fuel may NOT be enough for the nonstop distance!"),
                     f"🚚 <b>HIGH-VALUE LOAD picked up.</b>\n"
                     f"🛑 Roll {s.hv_nonstop_miles:.0f} miles NONSTOP from the shipper. "
                     f"I will tell you when you reach mile {s.hv_nonstop_miles:.0f}."))
        return msgs, {"miles_left": s.hv_nonstop_miles}

    # ---- after pickup: count miles, watch for stops -------------------------
    if load["status"] == "rolling":
        if have_gps:
            if load.get("last"):
                step = miles_between(load["last"][0], load["last"][1], truck["lat"], truck["lon"])
                load["miles"] += step * 1.1          # road distance is ~10% longer than straight line
                if step < s.stop_move_miles:
                    load["still"] += 1
                else:
                    load["still"], load["stop_alerted"] = 0, False
            load["last"] = [truck["lat"], truck["lon"]]

        left = s.hv_nonstop_miles - load["miles"]
        if left <= 0:
            hv.pop(unit, None)
            msgs.append(("info",
                         f"✅ {head}: {s.hv_nonstop_miles:.0f} nonstop miles done "
                         f"(~{load['miles']:.0f} mi). Normal fuel stops allowed.",
                         f"✅ You have driven {s.hv_nonstop_miles:.0f} miles since pickup. You may stop now. "
                         f"Always park in a safe, secure area and back the trailer against a wall."))
            return msgs, None

        place = html.escape(truck["place"])
        if load["still"] >= s.stop_checks and not load["stop_alerted"]:
            load["stop_alerted"] = True
            msgs.append(("tag",
                         f"🛑 <b>HV truck STOPPED at mile ~{load['miles']:.0f}</b> "
                         f"(nonstop until {s.hv_nonstop_miles:.0f})\n{head}\n📍 {place}",
                         f"🛑 <b>HIGH-VALUE LOAD: the truck has stopped at mile ~{load['miles']:.0f}.</b>\n"
                         f"Please keep rolling – about {left:.0f} mi of nonstop driving left. "
                         f"If you must stop, tell us right away."))

        need = fuel_needed_pct(s, left) + s.reserve_pct
        if truck["fuel"] is not None and truck["fuel"] < need and "fuel_short" not in load["alerted"]:
            load["alerted"].append("fuel_short")
            msgs.append(("tag",
                         f"🔴 <b>HV fuel too low for nonstop: {truck['fuel']:.0f}%, "
                         f"needs ~{need:.0f}% for {left:.0f} mi left</b>\n{head}\n📍 {place}",
                         None))   # monitoring decides what the driver should do
        return msgs, {"miles_left": left}

    return msgs, None
