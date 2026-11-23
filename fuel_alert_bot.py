"""
FleetGO low-fuel alert bot.

Reads every truck's fuel % and GPS from the FleetGO Fleet page (DATA_SOURCE=fleetgo, default,
no API key needed) or from the Motive API (DATA_SOURCE=motive), and posts alerts to a Telegram group:

    below 40%  -> alert (no tag)
    30% or less -> alert + tag FleetGO monitoring
    25% or less -> URGENT alert + tag, repeated every REMIND_MINUTES while it stays low
    back above RESET_PERCENT -> "refueled" message, truck is reset

Each truck is alerted once per level, so the group is not spammed every 5 minutes.
Only the Python standard library is used - nothing to install.

Usage:
    python fuel_alert_bot.py              # check once (for GitHub Actions / cron)
    python fuel_alert_bot.py --loop       # keep running, check every CHECK_EVERY_MINUTES
    python fuel_alert_bot.py --loop --max-minutes 330   # GitHub: run ~5.5 h, then the next run takes over
    python fuel_alert_bot.py --dry-run    # print messages instead of sending
    python fuel_alert_bot.py --sample sample_vehicles.json --dry-run   # test without Motive
    python fuel_alert_bot.py --get-chat-id   # show the Telegram group id after you add the bot
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Make sure helper files (hv_loads.py, fleetgo_source.py) next to this script are found
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- settings ---

def load_env_file(path=".env"):
    """Load KEY=VALUE lines from a .env file (only if the variable is not already set)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name, default=None):
    value = os.environ.get(name, "").strip()
    return value if value else default


def env_num(name, default):
    return float(env(name, default))


