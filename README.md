# FleetGO Low-Fuel Alert Bot

Every 5 minutes the bot signs in to the **FleetGO website**, reads the Fleet page (fuel %, GPS,
last update for all trucks) and posts to a Telegram group. **No Motive API key needed.**
(If you get a Motive API key later, set `DATA_SOURCE=motive` and it reads Motive directly instead.)

The people tagged are set in the `MONITOR_TAGS` variable.

| Fuel | What the bot does |
|---|---|
| below 40% | 🟡 Alert in the group (no tag) |
| 30% or less | 🟠 Alert + tags FleetGO monitoring |
| 25% or less | 🔴 URGENT alert + tag, **repeated every 60 min** until fueled |
| back to 50%+ | ✅ "Unit X refueled" and the truck is reset |

Each alert includes the **estimated range** as low–high miles. The bot **learns each truck's real MPG**
from its own GPS miles and fuel used (after ~30 gal burned; hills, heavy loads and driving style are
included). Until then it uses 6.5 mpg. The low end assumes 15% worse MPG (`HARD_ROAD_FACTOR=0.85`),
and "fuel within" keeps a 10% reserve on top of that and a ⛔ warning when the truck is in a black state (CA, MA, NH, OR, WA).

### Company drivers only
Fuel alerts (low fuel, black state, theft) go only to **company drivers**. The bot reads the driver
type from the group name: `(C/D)` company driver, `(O/O)` owner-operator, `(L/O)` lease operator,
e.g. "2304 Scotty (C/D)". You can also type `/unit 2304 C/D` in the group. O/O and L/O groups get
no fuel alerts (they buy their own fuel) but still get HV load rules. Groups with no type in the
name still get fuel alerts, so no truck is missed (`ALERT_UNKNOWN_TYPE=0` turns that off).

### Possible fuel theft
Every check compares the fuel drop with the miles actually driven (1 gal ≈ 6.5 mi). If fuel fell
**10+ % points more than driving explains** (e.g. 62% → 41% while parked) and the next reading
confirms it, the bot sends 🚨 **POSSIBLE FUEL THEFT** with the gallons lost and location, and tags
monitoring. Theft alerts go **only** to the private monitoring group (`TELEGRAM_CHAT_ID`), never to
the driver group (if `TELEGRAM_CHAT_ID` is not set, they fall back to the driver group). That group
gets nothing else unless `FALLBACK_TO_MONITORING=1`.

### Black states ahead
Every load message (`🚛 NEW TRIP ... HAS BEEN ASSIGNED`) is tracked stop by stop. When the truck's
**next stop is in a black state**, the truck is in a **neighboring state** (or within 300 mi of the stop)
and fuel is **below 85%**, the bot tags monitoring: "⛔ Unit X is heading to CA (black state) – fill up
before entering CA". If fuel is still low an hour later, it sends a 🔴 reminder (max 2). Every truck gets each alert only once. Trucks with GPS data older
than 3 hours are skipped.

Example:

```
🟠 Low fuel: Unit 2304 – 29%
📍 5.3 mi NE of Shawnee, OK
💳 Fuel card discount: TA, Love's, Pilot, Flying J
⛽ Plan fuel from here
🔔 @your_monitoring_team
```

Driver names are filled in from the FleetGO "Plan a route" driver list (the Fleet page shows "No driver").

---

## High-value (HV) loads

The bot is a member of every **driver group** and reads new messages:

| Message in driver group | What the bot does |
|---|---|
| `🚛 NEW TRIP 102928 HAS BEEN ASSIGNED` | Saves trip #, pickup stop, address, appointment |
| `❗️HIGH-VALUE LOAD` (before or after the trip message, within 6 h) | Marks that trip as **HV** |
| Regular load (no HV message) | Nothing special, normal fuel alerts |
| `Loaded`, `Picked up`, `PU done`, `BOL` | Pickup confirmed (backup to GPS) |

For an HV load:

| When | Alert |
|---|---|
| Fuel below **75%** before pickup | ⚠️ "fuel BEFORE arriving at the shipper" + tag |
| Still below 75% within **60 mi** of the shipper | 🔴 URGENT + tag |
| Truck leaves the shipper (GPS) or driver says "Loaded" | 🚚 "300 mi NONSTOP starts" |
| Truck **stands still ~10 min** before mile 300 | 🛑 "HV truck STOPPED at mile X" + tag |
| Low-fuel alert during the 300 mi | Says **"NO STOP – X mi left"** instead of a fuel-stop link |
| Fuel not enough to finish the nonstop miles | 🔴 + tag |
| Mile 300 reached | ✅ "Normal fuel stops allowed" |

Miles are counted from the truck's GPS every check. The shipper address is located with the free
US Census address lookup (if the address can't be found, the ZIP code is used).

### Where alerts go
By default (`ALERT_MODE=driver_groups`) every alert is **one message in that driver's own group**.
The monitoring team is a member of each driver group and is **tagged** (`MONITOR_TAGS`) at 30%, 25%
and on HV problems. No separate monitoring group is needed; `TELEGRAM_CHAT_ID` is only a fallback
for trucks whose group isn't linked yet. (`ALERT_MODE=separate` sends tagged alerts to a monitoring
group instead and a plain message to the driver.)

### Messages in the driver group

