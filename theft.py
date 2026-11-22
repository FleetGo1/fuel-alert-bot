"""
Sudden fuel drop (possible theft) detection.

Every check compares the truck's fuel with the last trusted reading. Driving explains some
drop: miles moved / MPG / tank size. If fuel fell at least THEFT_DROP_PERCENT points MORE than
driving can explain (e.g. 62% -> 41% while the truck barely moved), the reading is marked
suspicious. The next reading must confirm it (fuel still low) before the alert goes out, so a
single sensor glitch or fuel sloshing on a hill does not raise a false alarm.
"""

import html
from datetime import datetime

from hv_loads import miles_between


def _ts(truck, now_ts):
    t = truck.get("located_at")
    return t.timestamp() if isinstance(t, datetime) else now_ts


def theft_check(s, state, truck, now_ts):
    """Returns [(kind, monitor_text, driver_text)] - driver_text is None (monitoring only)."""
    if truck["fuel"] is None or truck["lat"] is None or truck["lon"] is None:
        return []
    last = state.setdefault("fuel_last", {})
    pending = state.setdefault("theft_pending", {})
    unit = truck["unit"]
    ts = _ts(truck, now_ts)
    cur = {"ts": ts, "fuel": truck["fuel"], "lat": truck["lat"], "lon": truck["lon"], "place": truck["place"]}

    base = last.get(unit)
    if not base:
        last[unit] = cur
        return []
    if ts <= base["ts"]:
        return []            # no new reading since last time

    moved = miles_between(base["lat"], base["lon"], cur["lat"], cur["lon"]) * 1.2
    # % that driving explains - generous (worst-case MPG) so hills/heavy loads never look like theft
    worst_mpg = min(truck.get("mpg") or s.mpg, s.mpg) * s.hard_road_factor
    burn = moved / worst_mpg / s.tank_gallons * 100
    unexplained = (base["fuel"] - cur["fuel"]) - burn

    if unexplained < s.theft_drop_pct:
        # Normal (or refuel / sensor bounced back): this reading becomes the new baseline
        pending.pop(unit, None)
        last[unit] = cur
        return []

    if unit not in pending and s.theft_confirm:
        pending[unit] = cur   # wait for one more reading before alerting (baseline stays)
        return []

    # Confirmed sudden drop
    pending.pop(unit, None)
    last[unit] = cur
    e = html.escape
    gallons = (base["fuel"] - cur["fuel"]) / 100 * s.tank_gallons
    minutes = max(1, round((ts - base["ts"]) / 60))
    text = (f"🚨 <b>POSSIBLE FUEL THEFT – Unit {e(unit)}</b>\n"
            f"⛽ Fuel dropped {base['fuel']:.0f}% → {cur['fuel']:.0f}% "
            f"(−{base['fuel'] - cur['fuel']:.0f}%, ≈ {gallons:.0f} gal) in {minutes} min\n"
            f"🚚 Truck moved only ~{moved:.0f} mi in that time (driving explains ~{burn:.0f}%)\n"
            f"📍 {e(cur['place'])}\n"
            f"Please check with the driver and the truck right away.")
    return [("tag", text, None)]