class Settings:
    def __init__(self):
        # Where truck data comes from: "fleetgo" (website login, no API) or "motive" (API key)
        self.source = env("DATA_SOURCE", "fleetgo").lower()
        self.fleetgo_url = env("FLEETGO_URL", "")
        self.fleetgo_email = env("FLEETGO_EMAIL")
        self.fleetgo_password = env("FLEETGO_PASSWORD")
        # Which company to select in FleetGO (set in .env / GitHub variables, not in the code)
        self.fleetgo_company_id = env("FLEETGO_COMPANY_ID", "")
        self.save_session = env("SAVE_SESSION", "1") == "1"
        self.motive_api_key = env("MOTIVE_API_KEY")
        self.motive_oauth_token = env("MOTIVE_OAUTH_TOKEN")
        self.motive_base = env("MOTIVE_BASE_URL", "https://api.gomotive.com")
        # Pick the real token out of whatever was pasted (extra spaces, "token:" text, etc.)
        raw_token = env("TELEGRAM_BOT_TOKEN") or ""
        m = re.search(r"\d{6,12}:[A-Za-z0-9_-]{30,}", raw_token)
        self.telegram_token = m.group(0) if m else (raw_token.strip() or None)
        self.telegram_chat_id = env("TELEGRAM_CHAT_ID")
        # Telegram usernames to tag, e.g. "@ali_fleetgo @monitoring_night"
        self.monitor_tags = env("MONITOR_TAGS", "")
        self.watch = env_num("WATCH_PERCENT", 40)
        self.tag = env_num("TAG_PERCENT", 30)
        self.critical = env_num("CRITICAL_PERCENT", 25)
        self.reset = env_num("RESET_PERCENT", 50)
        self.remind_minutes = env_num("REMIND_MINUTES", 60)
        self.stale_hours = env_num("STALE_HOURS", 3)
        self.check_every = env_num("CHECK_EVERY_MINUTES", 5)
        self.state_file = env("STATE_FILE", "state.json")
        self.planner_url = env("PLANNER_URL", self.fleetgo_url.rstrip("/") + "/plan-route")
        self.fuel_card_chains = env("FUEL_CARD_CHAINS", "TA, Love's, Pilot, Flying J")
        self.never_fuel_states = {
            s.strip().upper() for s in env("NEVER_FUEL_STATES", "CA,MA,NH,OR,WA").split(",") if s.strip()
        }
        # Units to ignore, e.g. trucks in the shop: "2602,2026"
        self.ignore_units = {u.strip() for u in env("IGNORE_UNITS", "").split(",") if u.strip()}

        # High-value loads (read from the drivers' Telegram groups)
        self.read_driver_groups = env("READ_DRIVER_GROUPS", "1") == "1"
        # Where alerts go:
        #   central       = all fuel/black-state/theft alerts to ONE fuel group; driver groups only
        #                   get HV-load messages (default)
        #   driver_groups = one message in each driver's group, tagging MONITOR_TAGS there
        #   separate      = tagged alert to TELEGRAM_CHAT_ID (monitoring group) + plain message to driver
        self.alert_mode = env("ALERT_MODE", "central").lower()
        # The fuel group for central mode (defaults to the monitoring group TELEGRAM_CHAT_ID)
        self.fuel_chat_id = env("FUEL_ALERT_CHAT_ID") or self.telegram_chat_id
        # After a fuel alert, stay quiet about that truck for this long (time to reach the station)
        self.cooldown_hours = env_num("ALERT_COOLDOWN_HOURS", 5)
        # driver_groups mode: send alerts of trucks without a linked group to TELEGRAM_CHAT_ID?
        # Off by default - that group only gets theft alerts.
        self.fallback_to_monitoring = env("FALLBACK_TO_MONITORING", "0") == "1"
        # (separate mode only) also post the plain message in each driver's own group
        self.driver_alerts = env("DRIVER_ALERTS", "1") == "1"
        self.hv_min_fuel = env_num("HV_MIN_FUEL_PERCENT", 75)       # before pickup
        self.hv_nonstop_miles = env_num("HV_NONSTOP_MILES", 300)    # after pickup
        self.hv_near_miles = env_num("HV_NEAR_SHIPPER_MILES", 60)   # urgent if still low this close
        self.tank_gallons = env_num("TANK_GALLONS", 150)
        self.mpg = env_num("MPG", 6.5)                              # until a truck's own MPG is learned
        self.hard_road_factor = env_num("HARD_ROAD_FACTOR", 0.85)    # low range = 15% worse (hills, heavy)
        self.mpg_min_gallons = env_num("MPG_MIN_GALLONS", 30)        # learn only after this much fuel used
        self.reserve_pct = env_num("RESERVE_PERCENT", 10)
        self.stop_move_miles = env_num("STOP_MOVE_MILES", 0.3)      # moved less than this = standing
        self.stop_checks = int(env_num("STOP_CHECKS", 2))           # 2 checks x 5 min = ~10 min stopped

        # Black states: warn before the truck drives into one
        self.black_min_fuel = env_num("BLACK_MIN_FUEL_PERCENT", 85)  # warn if below this
        self.black_warn_miles = env_num("BLACK_WARN_MILES", 300)     # ...within this distance of the stop
        self.black_remind_minutes = env_num("BLACK_REMIND_MINUTES", 60)  # remind if still low...
        self.black_reminders = int(env_num("BLACK_REMINDERS", 2))        # ...at most this many times

        # Driver types that get fuel alerts (low fuel, black state, theft). From the group name:
        # (C/D) company driver, (O/O) owner-operator, (L/O) lease operator.
        self.fuel_alert_types = {t.strip().upper() for t in env("FUEL_ALERT_TYPES", "C/D").split(",") if t.strip()}
        # Groups with no type in the name: still alert (1) so no truck is missed, or skip (0)
        self.alert_unknown_type = env("ALERT_UNKNOWN_TYPE", "1") == "1"

        # Sudden fuel drop (possible theft): % points lost beyond what driving explains
        self.theft_drop_pct = env_num("THEFT_DROP_PERCENT", 10)
        self.theft_confirm = env("THEFT_CONFIRM", "1") == "1"   # wait for a 2nd reading first


# ------------------------------------------------------------------ motive ---