| Situation | Driver sees |
|---|---|
| Fuel under 40% | 🟡 "Fuel is 36%. Please plan your next fuel stop." |
| 30% or less | 🟠 "Low fuel: 29%. Please fuel at the next TA, Love's, Pilot, Flying J and send us the location." |
| 25% or less | 🔴 "URGENT: fuel is 22%. Fuel at the nearest TA, Love's, Pilot, Flying J now." (repeats hourly) |
| HV, fuel under 75% | ⚠️ "Your fuel is 50%. It must be ABOVE 75% before pickup at ISH-OAK." |
| HV picked up | 🚚 "Roll 300 miles NONSTOP. I will tell you when you reach mile 300." |
| HV truck stopped | 🛑 "Please keep rolling – about 168 mi left. If you must stop, tell us right away." |
| HV low fuel during nonstop | "Do NOT stop yet – the monitoring team will contact you about fuel." |
| HV mile 300 | ✅ "You may stop now. Park in a safe area, trailer against a wall." |

If the HV truck's fuel is too low to finish the nonstop miles, **only monitoring** is told, so the team decides
what the driver should do. Set `DRIVER_ALERTS=0` to send alerts to the monitoring group only.

**Which truck is which group?** The bot finds the unit number in the group name (e.g. "2304 SCOTTY",
"Unit 2304"). If a group name has no unit number, type `/unit 2304` once in that group.

---

## Setup (about 20 minutes)

### 1. Create the Telegram bot
1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name, e.g. `Fleet Fuel Alert`.
2. Copy the **token** it gives you (looks like `123456:ABC...`). Keep it secret.
3. Add the bot to your monitoring group and send any message in the group.
4. Open this link in a browser (put your token in):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find `"chat":{"id":-100...` and copy that number. This is the **chat id**.
5. **So the bot can read driver groups:** in @BotFather send `/setprivacy`, choose your bot,
   then **Disable**. Then add the bot to **every driver group** (it only sees messages sent after it joins).
   The bot links a group to a truck from the first message it sees there, so after adding it,
   type `/unit 2304` (that group's unit) in each group to link it right away.
   After step 4, do not open the getUpdates link again while the bot is running, or it may miss messages.

### 2. FleetGO login for the bot
The bot needs a FleetGO email + password. **Best: ask a FleetGO admin to invite a separate account
just for the bot** (e.g. a new Gmail), so your own password is never stored anywhere and the bot can be
switched off without touching your account. You type the email/password only into GitHub Secrets (step 3).

### 3. Put the bot on GitHub (free, runs 24/7)
1. Create a free account at github.com.
2. Create a **new repository**, e.g. `fuel-alert-bot`.
   * **Public** repo = unlimited free run time. The code has no secrets and the logs only say
     "checked 39 trucks, sent 2 alerts", so this is safe.
   * Private repo = only 2,000 free minutes/month, which is **not enough** for every 5 minutes.
3. Upload these files: `fuel_alert_bot.py`, `fleetgo_source.py`, `hv_loads.py`, `.gitignore`, and the folder `.github/workflows/fuel-alert.yml`
   (on the repo page: **Add file → Upload files**; to create the workflow, use
   **Add file → Create new file** and type the name `.github/workflows/fuel-alert.yml`, then paste the content).
4. Go to **Settings → Secrets and variables → Actions**:
   * **Secrets** tab → *New repository secret*:
     `FLEETGO_EMAIL`, `FLEETGO_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
     (Secrets are hidden: nobody can read them back, not even in a public repo.)
   * **Variables** tab → *New repository variable* (optional):
     `MONITOR_TAGS` = Telegram usernames to tag, e.g. `@person1 @person2`
     `FLEETGO_URL` = the FleetGO website address
     `FLEETGO_COMPANY_ID` = the company to select in FleetGO
     `IGNORE_UNITS` = e.g. `2602,2026` (trucks in the shop; optional)
5. Go to **Actions → Low-fuel alert → Run workflow** to test it now.

From then on it runs by itself. Each GitHub run stays on for ~5.5 hours and checks every
5 minutes; GitHub is asked to start a run every 30 minutes, and while one is running the next
waits in line and takes over when it ends. (GitHub's own 5-minute schedule is often late, so the
bot doesn't depend on it.) You'll see many "cancelled" waiting runs in the Actions list - that's
normal. "Run workflow" by hand also waits in line while a run is active.

> GitHub pauses scheduled jobs in a public repo after **60 days with no commits**. It emails you first;
> just click "Enable workflow" again, or make a small edit to the README every month.

---

## Run on a Windows PC instead (optional)
1. Install Python from python.org, then run:
   `pip install playwright` and `python -m playwright install chromium`
2. Copy `.env.example` to `.env` and fill in the values.
3. Test without sending anything: `python fuel_alert_bot.py --sample sample_vehicles.json --dry-run`
4. Run for real: `python fuel_alert_bot.py --loop`

## Change the rules
All levels are settings (see `.env.example`): `WATCH_PERCENT`, `TAG_PERCENT`, `CRITICAL_PERCENT`,
`RESET_PERCENT`, `REMIND_MINUTES`, `STALE_HOURS`, `NEVER_FUEL_STATES`, `FUEL_CARD_CHAINS`.
On GitHub, add them under **Variables** and add a matching line in the workflow's `env:` block.
