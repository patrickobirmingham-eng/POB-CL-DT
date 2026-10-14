"""
Builds docs/spreads.html, the Credit Spreads dashboard (the second tab next to the
day-trading dashboard in docs/index.html).

Data:
  - spreads_state.json   open spreads, the bot's recent decisions, today's skips
  - spreads_trades.csv   every closed spread
  - spreads_settings.json the rules (shown, and editable, in the Rules box)
  - Alpaca (SPREADS_* keys): account, fills/expirations/assignments, daily P&L

Everything is embedded in the page as JSON; filters, tiles and sorting run in the
browser. Alpaca calls are optional: if the keys are missing or a call fails the
page is still built from the local files.

Usage: python spreads_dashboard.py
"""
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "docs", "spreads.html")
PAPER_URL = "https://paper-api.alpaca.markets"
# The Rules box saves through server/spreads-settings-save.php on the owner's web
# server (next to orb-settings-save.php, which does the same for the day bot).
# The passcode is typed on every save and never stored in this page.
SAVE_URL = os.getenv("SPREADS_SETTINGS_SAVE_URL", "https://stunster.com/spreads-settings-save.php")


def read_json(name, default):
    try:
        with open(os.path.join(HERE, name)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def read_trades():
    try:
        with open(os.path.join(HERE, "spreads_trades.csv")) as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


class Api:
    def __init__(self):
        key, secret = os.getenv("SPREADS_APCA_API_KEY_ID"), os.getenv("SPREADS_APCA_API_SECRET_KEY")
        self.ok = bool(key and secret)
        self.h = {"APCA-API-KEY-ID": key or "", "APCA-API-SECRET-KEY": secret or ""}

    def get(self, path, **params):
        r = requests.get(PAPER_URL + path, headers=self.h, params=params, timeout=20)
        r.raise_for_status()
        return r.json()


def parse_occ(sym):
    tail = sym[-15:]
    return sym[:-15], datetime.strptime(tail[:6], "%y%m%d").date().isoformat(), tail[6], int(tail[7:]) / 1000.0


def et_date(ts):
    """Alpaca timestamp (ISO, UTC) -> ET calendar date string."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET).date().isoformat()


def activities(api):
    """All option fills, expirations, assignments and fees on the account, oldest first."""
    out, token = [], None
    for _ in range(50):
        params = {"activity_types": "FILL,OPEXP,OPASN,OPEXC,FEE", "direction": "asc", "page_size": 100}
        if token:
            params["page_token"] = token
        page = api.get("/v2/account/activities", **params)
        if not page:
            break
        out += page
        if len(page) < 100:
            break
        token = page[-1]["id"]
    return out


def transactions(acts):
    """Activities -> one row per leg, labelled like a brokerage statement."""
    rows, pos, fees = [], {}, []
    for a in acts:
        kind = a.get("activity_type")
        sym = a.get("symbol") or ""
        if kind == "FEE":
            fees.append({"dt": a.get("date", "")[:10], "amt": float(a.get("net_amount") or 0)})
            continue
        if len(sym) < 16:
            continue                    # only option contracts belong on this page
        und, exp, cp, strike = parse_occ(sym)
        qty = abs(float(a.get("qty") or 0))
        if kind == "FILL":
            side = 1 if a.get("side") == "buy" else -1
            before = pos.get(sym, 0)
            opening = before == 0 or (before > 0) == (side > 0)
            act = ("Buy to Open" if opening else "Buy to Close") if side > 0 else \
                  ("Sell to Open" if opening else "Sell to Close")
            pos[sym] = before + side * qty
            px = float(a.get("price") or 0)
            rows.append({"dt": et_date(a["transaction_time"]), "act": act, "sym": und, "leg": f"{strike:g} {cp}",
                         "exp": exp, "q": qty, "px": round(-side * px, 2), "fee": 0.0})
        else:
            act = {"OPEXP": "Expired", "OPASN": "Assigned", "OPEXC": "Exercised"}.get(kind, kind)
            pos[sym] = 0
            rows.append({"dt": a.get("date", "")[:10], "act": act, "sym": und, "leg": f"{strike:g} {cp}",
                         "exp": exp, "q": qty, "px": 0.0, "fee": 0.0})
    for r in rows:
        r["amt"] = round(r["px"] * 100 * r["q"] - r["fee"], 2)
    return rows, fees


def daily_pl(api):
    h = api.get("/v2/account/portfolio/history", period="3M", timeframe="1D")
    out = []
    for ts, pl in zip(h.get("timestamp") or [], h.get("profit_loss") or []):
        if pl is None:
            continue
        out.append({"d": datetime.fromtimestamp(ts, timezone.utc).astimezone(ET).date().isoformat(), "v": round(pl, 2)})
    while out and out[0]["v"] == 0:     # drop the flat days before the first trade
        out.pop(0)
    return out[-30:]


def spread_rows(state, trades, settings):
    rows = []
    for sp in state.get("spreads", {}).values():
        cost = sp.get("last_cost")
        rows.append({"sym": sp["symbol"], "sec": settings["WATCHLIST"].get(sp["symbol"], sp.get("sector", "")),
                     "sk": sp["short_strike"], "lk": sp["long_strike"], "o": sp["opened_at"], "e": sp["expiration"],
                     "q": int(sp["qty"]), "d": float(sp.get("delta") or 0), "cr": float(sp["credit"]),
                     "px": float(cost) if cost is not None else float(sp["credit"]), "c": "", "why": "",
                     "price": sp.get("last_price"), "rolls": sp.get("rolls", 0)})
    for t in trades:
        why = t["reason"]
        why = "touched" if why.startswith("touched") else why
        rows.append({"sym": t["symbol"], "sec": settings["WATCHLIST"].get(t["symbol"], t.get("sector", "")),
                     "sk": float(t["short_strike"]), "lk": float(t["long_strike"]), "o": t["opened_at"],
                     "e": t["expiration"], "q": int(float(t["qty"])), "d": float(t["delta_at_open"] or 0),
                     "cr": float(t["credit"]), "px": float(t["close_cost"]), "c": t["closed_at"], "why": why})
    return rows


def build():
    settings = read_json("spreads_settings.json", {"WATCHLIST": {}})
    state = read_json("spreads_state.json", {})
    trades = read_trades()
    api = Api()
    acct, tx, fees, daily, errors = None, [], [], [], []
    if api.ok:
        try:
            a = api.get("/v2/account")
            acct = {"equity": float(a["equity"]), "cash": float(a["cash"]),
                    "last_equity": float(a.get("last_equity") or a["equity"]),
                    "bp": float(a.get("options_buying_power") or a.get("buying_power") or 0)}
        except Exception as e:
            errors.append(f"account: {e}")
        try:
            tx, fees = transactions(activities(api))
        except Exception as e:
            errors.append(f"activities: {e}")
        try:
            daily = daily_pl(api)
        except Exception as e:
            errors.append(f"portfolio history: {e}")
    rules = {k: v for k, v in settings.items() if not k.startswith("_")}
    now = datetime.now(ET)
    data = {"generated": now.isoformat(timespec="seconds"), "today": now.date().isoformat(),
            "account": acct, "spreads": spread_rows(state, trades, settings), "tx": tx, "fees": fees,
            "daily": daily, "events": state.get("events", [])[-40:][::-1], "skipped": state.get("skipped", []),
            "skipped_day": state.get("day", ""), "rules": rules, "errors": errors, "save_url": SAVE_URL}
    html = TEMPLATE.replace("__DATA__", json.dumps(data, default=str).replace("</", "<\\/"))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write(html)
    print(f"wrote {OUT}: {len(data['spreads'])} spreads, {len(tx)} transactions, {len(daily)} days"
          + (f", errors: {errors}" if errors else ""))


TEMPLATE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<script>try{if(localStorage.getItem("orb-dashboard-theme")==="light")document.documentElement.setAttribute("data-theme","light")}catch(e){}</script>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Credit Spreads</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<style>
:root{--bg:#0a0d14;--panel:#111621;--border:#1f2637;--border-strong:#2b344a;--text:#e8ebf2;--muted:#8791a7;--faint:#5d667b;--pos:#22c55e;--neg:#f0504e;--accent:#4f8cff;--accent-soft:rgba(79,140,255,.14);--row-hover:rgba(255,255,255,.03);--grid:rgba(255,255,255,.06);--warn:#f5a524}
[data-theme="light"]{--bg:#f3f5f9;--panel:#fff;--border:#e3e8f0;--border-strong:#cfd6e3;--text:#131a29;--muted:#5a6479;--faint:#8a93a6;--pos:#12833f;--neg:#c62828;--accent:#1f5fe0;--accent-soft:rgba(31,95,224,.10);--row-hover:rgba(15,30,60,.035);--grid:rgba(15,30,60,.08);--warn:#c46a00}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;font-size:15px;line-height:1.45}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--border);padding:14px max(32px,calc((100% - 1736px)/2));display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:18px;margin:0;font-weight:650}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.brand-mark{width:32px;height:32px;border-radius:8px;flex-shrink:0;background:linear-gradient(135deg,var(--accent),#7c5cff);display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px;font-weight:700}
@media (max-width:720px){.brand-mark{width:28px;height:28px;font-size:14px}}
.tabs{display:flex;gap:4px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:3px}
.tabs a{padding:6px 14px;border-radius:6px;color:var(--muted);text-decoration:none;font-weight:550;font-size:14px}
.tabs a.on{background:var(--accent-soft);color:var(--accent)}
.tabs a:not(.on):hover{color:var(--text)}
.right{margin-left:auto;display:flex;gap:8px;align-items:center;color:var(--muted);font-size:13px;flex-wrap:wrap}
.pill{border:1px solid var(--border-strong);border-radius:999px;padding:3px 10px;font-size:12px;font-weight:600}
.pill.paper{color:var(--warn);border-color:var(--warn)}
.btn{background:var(--panel);color:var(--text);border:1px solid var(--border-strong);border-radius:8px;padding:7px 13px;font-size:14px;cursor:pointer;font-weight:600;font-family:inherit}
.btn:hover{border-color:var(--accent)}
.banner{background:rgba(240,80,78,.12);color:var(--neg);text-align:center;padding:6px 16px;font-size:12px;font-weight:600}
main{max-width:1800px;margin:0 auto;padding:28px 32px 40px}
.acct{display:flex;flex-wrap:wrap;gap:8px 28px;justify-content:center;margin-bottom:16px;color:var(--muted);font-size:16px}
.acct b{color:var(--text);font-variant-numeric:tabular-nums}
.filters{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:16px;display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;justify-content:center}
.fgroup{display:flex;flex-direction:column;align-items:center;gap:6px;max-width:100%}
.fgroup>label,.fgroup>.flabel{font-size:14px;font-weight:600}
.checks{display:flex;flex-wrap:wrap;gap:4px 14px;border:1px solid var(--border-strong);border-radius:8px;padding:7px 12px;justify-content:center}
.checks label{display:flex;gap:6px;align-items:center;font-size:14px;cursor:pointer;white-space:nowrap}
.checks input{accent-color:var(--accent);width:15px;height:15px}
select,input[type=date],input[type=search]{background:var(--panel);color:var(--text);border:1px solid var(--border-strong);border-radius:6px;padding:7px 11px;font-size:14px;font-family:inherit}
input[type=search]{width:260px;max-width:100%}
.groups{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;margin-bottom:20px}
@media (min-width:1700px){.groups{grid-template-columns:repeat(4,1fr)}}
@media (max-width:640px){.groups{grid-template-columns:1fr}}
.gtitle{font-size:13px;font-weight:700;letter-spacing:.06em;color:var(--muted);display:flex;align-items:center;gap:10px;margin-bottom:8px}
.gtitle:after{content:"";flex:1;height:1px;background:var(--border)}
.gtiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.tile{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 8px;text-align:center;min-height:104px;display:flex;flex-direction:column;justify-content:space-between}
.tile .k{color:var(--muted);font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;line-height:1.3}
.tile .v{font-size:24px;font-weight:650;letter-spacing:-.02em;margin-top:8px;white-space:nowrap;font-variant-numeric:tabular-nums}
.pos{color:var(--pos)} .neg{color:var(--neg)} .warn{color:var(--warn)} .muted{color:var(--muted)}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px 22px;margin-bottom:20px;min-width:0}
.phead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.phead h2{font-size:14px;margin:0;font-weight:650;text-transform:uppercase;letter-spacing:.07em;display:flex;align-items:center;gap:10px}
.phead h2:before{content:"";width:3px;height:15px;border-radius:2px;background:var(--accent)}
.phead .sub{color:var(--muted);font-size:13px}
.phead input{margin-left:auto}
.row2{display:grid;grid-template-columns:2fr 1fr;gap:20px}
@media (max-width:1000px){.row2{grid-template-columns:1fr}}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em;text-align:center;padding:10px 8px;border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--text)}
th .arr{font-size:10px;margin-left:3px;color:var(--accent)}
td{padding:11px 8px;font-size:14.5px;border-bottom:1px solid var(--border);text-align:center;white-space:nowrap}
th.l,td.l{text-align:left}
tr:hover td{background:var(--row-hover)}
.sym{font-weight:650}
.bar{position:relative;width:150px;height:8px;border-radius:4px;background:linear-gradient(90deg,rgba(240,80,78,.35) 0,rgba(240,80,78,.35) 80%,rgba(34,197,94,.35) 80%,rgba(34,197,94,.35) 100%);display:inline-block;vertical-align:middle}
.bar i{position:absolute;top:-3px;width:3px;height:14px;border-radius:2px;background:var(--text)}
.bar b{position:absolute;top:-3px;left:80%;width:1px;height:14px;background:var(--faint)}
.lbl{display:flex;justify-content:space-between;width:150px;font-size:10px;color:var(--faint);margin:2px auto 0}
.tag{display:inline-block;font-size:11px;font-weight:600;border-radius:4px;padding:2px 6px;background:rgba(135,145,167,.15);color:var(--muted)}
.tag.target{background:rgba(34,197,94,.15);color:var(--pos)}
.tag.stop{background:rgba(240,80,78,.15);color:var(--neg)}
.tag.rolled{background:var(--accent-soft);color:var(--accent)}
.tag.touched{background:rgba(245,165,36,.15);color:var(--warn)}
.sector{display:grid;grid-template-columns:110px 1fr 40px;gap:8px;align-items:center;margin:8px 0;font-size:13px}
.sector .track{height:10px;background:var(--grid);border-radius:5px;overflow:hidden}
.sector .fill{height:100%;background:var(--accent);border-radius:5px}
.sector .fill.full{background:var(--warn)}
.note{color:var(--faint);font-size:13px;margin-top:8px}
.empty{color:var(--faint);text-align:center;padding:14px}
.log{list-style:none;margin:0;padding:0;max-height:360px;overflow-y:auto;font-size:14px}
.log li{padding:6px 0;border-bottom:1px solid var(--border);display:flex;gap:10px}
.log li span:first-child{color:var(--faint);white-space:nowrap;font-variant-numeric:tabular-nums}
svg text{fill:var(--muted);font-size:11px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:flex-start;justify-content:center;padding:40px 16px;z-index:20}
.modal.on{display:flex}
.modal .box{background:var(--panel);border:1px solid var(--border-strong);border-radius:12px;padding:20px;max-width:720px;width:100%;max-height:88vh;overflow-y:auto}
.modal h2{margin:0;font-size:18px}
.mhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.mhead .x{background:none;border:none;color:var(--muted);font-size:26px;cursor:pointer;line-height:1}
.rgroup{border-top:1px solid var(--border);padding:12px 0 6px}
.rgroup h3{margin:0 0 8px;font-size:12px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
.rrow{display:flex;align-items:center;gap:10px;margin:7px 0;flex-wrap:wrap}
.rrow label{flex:1;min-width:200px;font-size:14px}
.rrow label small{display:block;color:var(--faint);font-size:12px}
.rrow input[type=number],.rrow input[type=time],.wt input{width:110px;background:var(--bg);color:var(--text);border:1px solid var(--border-strong);border-radius:6px;padding:6px 8px;font-size:14px;font-family:inherit}
.rrow .suf{color:var(--muted);font-size:13px;min-width:44px}
.rrow input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent)}
.wt{width:auto}.wt td{padding:4px;border:none}.wt input{width:90px}.wt th{cursor:default;border:none}
#wlText{width:100%;min-height:220px;background:var(--bg);color:var(--text);border:1px solid var(--border-strong);border-radius:6px;padding:8px;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}
.savebar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;border-top:1px solid var(--border);padding-top:14px;margin-top:8px;position:sticky;bottom:-20px;background:var(--panel);padding-bottom:6px}
.savebar input{background:var(--bg);color:var(--text);border:1px solid var(--border-strong);border-radius:6px;padding:7px 10px;font-size:14px;width:180px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn:disabled{opacity:.5;cursor:default}
.savestatus{font-size:14px;margin-top:8px;min-height:20px}
.savestatus.ok{color:var(--pos)}.savestatus.err{color:var(--neg)}.savestatus.info{color:var(--muted)}
@media (max-width:720px){body{font-size:14px}main{padding:16px}header{padding:10px 16px}.panel{padding:14px 12px}.tile .v{font-size:19px}td{font-size:13px}.right{margin-left:0;width:100%}.phead input{margin-left:0;width:100%}.tabs{order:3;width:100%}.tabs a{flex:1;text-align:center}}
</style>
</head>
<body>
<div class="banner" id="errbanner" style="display:none"></div>
<header>
  <div class="brand"><div class="brand-mark">&#9650;</div><h1>Claude.AI Paper Trading</h1></div>
  <nav class="tabs" aria-label="Dashboards"><a href="index.html">Day Trading</a><a href="spreads.html" class="on">Credit Spreads</a></nav>
  <div class="right">
    <span id="updated"></span>
    <button class="btn" onclick="location.reload()">&#8635; Refresh</button>
    <span class="pill paper">PAPER</span>
    <button class="btn" onclick="document.getElementById('rules').classList.add('on')">&#9881; Rules</button>
    <button class="btn" id="themeBtn" onclick="toggleTheme()"></button>
  </div>
</header>
<main>
  <div class="acct" id="acct"></div>
  <div class="filters">
    <div class="fgroup"><span class="flabel">1. Status Filter</span>
      <div class="checks" id="status"></div></div>
    <div class="fgroup"><label for="range">2. Quick Range</label>
      <select id="range">
        <option value="all">Show All</option><option value="today">Today</option><option value="last7">Last 7 Days</option><option value="month">Current Month</option><option value="prevmonth">Last Month</option><option value="ytd">Current Year</option><option value="lastyear">Last Year</option><option value="last3m">Last 3 Months</option><option value="last6m">Last 6 Months</option><option value="last12m">Last 12 Months</option><option value="custom">Custom</option>
      </select></div>
    <div class="fgroup"><label for="from">3. From Date</label><input type="date" id="from"></div>
    <div class="fgroup"><label for="to">4. To Date</label><input type="date" id="to"></div>
    <button class="btn" id="reset">&#8634; Reset Filters</button>
  </div>

  <div class="groups" id="groups"></div>

  <div class="panel">
    <div class="phead"><h2>Open spreads</h2><span class="sub" id="opensub"></span>
      <input type="search" id="qOpen" placeholder="Search stock or sector…"></div>
    <div class="tw"><table id="open"></table></div>
    <div class="note" id="opennote"></div>
  </div>

  <div class="row2">
    <div class="panel">
      <div class="phead"><h2>Daily profit / (loss)</h2><span class="sub">whole account, from Alpaca</span></div>
      <div id="chart"></div>
    </div>
    <div class="panel">
      <div class="phead"><h2>Risk</h2><span class="sub">worst case if every open spread lost max</span></div>
      <div id="risk"></div>
      <div class="note" id="risknote"></div>
    </div>
  </div>

  <div class="panel">
    <div class="phead"><h2>Closed spreads</h2><span class="sub" id="closedsub"></span>
      <input type="search" id="qClosed" placeholder="Search stock, sector or reason…"></div>
    <div class="tw"><table id="closed"></table></div>
  </div>

  <div class="panel">
    <div class="phead"><h2>Transactions</h2><span class="sub" id="txsub"></span>
      <input type="search" id="qTx" placeholder="Search…"></div>
    <div class="tw"><table id="tx"></table></div>
  </div>

  <div class="row2">
    <div class="panel">
      <div class="phead"><h2>What the bot did</h2><span class="sub">latest first</span></div>
      <ul class="log" id="events"></ul>
    </div>
    <div class="panel">
      <div class="phead"><h2>Skipped today</h2><span class="sub">stocks it looked at but didn't trade</span></div>
      <ul class="log" id="skipped"></ul>
    </div>
  </div>
</main>
<div class="modal" id="rules" onclick="if(event.target===this)this.classList.remove('on')"><div class="box">
  <div class="mhead"><h2>Bot rules</h2><button class="x" onclick="document.getElementById('rules').classList.remove('on')" aria-label="Close">&times;</button></div>
  <div class="note" style="margin:0 0 12px">Change any rule and press Save. The bot uses the new rules from its next check (every 15 minutes). Spreads already open keep running under the new exit rules.</div>
  <div id="rulesform"></div>
  <div class="savebar">
    <input type="password" id="passcode" placeholder="Passcode" autocomplete="current-password">
    <button class="btn primary" id="saveBtn" onclick="saveRules()">Save</button>
    <button class="btn" onclick="renderRules(CURRENT)">Undo changes</button>
  </div>
  <div id="saveStatus" class="savestatus"></div>
</div></div>
<script>
const DATA=__DATA__;
function themeLabel(){const l=document.documentElement.getAttribute('data-theme')==='light';document.getElementById('themeBtn').innerHTML=l?'&#9790; Dark':'&#9728; Light'}
function toggleTheme(){const t=document.documentElement.getAttribute('data-theme')==='light'?'dark':'light';document.documentElement.setAttribute('data-theme',t);try{localStorage.setItem('orb-dashboard-theme',t)}catch(e){}themeLabel()}
themeLabel();
const R=DATA.rules, TP=R.TAKE_PROFIT_PCT||.5, SL=R.STOP_LOSS_MULT||2, MAXN=R.MAX_OPEN_SPREADS||20, MAXL=R.MAX_LOSS_PER_SPREAD||30000, MAXSEC=R.MAX_PER_SECTOR||5;
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money=(v,s=false,c=2)=>(v<0?'−':(s&&v>0?'+':''))+'$'+Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:c,maximumFractionDigits:c});
const tm=v=>Math.abs(v)>=1e4?money(v,false,0):money(v);
const m0=v=>money(v,false,0), cls=v=>v>0?'pos':v<0?'neg':'muted';
const D=s=>new Date(s+'T12:00:00'), fmt=d=>d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
const today=D(DATA.today), iso=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
$('updated').textContent='Last updated '+new Date(DATA.generated).toLocaleString('en-US',{month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit'});
if(DATA.errors.length){$('errbanner').style.display='block';$('errbanner').textContent='Some live data could not be loaded: '+DATA.errors.join(' · ')}
if(DATA.account){const a=DATA.account,ch=a.equity-a.last_equity;$('acct').innerHTML=`<span>Account value <b>${m0(a.equity)}</b></span><span>Today <b class="${cls(ch)}">${money(ch,true,0)}</b></span><span>Cash <b>${m0(a.cash)}</b></span><span>Options buying power <b>${m0(a.bp)}</b></span>`}
if(R.ENABLED===false)$('acct').innerHTML+='<span class="warn"><b class="warn">Bot is switched OFF in the rules</b></span>';
const spreads=DATA.spreads.map(s=>{
  const open=!s.c,w=s.sk-s.lk,days=Math.round(((open?today:D(s.c))-D(s.o))/864e5);
  return {...s,why:s.why||'',open,w,days,credit:s.cr*100*s.q,cost:s.px*100*s.q,pl:(s.cr-s.px)*100*s.q,ml:(w-s.cr)*100*s.q,dte:Math.round((D(s.e)-today)/864e5)};
});
const tx=DATA.tx;
const ACTS=['Buy to Close','Assigned','Called Away','Sell to Close','Expired','Sell to Open','Buy to Open'];
$('status').innerHTML=ACTS.map(a=>`<label><input type="checkbox" value="${a}" checked> ${a}</label>`).join('');
function setRange(v){
  const y=today.getFullYear(), m=today.getMonth(), ago=d=>new Date(today-d*864e5), monthsAgo=n=>new Date(y,m-n,today.getDate());
  let f,t=today;
  if(v==='all'){f=new Date(2000,0,1);t=new Date(2100,0,1)}
  else if(v==='today'){f=today}
  else if(v==='last7'){f=ago(6)}
  else if(v==='month'){f=new Date(y,m,1);t=new Date(y,m+1,0)}
  else if(v==='prevmonth'){f=new Date(y,m-1,1);t=new Date(y,m,0)}
  else if(v==='ytd'){f=new Date(y,0,1);t=new Date(y,11,31)}
  else if(v==='lastyear'){f=new Date(y-1,0,1);t=new Date(y-1,11,31)}
  else if(v==='last3m'){f=monthsAgo(3)}
  else if(v==='last6m'){f=monthsAgo(6)}
  else if(v==='last12m'){f=monthsAgo(12)}
  else return;
  $('from').value=iso(f);$('to').value=iso(t);
}
const sortState={tx:{k:0,dir:-1}};
function sortable(id,cols,rows,render,q){
  const st=sortState[id]||{};
  const term=($(q).value||'').toLowerCase();
  let r=rows.filter(x=>!term||cols.some(c=>String(c.key?x[c.key]:'').toLowerCase().includes(term)||(c.search&&c.search(x).toLowerCase().includes(term))));
  if(st.k!=null){const c=cols[st.k];r=[...r].sort((a,b)=>{const va=c.sort?c.sort(a):a[c.key],vb=c.sort?c.sort(b):b[c.key];return (va>vb?1:va<vb?-1:0)*st.dir})}
  const head='<tr>'+cols.map((c,i)=>`<th class="${c.l?'l':''}" data-i="${i}">${c.t}${st.k===i?`<span class="arr">${st.dir>0?'▲':'▼'}</span>`:''}</th>`).join('')+'</tr>';
  const t=$(id);
  t.innerHTML=head+(r.length?r.map(render).join(''):`<tr><td colspan="${cols.length}" class="empty">${rows.length?'No matches':'Nothing yet'}</td></tr>`);
  t.querySelectorAll('th').forEach(th=>th.onclick=()=>{const i=+th.dataset.i;sortState[id]={k:i,dir:st.k===i?-st.dir:1};draw()});
  return r;
}
// position of the spread between its stop (left) and profit target (right)
const prog=s=>(s.cr-s.px)/s.cr;
function bar(s){const lo=-SL,hi=TP,x=Math.max(0,Math.min(100,(prog(s)-lo)/(hi-lo)*100)),z=(0-lo)/(hi-lo)*100;return `<span class="bar" style="background:linear-gradient(90deg,rgba(240,80,78,.35) 0,rgba(240,80,78,.35) ${z}%,rgba(34,197,94,.35) ${z}%,rgba(34,197,94,.35) 100%)"><b style="left:${z}%"></b><i style="left:calc(${x}% - 1px)"></i></span><div class="lbl"><span>stop</span><span>open</span><span>target</span></div>`}
const sym=s=>`<span class="sym">${esc(s.sym)}</span> <span class="muted">${esc(s.sec)}</span>`;
const openCols=[
 {t:'Stock',key:'sym',l:1,search:s=>s.sec},{t:'Spread',key:'sk'},{t:'Opened',key:'o'},{t:'Expires',key:'e'},{t:'Qty',key:'q'},{t:'Delta at open',key:'d'},
 {t:'Total credit',key:'credit'},{t:'Total cost to close',key:'cost'},{t:'P&amp;L',key:'pl'},{t:'Stop ← → Target',sort:prog},{t:'Max loss',key:'ml'}];
const closedCols=[
 {t:'Stock',key:'sym',l:1,search:s=>s.sec},{t:'Spread',key:'sk'},{t:'Opened',key:'o'},{t:'Closed',key:'c'},{t:'Days held',key:'days'},{t:'Qty',key:'q'},
 {t:'Total credit',key:'credit'},{t:'Total cost to close',key:'cost'},{t:'P&amp;L',key:'pl'},{t:'Why',key:'why'}];
const txCols=[{t:'Date',key:'dt'},{t:'Action',key:'act'},{t:'Stock',key:'sym',l:1},{t:'Option',key:'leg'},{t:'Expires',key:'exp'},{t:'Qty',key:'q'},{t:'Price',key:'px'},{t:'Fees',key:'fee'},{t:'Amount',key:'amt'}];
function draw(){
  const from=$('from').value, to=$('to').value;
  const acts=[...document.querySelectorAll('#status input:checked')].map(i=>i.value);
  const inR=d=>d&&d>=from&&d<=to;
  const ftx=tx.filter(t=>inR(t.dt)&&acts.includes(t.act));
  const sum=(a,f)=>a.reduce((x,y)=>x+f(y),0);
  const opens=sum(ftx.filter(t=>t.act==='Sell to Open'||t.act==='Buy to Open'),t=>t.amt);
  const closes=sum(ftx.filter(t=>t.act==='Buy to Close'||t.act==='Sell to Close'),t=>t.amt);
  const openSp=spreads.filter(s=>s.open), closedR=spreads.filter(s=>!s.open&&inR(s.c)), openedR=spreads.filter(s=>inR(s.o));
  const realized=sum(closedR,s=>s.pl), openPrem=sum(openSp,s=>s.credit), assigned=sum(closedR.filter(s=>s.why==='assigned'),s=>s.pl);
  const avgPrem=openedR.length?sum(openedR,s=>s.credit)/openedR.length:0;
  const captured=sum(closedR,s=>s.credit)?realized/sum(closedR,s=>s.credit)*100:0;
  const fees=-sum(DATA.fees.filter(f=>inR(f.dt)),f=>f.amt)+sum(ftx,t=>t.fee), days=closedR.length?sum(closedR,s=>s.days)/closedR.length:0;
  const tile=(k,v,c)=>`<div class="tile"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`;
  const group=(t,ts)=>`<div><div class="gtitle">${t}</div><div class="gtiles">${ts.join('')}</div></div>`;
  $('groups').innerHTML=
   group('CASH FLOW',[tile('Sell to Open + Buy to Open',tm(opens),cls(opens)),tile('Buy to Close + Sell to Close',tm(closes),cls(closes)),tile('Cash Flow',tm(opens+closes),cls(opens+closes))])+
   group('PROFIT &amp; LOSS',[tile('Realized Profit / Loss',tm(realized),cls(realized)),tile('Open Premium',tm(openPrem),'warn'),tile('Assigned Premium',tm(assigned),'warn')])+
   group('PERFORMANCE',[tile('Avg Premium per Trade',tm(avgPrem),'pos'),tile('% Premium Captured',closedR.length?captured.toFixed(1)+'%':'—',captured>=50?'pos':closedR.length?'neg':'muted'),tile('Commission Drag',tm(fees),'warn')])+
   group('ACTIVITY',[tile('Open / Closed Positions',`${openSp.length} / ${closedR.length}`,'warn'),tile('Open / Closed Contracts',`${sum(openSp,s=>s.q)} / ${sum(closedR,s=>s.q)}`,'warn'),tile('Avg Days Held',closedR.length?days.toFixed(1)+'d':'—','warn')]);
  sortable('open',openCols,openSp,s=>`<tr><td class="l">${sym(s)}${s.rolls?` <span class="tag rolled" title="rolled ${s.rolls}×">rolled</span>`:''}</td><td>${s.sk}/${s.lk} put</td><td>${fmt(D(s.o))}</td><td>${fmt(D(s.e))} <span class="muted">(${s.dte}d)</span></td><td>${s.q}</td><td>${s.d.toFixed(2)}</td><td>${m0(s.credit)}</td><td>${m0(s.cost)}</td><td class="${cls(s.pl)}">${money(s.pl,true,0)}</td><td>${bar(s)}</td><td>${m0(s.ml)}</td></tr>`,'qOpen');
  $('opensub').textContent=`${openSp.length} of ${MAXN} · ${m0(openPrem)} credit collected`;
  sortable('closed',closedCols,closedR,s=>`<tr><td class="l">${sym(s)}</td><td>${s.sk}/${s.lk} put</td><td>${fmt(D(s.o))}</td><td>${fmt(D(s.c))}</td><td>${s.days}</td><td>${s.q}</td><td>${m0(s.credit)}</td><td>${m0(s.cost)}</td><td class="${cls(s.pl)}">${money(s.pl,true,0)}</td><td><span class="tag ${esc(s.why.split(' ')[0])}">${esc(s.why)}</span></td></tr>`,'qClosed');
  $('closedsub').textContent=`${closedR.length} in range · ${closedR.filter(s=>s.pl>0).length} winners`;
  sortable('tx',txCols,ftx,x=>`<tr><td>${fmt(D(x.dt))}</td><td>${esc(x.act)}</td><td class="l sym">${esc(x.sym)}</td><td>${esc(x.leg)}</td><td>${fmt(D(x.exp))}</td><td>${x.q}</td><td>${x.px?money(Math.abs(x.px)):'—'}</td><td>${money(x.fee)}</td><td class="${cls(x.amt)}">${money(x.amt,true)}</td></tr>`,'qTx');
  $('txsub').textContent=`${ftx.length} in range`;
  const risk=sum(openSp,s=>s.ml), by={};openSp.forEach(s=>{const k=s.sec.replace(' ETF','');by[k]=(by[k]||0)+1});
  $('risk').innerHTML=`<div style="font-size:22px;font-weight:650">${m0(risk)}</div><div class="muted" style="font-size:12px;margin-bottom:10px">of ${m0(MAXN*MAXL)} allowed (${MAXN} × ${m0(MAXL)})</div>`+(Object.keys(by).length?Object.entries(by).sort((a,b)=>b[1]-a[1]).map(([s,n])=>`<div class="sector"><span>${esc(s)}</span><div class="track"><div class="fill ${n>=MAXSEC?'full':''}" style="width:${Math.min(100,n/MAXSEC*100)}%"></div></div><span class="muted">${n}/${MAXSEC}</span></div>`).join(''):'<div class="empty">No open spreads</div>');
}
$('opennote').textContent=`Click any column title to sort. The bar shows each spread between its stop (cost to close reaches ${1+SL}× the credit, left) and its profit target (${Math.round(TP*100)}% of the credit kept, right).`;
$('risknote').textContent=`Limits: ${MAXSEC} spreads per sector · ${R.MAX_PER_SYMBOL||2} per stock · ${MAXN} total · ${m0(MAXL)} max loss each · ${R.MAX_NEW_PER_DAY||5} new per day.`;
['from','to'].forEach(id=>$(id).onchange=()=>{$('range').value='custom';draw()});
$('range').onchange=e=>{setRange(e.target.value);draw()};
$('status').onchange=draw;
['qOpen','qClosed','qTx'].forEach(id=>$(id).oninput=draw);
$('reset').onclick=()=>{document.querySelectorAll('#status input').forEach(i=>i.checked=true);$('range').value='month';setRange('month');['qOpen','qClosed','qTx'].forEach(id=>$(id).value='');for(const k in sortState)delete sortState[k];sortState.tx={k:0,dir:-1};draw()};
$('range').value='month';setRange('month');draw();
// daily P&L chart
(function(){
  const pts=DATA.daily;
  if(!pts.length){$('chart').innerHTML='<div class="empty">No trading days yet</div>';return}
  const vals=pts.map(p=>p.v), W=Math.max(300,$('chart').clientWidth||640),H=260,pad=50,mx=Math.max(1,...vals.map(Math.abs)),bw=(W-pad*2)/vals.length,y=v=>H/2-v/mx*(H/2-20);
  const every=Math.ceil(vals.length/Math.max(4,Math.floor(W/70)));
  let svg=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Daily profit and loss bars"><line x1="${pad}" x2="${W-pad}" y1="${H/2}" y2="${H/2}" stroke="var(--border-strong)"/>`;
  vals.forEach((v,i)=>{const x=pad+i*bw+bw*.18,h=Math.max(1,Math.abs(y(v)-H/2)),lab=fmt(D(pts[i].d));svg+=`<rect x="${x}" y="${v>0?y(v):H/2}" width="${bw*.64}" height="${h}" rx="2" fill="${v>=0?'var(--pos)':'var(--neg)'}"><title>${lab}: ${money(v,true,0)}</title></rect>`+(i%every===0?`<text x="${x+bw*.32}" y="${H-4}" text-anchor="middle">${lab}</text>`:'')});
  $('chart').innerHTML=svg+`<text x="4" y="14">+${m0(mx)}</text><text x="4" y="${H-22}">−${m0(mx)}</text></svg>`;
})();
// bot activity
const t2=s=>new Date(s).toLocaleString('en-US',{month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit'});
$('events').innerHTML=DATA.events.length?DATA.events.map(e=>`<li><span>${t2(e.at)}</span><span><span class="tag ${esc(e.kind.split(' ')[0])}">${esc(e.kind)}</span> ${esc(e.text)}</span></li>`).join(''):'<li class="empty">Nothing yet — the bot checks every 15 minutes while the market is open.</li>';
const sk=DATA.skipped_day===DATA.today?DATA.skipped:[];
$('skipped').innerHTML=sk.length?sk.map(s=>`<li><span>${esc(s.at)}</span><span><b>${esc(s.symbol)}</b> — ${esc(s.reason)}</span></li>`).join(''):'<li class="empty">Nothing skipped today</li>';
// ---- Rules box: edit and save spreads_settings.json
// [key, label, help, kind, suffix]; kind: int | num | pct (stored as a fraction) | bool | time
const RULE_GROUPS=[
 ['On / off',[['ENABLED','Bot on','Off = no new spreads and no exits. Open spreads stay as they are.','bool']]],
 ['Expiration',[['MIN_DTE','Shortest expiration','','int','days'],['TARGET_DTE','Preferred expiration','picks the date closest to this','int','days'],['MAX_DTE','Longest expiration','','int','days']]],
 ['Short strike',[['MIN_DELTA','Lowest delta','','num'],['TARGET_DELTA','Preferred delta','','num'],['MAX_DELTA','Highest delta','','num']]],
 ['Credit',[['MIN_CREDIT_PCT_OF_WIDTH','Minimum credit','as % of the spread width','pct','%'],['MIN_NATURAL_PCT_OF_MID','Liquidity check','bid/ask price must give at least this % of the mid credit','pct','%']]],
 ['Exits',[['TAKE_PROFIT_PCT','Take profit','close when this % of the credit is kept','pct','%'],['STOP_LOSS_MULT','Stop','close when the loss reaches this many × the credit','num','×'],['STOP_CONFIRM_CHECKS','Stop must show on','checks in a row (one check every ~15 min, real bid/ask on both options)','int','checks'],['CLOSE_BEFORE_EXPIRY_DTE','Close before expiration','days before','int','days']]],
 ['Rolling',[['ROLL_ENABLED','Roll when the short strike is touched','otherwise close','bool'],['ROLL_MIN_DTE_LEFT','Only roll with at least','days left','int','days'],['ROLL_TARGET_DTE','Roll out to about','','int','days'],['MAX_ROLLS','Most rolls per spread','','int']]],
 ['Size and limits',[['MAX_LOSS_PER_SPREAD','Max loss per spread','','num','$'],['MAX_OPEN_SPREADS','Most spreads open','','int'],['MAX_NEW_PER_DAY','New spreads per day','','int'],['MAX_PER_SECTOR','Per category','','int'],['MAX_PER_SYMBOL','Per stock','','int']]],
 ['Timing and earnings',[['OPEN_START_ET','Open new spreads from','ET','time'],['OPEN_END_ET','until','ET','time'],['AVOID_EARNINGS','Skip stocks with earnings before expiration','ETFs are never skipped','bool']]],
];
let CURRENT=JSON.parse(JSON.stringify(R));
function wlToText(wl){const by={};Object.entries(wl||{}).forEach(([s,c])=>{(by[c]=by[c]||[]).push(s)});return Object.entries(by).map(([c,l])=>`${c}: ${l.join(', ')}`).join('\n')}
function textToWl(t){const wl={};const bad=[];t.split('\n').map(x=>x.trim()).filter(Boolean).forEach((line,i)=>{const m=line.match(/^([^:]+):(.*)$/);if(!m){bad.push(`line ${i+1} needs "Category: SYM, SYM"`);return}const c=m[1].trim();m[2].split(/[,\s]+/).map(x=>x.trim().toUpperCase().replace(/^\$/,'')).filter(Boolean).forEach(sym=>{if(!/^[A-Z][A-Z.]{0,5}$/.test(sym))bad.push(`"${sym}" is not a ticker`);else if(!(sym in wl))wl[sym]=c})});return {wl,bad}}
function renderRules(v){
  let h='';
  RULE_GROUPS.forEach(([g,fields])=>{h+=`<div class="rgroup"><h3>${g}</h3>`;fields.forEach(([k,lab,help,kind,suf])=>{
    const val=v[k];let inp;
    if(kind==='bool')inp=`<input type="checkbox" data-k="${k}" data-kind="bool" ${val?'checked':''}>`;
    else if(kind==='time')inp=`<input type="time" data-k="${k}" data-kind="time" value="${esc(val||'')}">`;
    else{const shown=kind==='pct'?+(val*100).toFixed(2):val;inp=`<input type="number" step="any" data-k="${k}" data-kind="${kind}" value="${shown??''}">`}
    h+=`<div class="rrow"><label>${lab}${help?`<small>${help}</small>`:''}</label>${inp}<span class="suf">${suf||''}</span></div>`})
    h+='</div>'});
  const W=v.WIDTH_TABLE||[];
  h+=`<div class="rgroup"><h3>Spread width by stock price</h3><table class="wt"><tr><th>Price from $</th><th>to $</th><th>Width min $</th><th>max $</th></tr>`+
    W.map((r,i)=>`<tr>${r.map((n,j)=>`<td><input type="number" step="any" data-w="${i},${j}" value="${n}"></td>`).join('')}</tr>`).join('')+`</table></div>`;
  h+=`<div class="rgroup"><h3>Watchlist (${Object.keys(v.WATCHLIST||{}).length} stocks)</h3><div class="note" style="margin:0 0 6px">One category per line: <b>Category: SYM, SYM, …</b> Each category counts toward the per-category limit. Put "ETF" in the name of ETF categories (they skip the earnings check).</div><textarea id="wlText" spellcheck="false">${esc(wlToText(v.WATCHLIST))}</textarea></div>`;
  $('rulesform').innerHTML=h;
}
function collectRules(){
  const out={},errs=[];
  document.querySelectorAll('#rulesform [data-k]').forEach(el=>{const k=el.dataset.k,kind=el.dataset.kind;
    if(kind==='bool')out[k]=el.checked;
    else if(kind==='time')out[k]=el.value;
    else{if(el.value===''){errs.push(`${k} is empty`);return}let n=Number(el.value);if(!isFinite(n)){errs.push(`${k} is not a number`);return}if(kind==='pct')n=+(n/100).toFixed(4);if(kind==='int')n=Math.round(n);out[k]=n}});
  const W=(CURRENT.WIDTH_TABLE||[]).map(r=>r.slice());
  document.querySelectorAll('#rulesform [data-w]').forEach(el=>{const [i,j]=el.dataset.w.split(',').map(Number);W[i][j]=Number(el.value)});
  out.WIDTH_TABLE=W;
  const {wl,bad}=textToWl($('wlText').value);errs.push(...bad);
  if(!Object.keys(wl).length)errs.push('the watchlist is empty');
  out.WATCHLIST=wl;
  if(!(out.MIN_DTE<=out.TARGET_DTE&&out.TARGET_DTE<=out.MAX_DTE))errs.push('expirations must be shortest ≤ preferred ≤ longest');
  if(!(out.MIN_DELTA<=out.TARGET_DELTA&&out.TARGET_DELTA<=out.MAX_DELTA))errs.push('deltas must be lowest ≤ preferred ≤ highest');
  return {out,errs};
}
async function repoRules(){
  const r=await fetch('https://api.github.com/repos/patrickobirmingham-eng/POB-CL-DT/contents/spreads_settings.json?ref=main&_='+Date.now(),{headers:{Accept:'application/vnd.github.raw+json'},cache:'no-store'});
  if(!r.ok)throw new Error('HTTP '+r.status);return r.json();
}
function setStatus(t,c){$('saveStatus').textContent=t;$('saveStatus').className='savestatus '+c}
async function saveRules(){
  const {out,errs}=collectRules();
  if(errs.length){setStatus('Not saved: '+errs.join('; '),'err');return}
  const pass=$('passcode').value;
  if(!pass){setStatus('Enter the passcode to save.','err');return}
  if(!DATA.save_url){setStatus('Saving is not set up on this page.','err');return}
  $('saveBtn').disabled=true;setStatus('Saving…','info');
  try{
    const resp=await fetch(DATA.save_url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:pass,settings:out})});
    const d=await resp.json().catch(()=>({}));
    if(!resp.ok||!d.ok)throw new Error(d.error||('HTTP '+resp.status));
    $('passcode').value='';
    if(d.unchanged){setStatus('Nothing changed.','info');return}
    setStatus('Saved; checking the repo…','info');
    await new Promise(r=>setTimeout(r,2000));
    try{const repo=await repoRules();const same=JSON.stringify(Object.keys(out).map(k=>repo[k]))===JSON.stringify(Object.keys(out).map(k=>out[k]));
      CURRENT=Object.assign({},CURRENT,repo);renderRules(CURRENT);
      setStatus(same?'Saved and confirmed. The bot uses these rules from its next check.':'Saved, but the repo shows different values; the form now shows what the bot will use.',same?'ok':'err');
    }catch(e){CURRENT=Object.assign({},CURRENT,out);setStatus('Saved. (Could not re-check the repo just now.) The bot uses these rules from its next check.','ok')}
  }catch(e){
    const hint=/Failed to fetch|NetworkError|Load failed/.test(e.message)?' — the save helper (spreads-settings-save.php) may not be uploaded to the website yet.':'';
    setStatus('Save failed: '+e.message+hint,'err');
  }finally{$('saveBtn').disabled=false}
}
renderRules(CURRENT);
// show the newest saved rules (this page may be up to 15 minutes old)
repoRules().then(v=>{CURRENT=Object.assign({},CURRENT,v);renderRules(CURRENT)}).catch(()=>{});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