def http_json(url, headers=None, data=None, timeout=30):
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers = dict(headers or {}, **{"Content-Type": "application/json"})
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_motive_vehicles(s):
    """Return the raw vehicle list from Motive's /v1/vehicle_locations (all pages)."""
    headers = {"Accept": "application/json"}
    if s.motive_api_key:
        headers["X-Api-Key"] = s.motive_api_key
    elif s.motive_oauth_token:
        headers["Authorization"] = "Bearer " + s.motive_oauth_token
    else:
        raise SystemExit("Set MOTIVE_API_KEY (or MOTIVE_OAUTH_TOKEN) first.")

    vehicles, page = [], 1
    while True:
        url = f"{s.motive_base}/v1/vehicle_locations?per_page=100&page_no={page}"
        data = http_json(url, headers)
        batch = data.get("vehicles", [])
        vehicles.extend(batch)
        pag = data.get("pagination") or {}
        total = int(pag.get("total") or 0)
        if not batch or len(vehicles) >= total or page >= 50:
            break
        page += 1
    return vehicles


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize(raw):
    """Turn one Motive vehicle record into a simple dict (tolerant of field differences)."""
    v = raw.get("vehicle", raw)
    loc = v.get("current_location") or {}
    driver = v.get("current_driver") or {}
    fuel = loc.get("fuel_primary_remaining_percentage")
    if fuel is None:
        fuel = v.get("fuel_primary_remaining_percentage")
    driver_name = " ".join(x for x in (driver.get("first_name"), driver.get("last_name")) if x).strip()
    description = loc.get("description") or ""
    m = re.search(r",\s*([A-Z]{2})\s*$", description)
    return {
        "unit": str(v.get("number") or v.get("id")),
        "driver": driver_name or None,
        "fuel": float(fuel) if fuel is not None else None,
        "lat": loc.get("lat"),
        "lon": loc.get("lon"),
        "place": description or "Unknown location",
        "state": m.group(1) if m else None,
        "located_at": parse_time(loc.get("located_at")),
    }


# ---------------------------------------------------------------- telegram ---

