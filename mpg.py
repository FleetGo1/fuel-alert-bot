"""
Learns each truck's real fuel economy (MPG) from its own readings.

A "window" starts at some fuel level. Miles driven are added up from GPS every check.
Once the truck has burned at least MPG_MIN_GALLONS since the window started, MPG for that
window = miles / gallons. The truck's MPG is a running average of its windows, so hills,
heavy loads and driving style are all included. Refuels and suspicious drops (possible theft)
restart the window so they don't spoil the number.
"""

from datetime import datetime

from hv_loads import miles_between

MIN_VALID, MAX_VALID = 3.5, 10.0     # ignore impossible results (sensor problems)


def learn_mpg(s, state, truck, now_ts):
    """Update the learned MPG for this truck and return it (None until learned)."""
    track = state.setdefault("mpg", {}).setdefault(truck["unit"], {"est": None, "n": 0, "win": None})
    if truck["fuel"] is None or truck["lat"] is None or truck["lon"] is None:
        return track["est"]
    t = truck.get("located_at")
    ts = t.timestamp() if isinstance(t, datetime) else now_ts
    cur = {"ts": ts, "fuel": truck["fuel"], "lat": truck["lat"], "lon": truck["lon"]}

    w = track["win"]
    if not w:
        track["win"] = {"start_fuel": cur["fuel"], "miles": 0.0, "last": cur}
        return track["est"]
    last = w["last"]
    if ts <= last["ts"]:
        return track["est"]

    # between 5-minute GPS points the road is nearly straight: add only 5% for curves
    step = miles_between(last["lat"], last["lon"], cur["lat"], cur["lon"]) * 1.05
    gap_h = (ts - last["ts"]) / 3600
    drop_now = last["fuel"] - cur["fuel"]

    refuel = cur["fuel"] > w["start_fuel"] + 3
    odd_drop = drop_now / 100 * s.tank_gallons > step / MIN_VALID + 8   # far more fuel gone than driving explains
    long_gap = gap_h > 3
    if refuel or odd_drop or long_gap:
        track["win"] = {"start_fuel": cur["fuel"], "miles": 0.0, "last": cur}
        return track["est"]

    w["miles"] += step
    w["last"] = cur
    gallons = (w["start_fuel"] - cur["fuel"]) / 100 * s.tank_gallons
    if gallons >= s.mpg_min_gallons and w["miles"] > 50:
        value = w["miles"] / gallons
        if MIN_VALID <= value <= MAX_VALID:
            track["est"] = round(value if track["est"] is None else 0.6 * track["est"] + 0.4 * value, 2)
            track["n"] += 1
        track["win"] = {"start_fuel": cur["fuel"], "miles": 0.0, "last": cur}
    return track["est"]
