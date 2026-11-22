"""
Read truck fuel/GPS from the FleetGO website itself (no Motive API key needed).

A headless Chrome (Playwright) signs in to FleetGO with the account in
FLEETGO_EMAIL / FLEETGO_PASSWORD, selects the company, opens the Fleet page and
reads the same data the page shows. On a PC the sign-in is saved to
fleetgo_session.json so the bot does not log in on every check (SAVE_SESSION=1).
On GitHub it signs in fresh each run, so no login token is ever stored there.

Install once:  pip install playwright  &&  python -m playwright install chromium
"""

import json
import os
import re

SESSION_FILE = "fleetgo_session.json"


def _decode(node):
    """Decode the website's data format (seroval) into normal Python values."""
    if not isinstance(node, dict):
        return node
    t = node.get("t")
    if t in (0, 1):                       # number / string
        return node.get("s")
    if t == 2:                            # constants
        return {2: True, 3: False}.get(node.get("s"))   # 0/1 = null/undefined
    if t == 9:                            # array
        return [_decode(x) for x in node.get("a", [])]
    if t == 10:                           # object
        p = node.get("p") or {}
        return dict(zip(p.get("k", []), (_decode(v) for v in p.get("v", []))))
    return None


def _find_vehicles(value):
    """Find the list of vehicles anywhere in the decoded response."""
    if isinstance(value, dict):
        if isinstance(value.get("vehicles"), list):
            return value["vehicles"]
        for v in value.values():
            found = _find_vehicles(v)
            if found is not None:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _find_vehicles(v)
            if found is not None:
                return found
    return None


def _state_from(description):
    m = re.search(r",\s*([A-Z]{2})\s*$", description or "")
    return m.group(1) if m else None


def _login(page, base, email, password):
    page.goto(f"{base}/auth", wait_until="networkidle")
    page.locator('input[type="email"], input[name="email"]').first.fill(email)
    page.locator('input[type="password"]').first.fill(password)
    page.get_by_role("button", name=re.compile("sign in", re.I)).click()
    page.wait_for_url(lambda url: "/auth" not in url, timeout=30000)


def _driver_names(page, base):
    """Map unit -> driver name from the 'Plan a route' driver list ("NAME · Unit 338")."""
    names = {}
    try:
        page.goto(f"{base}/plan-route", wait_until="networkidle")
        text = page.locator("main").inner_text(timeout=15000)
        for name, unit in re.findall(r"^(.+?) · Unit (\S+)\s*$", text, re.M):
            names[unit.strip()] = name.strip()
    except Exception as exc:
        print("Could not read driver list:", exc)
    return names


def fetch_fleetgo_trucks(s):
    """Return a list of trucks: unit, driver, fuel, lat, lon, place, state, located_at (ISO)."""
    from playwright.sync_api import sync_playwright

    if not (s.fleetgo_email and s.fleetgo_password):
        raise SystemExit("Set FLEETGO_EMAIL and FLEETGO_PASSWORD first.")
    base = s.fleetgo_url.rstrip("/")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        use_saved = s.save_session and os.path.exists(SESSION_FILE)
        ctx_args = {"storage_state": SESSION_FILE} if use_saved else {}
        context = browser.new_context(**ctx_args)
        # Select the company (the site keeps it in localStorage)
        context.add_init_script(
            f"localStorage.setItem('fleetgo.selectedCompany', {json.dumps(s.fleetgo_company_id)});"
        )
        page = context.new_page()

        captured = []

        def on_response(resp):
            if "/_serverFn/" in resp.url and resp.status == 200:
                try:
                    vehicles = _find_vehicles(_decode(json.loads(resp.text())))
                    if vehicles and isinstance(vehicles[0], dict) and "fuelPct" in vehicles[0]:
                        captured.append(vehicles)
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(f"{base}/fleet", wait_until="networkidle")
        if "/auth" in page.url:
            _login(page, base, s.fleetgo_email, s.fleetgo_password)
            page.goto(f"{base}/fleet", wait_until="networkidle")
        page.wait_for_timeout(3000)
        if not captured:
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(5000)
        if not captured:
            raise RuntimeError("Could not read the Fleet page data (site may have changed).")

        drivers = _driver_names(page, base)
        if s.save_session:
            context.storage_state(path=SESSION_FILE)
        browser.close()

    trucks = []
    for v in captured[-1]:
        unit = str(v.get("unit") or "")
        lat, lon = v.get("lat"), v.get("lng")
        trucks.append({
            "unit": unit,
            "driver": (v.get("driver") or "").strip() or drivers.get(unit),
            "fuel": float(v["fuelPct"]) if isinstance(v.get("fuelPct"), (int, float)) else None,
            "lat": lat if isinstance(lat, (int, float)) else None,
            "lon": lon if isinstance(lon, (int, float)) else None,
            "place": v.get("locationDesc") or "Unknown location",
            "state": _state_from(v.get("locationDesc")),
            "located_at": v.get("locatedAt") or None,
        })
    return trucks