def send_telegram(s, text, dry_run=False, chat_id=None):
    if dry_run or not s.telegram_token:
        who = "driver group" if chat_id else "monitoring"
        print(f"----- to {who} -----\n" + html.unescape(re.sub(r"<[^>]+>", "", text)) + "\n")
        return
    url = f"https://api.telegram.org/bot{s.telegram_token}/sendMessage"
    payload = {
        "chat_id": chat_id or s.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        http_json(url, data=payload)
    except urllib.error.HTTPError as e:
        print("Telegram error:", e.code, e.read().decode("utf-8", "ignore"), file=sys.stderr)


def get_chat_id(s):
    if not s.telegram_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")
    data = http_json(f"https://api.telegram.org/bot{s.telegram_token}/getUpdates")
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("my_chat_member") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not seen:
        print("No chats yet. Add the bot to your group, send any message there, then run this again.")
    for cid, title in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({title})")


# ------------------------------------------------------------------- logic ---

LEVELS = {"none": 0, "watch": 1, "tag": 2, "critical": 3}


def level_for(fuel, s):
    if fuel <= s.critical:
        return "critical"
    if fuel <= s.tag:
        return "tag"
    if fuel < s.watch:
        return "watch"
    return "none"


def planner_link(s, truck):
    query = urllib.parse.urlencode({"from": f"{truck['lat']},{truck['lon']}", "driver": "", "auto": "nearby"})
    return f"{s.planner_url}?{query}"


def build_message(s, truck, level, reminder=False, nonstop=None):
    e = html.escape
    fuel = f"{truck['fuel']:.0f}%"
    if level == "critical":
        head = f"🔴 <b>URGENT: Unit {e(truck['unit'])} – {fuel} fuel</b>"
    elif level == "tag":
        head = f"🟠 <b>Low fuel: Unit {e(truck['unit'])} – {fuel}</b>"
    else:
        head = f"🟡 Fuel watch: Unit {e(truck['unit'])} – {fuel}"

    lines = [head]
    if reminder:
        lines.append(f"⏰ <b>Still not refueled</b> – {s.cooldown_hours:.0f} h since the last alert.")
    if truck["driver"]:
        lines.append(f"👤 {e(truck['driver'])}")
    lines.append(f"📍 {e(truck['place'])}")
    lines.append(range_line(s, truck["fuel"], truck.get("mpg")))
    if truck["state"] in s.never_fuel_states:
        lines.append(f"⛔ In {truck['state']} (black state): buy only enough to leave the state.")
    if nonstop:
        lines.append(f"🛑 HV load: NO STOP – about {nonstop['miles_left']:.0f} mi of nonstop driving left. "
                     f"Fuel only after that.")
    if truck["lat"] is not None and truck["lon"] is not None and not nonstop:
        lines.append(f'⛽ <a href="{e(planner_link(s, truck))}">Plan fuel from here</a>')
    return "\n".join(lines)


def range_miles(s, fuel_pct, mpg=None):
    """(low, high, plan_within) miles for a fuel %.

    high = at the truck's own average MPG (learned from its data, else MPG setting);
    low  = HARD_ROAD_FACTOR worse (hills, heavy load, headwind);
    plan_within = low estimate down to the safety reserve.
    """
    mpg = mpg or s.mpg
    gallons = fuel_pct / 100 * s.tank_gallons
    high = gallons * mpg
    low = high * s.hard_road_factor
    plan = max(0.0, (fuel_pct - s.reserve_pct) / 100 * s.tank_gallons * mpg * s.hard_road_factor)
    return low, high, plan


def range_line(s, fuel_pct, mpg=None):
    low, high, plan = range_miles(s, fuel_pct, mpg)
    basis = (f"this truck averages {mpg:.1f} mpg" if mpg
             else f"standard {s.mpg:.1f} mpg – still learning this truck")
    return (f"📏 Range: about <b>{low:.0f}–{high:.0f} mi</b> ({basis}). "
            f"Fuel within ~{plan:.0f} mi.")


def build_driver_message(s, truck, level, nonstop=None):
    """Short, direct message for the driver's own group (no tags, no internal links)."""
    e = html.escape
    fuel = f"{truck['fuel']:.0f}%"
    unit = f"Unit {e(truck['unit'])} – "
    if nonstop:
        lines = [f"{'🔴' if level == 'critical' else '🟠' if level == 'tag' else '🟡'} "
                 f"<b>{unit}fuel is {fuel}.</b>",
                 range_line(s, truck["fuel"], truck.get("mpg")),
                 f"🛑 HIGH-VALUE LOAD: do NOT stop yet – about {nonstop['miles_left']:.0f} mi of nonstop "
                 f"driving left. The monitoring team will contact you about fuel."]
        return "\n".join(lines)
    rl = range_line(s, truck["fuel"], truck.get("mpg"))
    if level == "critical":
        lines = [f"🔴 <b>URGENT: {unit}fuel is {fuel}.</b>", rl, "⛽ <b>Refueling needed now.</b>"]
    elif level == "tag":
        lines = [f"🟠 <b>Low fuel: {unit}{fuel}.</b>", rl, "⛽ Refueling needed."]
    else:
        lines = [f"🟡 {unit}fuel is {fuel}.", rl, "⛽ Plan the next fuel stop."]
    if truck["state"] in s.never_fuel_states:
        lines.append(f"⛔ You are in {truck['state']} (black state): buy only enough fuel to leave the state.")
    return "\n".join(lines)


def driver_chats(state):
    """unit -> driver group chat id (learned from the groups the bot is in)."""
    return {g["unit"]: cid for cid, g in state.get("groups", {}).items() if g.get("unit")}


def driver_types(state):
    """unit -> 'C/D' / 'O/O' / 'L/O' (from the group name), when known."""
    return {g["unit"]: g["type"] for g in state.get("groups", {}).values() if g.get("unit") and g.get("type")}


def fuel_alerts_for(s, dtype):
    """Fuel alerts (low fuel, black state, theft) are for company drivers only."""
    if dtype is None:
        return s.alert_unknown_type
    return dtype in s.fuel_alert_types


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    if state and "trucks" not in state and "hv" not in state:   # old format: {unit: {...}}
        state = {"trucks": state}
    if state.get("v") != STATE_VERSION:
        state.pop("trucks", None)   # older versions could mark alerts that were never delivered
    return state


STATE_VERSION = 2


def save_state(path, state):
    state["v"] = STATE_VERSION
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def notify(s, driver_chat, monitor_text, driver_text, tag, dry_run, private=False, hv=False):
    """Deliver one alert according to ALERT_MODE. Returns True if something was sent.

    central (default): every fuel / black-state / theft alert goes to ONE fuel group
        (FUEL_ALERT_CHAT_ID, or TELEGRAM_CHAT_ID). Driver groups stay quiet, except
        HV-load messages (hv=True), which go to the driver's group plus a copy to the fuel group.
    private=True (theft alerts): never to the driver group if a monitoring group is set.
    """
    tag_line = f"\n🔔 {html.escape(s.monitor_tags)}" if tag and s.monitor_tags else ""
    if s.alert_mode == "central":
        sent = False
        if hv and driver_text and driver_chat:
            send_telegram(s, driver_text + tag_line, dry_run, chat_id=driver_chat)
            sent = True
        if s.fuel_chat_id:
            send_telegram(s, monitor_text + tag_line, dry_run, chat_id=s.fuel_chat_id)
            sent = True
        return sent
    if private and s.telegram_chat_id:
        send_telegram(s, monitor_text + tag_line, dry_run)
        return True
    if s.alert_mode == "driver_groups":
        # One message in the driver's group (monitoring team is a member there and gets tagged)
        if driver_chat:
            send_telegram(s, (driver_text or monitor_text) + tag_line, dry_run, chat_id=driver_chat)
            return True
        if s.telegram_chat_id and s.fallback_to_monitoring:   # truck has no linked group yet
            send_telegram(s, monitor_text + tag_line, dry_run)
            return True
        return False
    # "separate": tagged details to the monitoring group, plain message to the driver group
    if s.telegram_chat_id:
        send_telegram(s, monitor_text + tag_line, dry_run)
    if driver_text and driver_chat and s.driver_alerts:
        send_telegram(s, driver_text, dry_run, chat_id=driver_chat)
    return True


def check_once(s, state, trucks, dry_run=False, now=None):
    from hv_loads import hv_check
    from trips import trip_check
    from theft import theft_check
    from mpg import learn_mpg

    now = now or datetime.now(timezone.utc)
    alerts = state.setdefault("trucks", {})
    chats = driver_chats(state)
    types = driver_types(state)
    sent = 0
    for truck in trucks:
        if not isinstance(truck["located_at"], datetime):
            truck["located_at"] = parse_time(truck["located_at"])
        unit = truck["unit"]
        if not unit or unit in s.ignore_units:
            continue
        if truck["located_at"] and (now - truck["located_at"]).total_seconds() > s.stale_hours * 3600:
            continue  # GPS/fuel data too old to trust

        driver_chat = chats.get(unit)
        # Fuel alerts only for company drivers (C/D); O/O and L/O buy their own fuel.
        # Checks still run for everyone so their data stays current.
        fuel_ok = fuel_alerts_for(s, types.get(unit))

        # This truck's real MPG, learned from its own miles and fuel use
        truck["mpg"] = learn_mpg(s, state, truck, now.timestamp())

        # Sudden fuel drop that driving can't explain -> possible theft
        for kind, text, _ in theft_check(s, state, truck, now.timestamp()):
            if truck["driver"]:
                text += f"\n👤 {html.escape(truck['driver'])}"
            if fuel_ok:
                sent += notify(s, driver_chat, text, None, True, dry_run, private=True)

        # Black state ahead on the current load -> fuel before entering
        for kind, text, driver_text in trip_check(s, state, truck, now.timestamp()):
            if truck["driver"]:
                text += f"\n👤 {html.escape(truck['driver'])}"
            if fuel_ok:
                sent += notify(s, driver_chat, text, driver_text, kind == "tag", dry_run)

        # High-value load rules (75% before pickup, 300 mi nonstop after)
        hv_msgs, nonstop = hv_check(s, state, truck, now.timestamp())
        for kind, text, driver_text in hv_msgs:
            if truck["driver"]:
                text += f"\n👤 {html.escape(truck['driver'])}"
            if driver_text:
                driver_text = f"<b>Unit {html.escape(unit)} · HV</b>\n" + driver_text
            sent += notify(s, driver_chat, text, driver_text, kind == "tag", dry_run, hv=True)

        if truck["fuel"] is None or not fuel_ok:
            continue
        prev = alerts.get(unit, {"level": "none", "last_alert": 0})
        level = level_for(truck["fuel"], s)

        # Refueled: reset and say so (only if we had alerted before)
        if truck["fuel"] >= s.reset:
            if prev["level"] != "none":
                msg = f"✅ Unit {html.escape(unit)} refueled – now {truck['fuel']:.0f}%"
                sent += notify(s, driver_chat, msg, msg if s.alert_mode == "driver_groups" else None,
                               False, dry_run)
            alerts.pop(unit, None)
            continue

        if level == "none":
            continue
        if s.cooldown_hours > 0:
            # One alert per truck, then quiet for ALERT_COOLDOWN_HOURS (time to reach the fuel
            # station that was given). Still low after that and not refueled -> alert again.
            first = prev["level"] == "none"
            due = first or now.timestamp() - prev["last_alert"] >= s.cooldown_hours * 3600
            reminder = not first
        else:   # old behaviour: alert on each new level, URGENT repeats every REMIND_MINUTES
            due = LEVELS[level] > LEVELS[prev["level"]] or (
                level == "critical" and now.timestamp() - prev["last_alert"] >= s.remind_minutes * 60)
            reminder = LEVELS[level] <= LEVELS[prev["level"]]
        if due:
            delivered = notify(s, driver_chat,
                               build_message(s, truck, level, reminder=reminder, nonstop=nonstop),
                               build_driver_message(s, truck, level, nonstop=nonstop),
                               level in ("tag", "critical"), dry_run)
            if delivered:   # only remember alerts that actually went somewhere
                alerts[unit] = {"level": level, "last_alert": now.timestamp()}
                sent += 1
    return sent


def run(s, args):
    state = load_state(s.state_file)
    if args.sample:
        with open(args.sample, encoding="utf-8") as f:
            data = json.load(f)
        vehicles = data.get("vehicles", data) if isinstance(data, dict) else data
        trucks = [normalize(v) for v in vehicles]
    elif s.source == "motive":
        trucks = [normalize(v) for v in fetch_motive_vehicles(s)]
    else:
        from fleetgo_source import fetch_fleetgo_trucks
        trucks = fetch_fleetgo_trucks(s)
    if s.read_driver_groups and not args.sample:
        from hv_loads import fetch_group_messages, process_messages
        try:
            messages = fetch_group_messages(s, state)
            process_messages(s, state, messages, {t["unit"] for t in trucks if t["unit"]})
        except Exception as exc:
            print("Reading driver groups failed:", exc, file=sys.stderr)
    sent = check_once(s, state, trucks, dry_run=args.dry_run)
    save_state(s.state_file, state)
    linked = len(driver_chats(state))
    token_ok = bool(s.telegram_token and re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", s.telegram_token))
    print(f"Telegram token format: {'OK' if token_ok else 'NOT RECOGNIZED - re-paste the secret'} | "
          f"driver groups linked: {linked}")
    # Keep logs free of unit/driver details (logs may be public on GitHub).
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC: checked {len(trucks)} trucks, sent {sent} alert(s)")


def test_telegram(s):
    """Read new driver-group messages, show what the bot understood, and reply in each group."""
    import hv_loads
    if not s.telegram_token:
        raise SystemExit("Put TELEGRAM_BOT_TOKEN in the .env file first.")
    state = load_state(s.state_file)
    messages = hv_loads.fetch_group_messages(s, state)
    print(f"Bot received {len(messages)} new message(s).")
    by_group = {}
    for m in messages:
        trip = hv_loads.parse_trip(m["text"])
        print(f"\n[{m['title']}] {m['text'][:60]!r}...")
        print("  unit from group name:", hv_loads.unit_for_group(m["title"], set()),
              "| trip:", trip and trip["trip"], "| pickup:", trip and trip["pickup"] and trip["pickup"]["name"],
              "| HV:", hv_loads.is_hv(m["text"]), "| pickup-done msg:", hv_loads.is_pickup_done(m["text"]))
        by_group.setdefault(m["chat_id"], m["title"])
    hv_loads.process_messages(s, state, messages, set())
    save_state(s.state_file, state)

    for chat_id, title in by_group.items():
        g = state.get("groups", {}).get(chat_id, {})
        unit = g.get("unit")
        trip = g.get("last_trip")
        hv = state.get("hv", {}).get(unit) if unit else None
        lines = ["🤖 <b>Fuel Alert bot test</b>",
                 f"✅ I can read this group. Linked to <b>Unit {html.escape(unit or '?')}</b>."]
        if trip:
            p = trip.get("pickup") or {}
            lines.append(f"🚛 Trip {html.escape(trip['trip'])} · pickup {html.escape(p.get('name', '?'))} · "
                         f"appt {html.escape(p.get('appt', '?'))}")
        if hv:
            geo = "found on map ✅" if hv.get("pickup_geo") else "NOT found on map ⚠️ (will rely on 'Loaded' message)"
            lines.append(f"❗️ HIGH-VALUE load detected. Shipper address {geo}.")
            lines.append(f"Rules: fuel above {s.hv_min_fuel:.0f}% before pickup, "
                         f"{s.hv_nonstop_miles:.0f} mi nonstop after pickup.")
        elif trip:
            lines.append("Regular load (no HIGH-VALUE message seen within 6 h).")
        send_telegram(s, "\n".join(lines), chat_id=chat_id)
        print(f"\nReplied in group [{title}].")


def main():
    p = argparse.ArgumentParser(description="FleetGO low-fuel alert bot")
    p.add_argument("--loop", action="store_true", help="keep running")
    p.add_argument("--max-minutes", type=float, default=0,
                   help="with --loop: stop after this many minutes (used on GitHub, max job time is 6 h)")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    p.add_argument("--sample", help="read vehicles from a JSON file instead of Motive")
    p.add_argument("--get-chat-id", action="store_true", help="show Telegram chat ids the bot can see")
    p.add_argument("--test-telegram", action="store_true",
                   help="read driver-group messages, show what the bot understood, reply in the group")
    args = p.parse_args()

    load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    s = Settings()
    if args.get_chat_id:
        return get_chat_id(s)
    if args.test_telegram:
        return test_telegram(s)
    if not args.dry_run and not s.telegram_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (or use --dry-run).")
    if not args.dry_run and s.alert_mode == "separate" and not s.telegram_chat_id:
        raise SystemExit("ALERT_MODE=separate needs TELEGRAM_CHAT_ID (the monitoring group).")

    started = time.monotonic()
    limit = args.max_minutes * 60 if args.max_minutes else None
    while True:
        check_start = time.monotonic()
        try:
            run(s, args)
        except Exception as exc:  # keep the loop alive on network hiccups
            print("Check failed:", exc, file=sys.stderr)
            if not args.loop:
                raise
        if not args.loop:
            break
        # next check CHECK_EVERY_MINUTES after this one started (not after it finished)
        wait = max(0.0, s.check_every * 60 - (time.monotonic() - check_start))
        if limit is not None and time.monotonic() - started + wait > limit:
            print(f"Loop time limit ({args.max_minutes:.0f} min) reached - stopping; the next run takes over.")
            break
        time.sleep(wait)


if __name__ == "__main__":
    main()
