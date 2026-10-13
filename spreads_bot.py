"""
Bull put credit-spread bot for a SEPARATE Alpaca paper account.

Runs once per invocation (GitHub Actions every ~15 min during market hours):
  1. Reconcile tracked spreads with the account's option positions.
  2. Manage open spreads, in priority order:
       stop      - cost to close >= credit x (1 + STOP_LOSS_MULT)  -> close
       target    - cost to close <= credit x (1 - TAKE_PROFIT_PCT) -> close
       roll      - underlying at/below the short strike with >= ROLL_MIN_DTE_LEFT
                   days left -> roll down and out (~ROLL_TARGET_DTE days) for a net
                   credit, otherwise close. At most MAX_ROLLS per position, and
                   not twice in one day.
       expiry    - <= CLOSE_BEFORE_EXPIRY_DTE days left -> close
  3. Open new spreads from the watchlist within the risk limits.

Rules live in spreads_settings.json. Credentials: SPREADS_APCA_API_KEY_ID /
SPREADS_APCA_API_SECRET_KEY (never the day bot's keys). Paper only.

Usage: python spreads_bot.py [--dry-run]
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "spreads_settings.json")
STATE_FILE = os.path.join(HERE, "spreads_state.json")
TRADES_FILE = os.path.join(HERE, "spreads_trades.csv")
EARNINGS_FILE = os.path.join(HERE, "spreads_earnings.json")
PAPER_URL = "https://paper-api.alpaca.markets"

TRADE_FIELDS = ["closed_at", "symbol", "sector", "short_strike", "long_strike", "expiration", "qty",
                "opened_at", "delta_at_open", "credit", "close_cost", "pl", "reason", "id"]


def log(msg):
    print(f"[{datetime.now(ET):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_settings():
    with open(SETTINGS_FILE) as f:
        return json.load(f)


def sector_of(settings, sym):
    return settings["WATCHLIST"].get(sym, "Other").replace(" ETF", "")


# ---------------------------------------------------------------- state ----
def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("spreads", {})     # id -> open spread
    st.setdefault("pending", {})     # order id -> {kind, spread id, ...}
    st.setdefault("day", "")
    st.setdefault("opened_today", 0)
    st.setdefault("events", [])      # recent decisions for the dashboard
    st.setdefault("skipped", [])     # today's skipped candidates + reasons
    return st


def save_state(st):
    st["events"] = st["events"][-200:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2, default=str)


def event(st, kind, text):
    st["events"].append({"at": datetime.now(ET).isoformat(timespec="seconds"), "kind": kind, "text": text})
    log(f"{kind}: {text}")


def append_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


# ---------------------------------------------------------------- broker ---
class Broker:
    """Thin wrapper over alpaca-py so the strategy logic can be tested with a fake."""

    def __init__(self, key, secret):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical import StockHistoricalDataClient
        self.key, self.secret = key, secret
        self.trading = TradingClient(key, secret, paper=True)
        self.opt = OptionHistoricalDataClient(key, secret)
        self.stk = StockHistoricalDataClient(key, secret)

    def clock(self):
        return self.trading.get_clock()

    def account(self):
        return self.trading.get_account()

    def positions(self):
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()
                if str(getattr(p, "asset_class", "")).lower().endswith("us_option")}

    def stock_price(self, sym):
        from alpaca.data.requests import StockLatestTradeRequest
        from alpaca.data.enums import DataFeed
        t = self.stk.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))
        return float(t[sym].price)

    def put_chain(self, sym, exp_gte, exp_lte):
        """{symbol: {strike, expiration, bid, ask, delta}} for puts in the date window."""
        from alpaca.data.requests import OptionChainRequest
        from alpaca.data.enums import OptionsFeed
        from alpaca.trading.enums import ContractType
        snaps = self.opt.get_option_chain(OptionChainRequest(
            underlying_symbol=sym, type=ContractType.PUT, feed=OptionsFeed.INDICATIVE,
            expiration_date_gte=exp_gte, expiration_date_lte=exp_lte))
        out = {}
        for s, snap in (snaps or {}).items():
            exp, strike = parse_occ(s)
            q = snap.latest_quote
            out[s] = {"strike": strike, "expiration": exp,
                      "bid": float(q.bid_price or 0) if q else 0.0, "ask": float(q.ask_price or 0) if q else 0.0,
                      "delta": float(snap.greeks.delta) if snap.greeks else None}
        return out

    def quotes(self, symbols):
        from alpaca.data.requests import OptionLatestQuoteRequest
        from alpaca.data.enums import OptionsFeed
        qs = self.opt.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=list(symbols),
                                                                       feed=OptionsFeed.INDICATIVE))
        return {s: (float(q.bid_price or 0), float(q.ask_price or 0)) for s, q in qs.items()}

    def submit_mleg(self, legs, qty, limit_price):
        """legs: [(symbol, 'buy'|'sell', intent)]. limit_price: + debit / - credit (net, per share)."""
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, PositionIntent
        req = LimitOrderRequest(
            qty=qty, order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            legs=[OptionLegRequest(symbol=s, ratio_qty=1, side=OrderSide(side), position_intent=PositionIntent(intent))
                  for s, side, intent in legs])
        o = self.trading.submit_order(req)
        return str(o.id)

    def order(self, oid):
        o = self.trading.get_order_by_id(oid)
        legs = {}
        for leg in (o.legs or []):
            legs[leg.symbol] = float(leg.filled_avg_price or 0)
        return {"status": str(o.status.value if hasattr(o.status, "value") else o.status),
                "filled_qty": float(o.filled_qty or 0), "legs": legs}

    def cancel(self, oid):
        try:
            self.trading.cancel_order_by_id(oid)
        except Exception as e:
            log(f"cancel {oid}: {e}")


def parse_occ(sym):
    """'AVGO261120P00330000' -> (date(2026,11,20), 330.0)"""
    tail = sym[-15:]
    exp = datetime.strptime(tail[:6], "%y%m%d").date()
    return exp, int(tail[7:]) / 1000.0


def mid(bid, ask):
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return max(bid, ask)


# --------------------------------------------------------------- earnings --
def load_earnings(today, settings, refresh=True):
    """{symbol: 'YYYY-MM-DD'} next earnings date within ~60 days (Nasdaq calendar), cached daily."""
    try:
        with open(EARNINGS_FILE) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    if cache.get("fetched") == today.isoformat() or not refresh:
        return cache.get("dates", {}), cache.get("ok", False)
    wanted = {s for s, sec in settings["WATCHLIST"].items() if "ETF" not in sec}
    dates, ok_days = {}, 0
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
           "Referer": "https://www.nasdaq.com/"}
    d = today
    for _ in range(62):
        if d.weekday() < 5:
            try:
                r = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}",
                                 headers=hdr, timeout=15)
                rows = ((r.json().get("data") or {}).get("rows")) or []
                ok_days += 1
                for row in rows:
                    s = (row.get("symbol") or "").upper()
                    if s in wanted and s not in dates:
                        dates[s] = d.isoformat()
            except Exception as e:
                log(f"earnings fetch {d}: {e}")
        d += timedelta(days=1)
    ok = ok_days >= 30
    cache = {"fetched": today.isoformat(), "ok": ok, "dates": dates}
    with open(EARNINGS_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    log(f"earnings calendar: {ok_days} days fetched, {len(dates)} watchlist dates found")
    return dates, ok


# --------------------------------------------------------------- strategy --
def width_range(settings, price):
    for lo, hi, wmin, wmax in settings["WIDTH_TABLE"]:
        if lo <= price < hi:
            return wmin, wmax
    return settings["WIDTH_TABLE"][-1][2:]


def pick_spread(settings, chain, price, today, dte_lo, dte_hi, dte_target):
    """Best bull put spread from a put chain, or (None, reason)."""
    exps = sorted({c["expiration"] for c in chain.values() if dte_lo <= (c["expiration"] - today).days <= dte_hi})
    if not exps:
        return None, f"no expiration {dte_lo}-{dte_hi} days out"
    exp = min(exps, key=lambda e: abs((e - today).days - dte_target))
    puts = {s: c for s, c in chain.items() if c["expiration"] == exp}
    by_strike = {c["strike"]: (s, c) for s, c in puts.items()}
    shorts = [(s, c) for s, c in puts.items()
              if c["delta"] is not None and settings["MIN_DELTA"] <= abs(c["delta"]) <= settings["MAX_DELTA"]
              and c["strike"] < price and c["bid"] > 0]
    if not shorts:
        return None, f"no strike with delta {settings['MIN_DELTA']}-{settings['MAX_DELTA']} on {exp}"
    wmin, wmax = width_range(settings, price)
    best, why = None, "no long strike in width range"
    for ssym, sc in sorted(shorts, key=lambda x: abs(abs(x[1]["delta"]) - settings["TARGET_DELTA"])):
        longs = [(k, v) for k, v in by_strike.items() if wmin <= sc["strike"] - k <= wmax and v[1]["ask"] > 0]
        if not longs:
            continue
        target_w = (wmin + wmax) / 2
        k, (lsym, lc) = min(longs, key=lambda kv: abs(sc["strike"] - kv[0] - target_w))
        width = sc["strike"] - k
        credit = mid(sc["bid"], sc["ask"]) - mid(lc["bid"], lc["ask"])
        if credit < settings["MIN_CREDIT_PCT_OF_WIDTH"] * width:
            why = f"credit ${credit:.2f} under {settings['MIN_CREDIT_PCT_OF_WIDTH']:.0%} of ${width:g} width"
            continue
        best = {"short": ssym, "long": lsym, "short_strike": sc["strike"], "long_strike": k, "width": width,
                "expiration": exp.isoformat(), "credit": round(credit, 2), "delta": round(abs(sc["delta"]), 3),
                "natural": round(sc["bid"] - lc["ask"], 2)}
        break
    return (best, None) if best else (None, why)


def size(settings, width, credit):
    per = (width - credit) * 100
    return max(0, int(settings["MAX_LOSS_PER_SPREAD"] // per)) if per > 0 else 0


def close_cost(quotes, sp):
    sb, sa = quotes.get(sp["short"], (0, 0))
    lb, la = quotes.get(sp["long"], (0, 0))
    return mid(sb, sa) - mid(lb, la), max(0.0, sa - lb)   # (mid, natural)


def decide(settings, sp, cost, price, today):
    """Exit decision for an open spread: (action, reason) or (None, None)."""
    credit = sp["credit"]
    dte = (date.fromisoformat(sp["expiration"]) - today).days
    if cost >= credit * (1 + settings["STOP_LOSS_MULT"]):
        return "close", "stop"
    if cost <= credit * (1 - settings["TAKE_PROFIT_PCT"]):
        return "close", "target"
    if price is not None and price <= sp["short_strike"] and sp.get("rolled_on") != today.isoformat():
        if (settings["ROLL_ENABLED"] and dte >= settings["ROLL_MIN_DTE_LEFT"]
                and sp.get("rolls", 0) < settings.get("MAX_ROLLS", 2)):
            return "roll", "touched short strike"
        return "close", "touched short strike"
    if dte <= settings["CLOSE_BEFORE_EXPIRY_DTE"]:
        return "close", "expiring"
    return None, None


# ------------------------------------------------------------------ run ----
def wait_fill(broker, oid, secs=25):
    t0 = time.time()
    while time.time() - t0 < secs:
        o = broker.order(oid)
        if o["status"] in ("filled", "canceled", "expired", "rejected"):
            return o
        time.sleep(2)
    return broker.order(oid)


def work_order(broker, legs, qty, prices, secs=20):
    """Try each net limit price in turn (best for us first), waiting `secs` for a fill.

    Returns (order_id, order). An unfilled last attempt is left working; a partial
    fill on an earlier attempt is returned as is (its filled_qty says how much)."""
    oid, o = None, None
    for i, px in enumerate(prices):
        oid = broker.submit_mleg(legs, qty, px)
        o = wait_fill(broker, oid, secs)
        if o["status"] == "filled":
            break
        if i == len(prices) - 1 and o["filled_qty"] == 0:
            break                       # leave the last price working; the next run picks it up
        broker.cancel(oid)
        o = broker.order(oid)
        if o["filled_qty"] > 0:         # partial fill: keep it, the rest is re-decided next run
            break
    return oid, o


def ladder(start, end, steps=3):
    """Prices from `start` to `end` inclusive, rounded to cents, without repeats."""
    out = []
    for i in range(steps):
        px = round(start + (end - start) * i / max(1, steps - 1), 2)
        if px not in out:
            out.append(px)
    return out


def record_close(st, sp, close_px, reason, today_iso, qty=None):
    """Log a closed spread. qty < the spread's qty records a partial close and keeps the rest open."""
    qty = sp["qty"] if qty is None or qty >= sp["qty"] else qty
    pl = round((sp["credit"] - close_px) * 100 * qty, 2)
    append_trade({"closed_at": today_iso, "symbol": sp["symbol"], "sector": sp["sector"],
                  "short_strike": sp["short_strike"], "long_strike": sp["long_strike"],
                  "expiration": sp["expiration"], "qty": qty, "opened_at": sp["opened_at"],
                  "delta_at_open": sp.get("delta", ""), "credit": sp["credit"], "close_cost": round(close_px, 2),
                  "pl": pl, "reason": reason, "id": sp["id"]})
    if qty < sp["qty"]:
        sp["qty"] -= qty
    else:
        st["spreads"].pop(sp["id"], None)
    event(st, reason, f"{sp['symbol']} {sp['short_strike']:g}/{sp['long_strike']:g} x{qty} closed at "
                      f"${close_px:.2f} (credit ${sp['credit']:.2f}) P&L ${pl:,.0f}")


def handle_pending(broker, st, now):
    """Check orders left working by earlier runs; cancel stale ones (they get re-decided this run)."""
    for oid, p in list(st["pending"].items()):
        o = broker.order(oid)
        if o["status"] == "filled":
            apply_fill(st, p, o, now)
            st["pending"].pop(oid)
        elif o["status"] in ("canceled", "expired", "rejected", "done_for_day"):
            st["pending"].pop(oid)
            if o["filled_qty"] > 0:
                apply_fill(st, p, o, now)
        else:
            broker.cancel(oid)
            st["pending"].pop(oid)
            log(f"cancelled stale {p['kind']} order {oid} for {p.get('symbol')}")
            o = broker.order(oid)
            if o["filled_qty"] > 0:          # filled (fully or partly) before the cancel landed
                apply_fill(st, p, o, now)


def apply_fill(st, p, o, now):
    legs = o["legs"]
    filled = int(o["filled_qty"] or 0) or None
    if p["kind"] == "open":
        sp = p["spread"]
        credit = legs.get(sp["short"], 0) - legs.get(sp["long"], 0) if legs else sp["credit"]
        sp["credit"] = round(credit if credit > 0 else sp["credit"], 2)
        sp["qty"] = filled or sp["qty"]
        st["spreads"][sp["id"]] = sp
        st["opened_today"] = st.get("opened_today", 0) + 1
        event(st, "open", f"{sp['symbol']} {sp['short_strike']:g}/{sp['long_strike']:g} put x{sp['qty']} exp "
                          f"{sp['expiration']} credit ${sp['credit']:.2f} (delta {sp['delta']})")
    elif p["kind"] == "close":
        sp = st["spreads"].get(p["spread_id"])
        if sp:
            px = legs.get(sp["short"], 0) - legs.get(sp["long"], 0) if legs else p["price"]
            record_close(st, sp, px if px > 0 else p["price"], p["reason"], now.date().isoformat(), filled)
    elif p["kind"] == "roll":
        sp = st["spreads"].get(p["spread_id"])
        new = p["spread"]
        if sp:
            px = legs.get(sp["short"], 0) - legs.get(sp["long"], 0) if legs else p["price"]
            record_close(st, sp, px if px > 0 else p["price"], "rolled", now.date().isoformat(), filled)
        new["qty"] = filled or new["qty"]
        if legs:
            new["credit"] = round(legs.get(new["short"], 0) - legs.get(new["long"], 0), 2) or new["credit"]
        st["spreads"][new["id"]] = new
        event(st, "open", f"{new['symbol']} rolled to {new['short_strike']:g}/{new['long_strike']:g} exp "
                          f"{new['expiration']} credit ${new['credit']:.2f}")


def reconcile(broker, st, today_iso):
    """Spreads whose legs vanished from the account expired or were assigned/closed elsewhere."""
    pos = broker.positions()
    for sid, sp in list(st["spreads"].items()):
        if sp["short"] not in pos and sp["long"] not in pos:
            exp = date.fromisoformat(sp["expiration"])
            reason = "expired" if date.fromisoformat(today_iso) >= exp else "closed outside bot"
            record_close(st, sp, 0.0 if reason == "expired" else sp["credit"], reason, today_iso)
        elif sp["short"] not in pos:
            event(st, "warning", f"{sp['symbol']} short leg missing (assigned?) - check the account")
    return pos


def run(broker, settings, dry=False, now=None):
    now = now or datetime.now(ET)
    today = now.date()
    st = load_state()
    if st["day"] != today.isoformat():
        st.update(day=today.isoformat(), opened_today=0, skipped=[])
    if not settings.get("ENABLED", True):
        log("spreads bot disabled in settings")
        save_state(st)
        return st
    clock = broker.clock()
    if not clock.is_open and not dry:
        log("market closed - nothing to trade")
        save_state(st)
        return st

    handle_pending(broker, st, now)
    reconcile(broker, st, today.isoformat())

    # ---- manage open spreads
    if st["spreads"]:
        syms = {s for sp in st["spreads"].values() for s in (sp["short"], sp["long"])}
        qs = broker.quotes(syms)
        prices = {}
        for sp in list(st["spreads"].values()):
            if sp["symbol"] not in prices:
                try:
                    prices[sp["symbol"]] = broker.stock_price(sp["symbol"])
                except Exception as e:
                    log(f"price {sp['symbol']}: {e}")
                    prices[sp["symbol"]] = None
            cost, natural = close_cost(qs, sp)
            sp["last_cost"], sp["last_price"], sp["last_checked"] = round(cost, 2), prices[sp["symbol"]], now.isoformat(timespec="seconds")
            action, reason = decide(settings, sp, cost, prices[sp["symbol"]], today)
            if not action:
                continue
            if action == "roll":
                new = roll_candidate(broker, settings, sp, prices[sp["symbol"]], cost, today)
                if new and new["credit"] > cost:
                    net = new["credit"] - cost
                    log(f"ROLL {sp['symbol']}: close ${cost:.2f} -> new {new['short_strike']:g}/{new['long_strike']:g} "
                        f"{new['expiration']} credit ${new['credit']:.2f} (net +${net:.2f})")
                    if not dry:
                        legs = [(sp["short"], "buy", "buy_to_close"), (sp["long"], "sell", "sell_to_close"),
                                (new["short"], "sell", "sell_to_open"), (new["long"], "buy", "buy_to_open")]
                        p = {"kind": "roll", "spread_id": sp["id"], "spread": new, "price": cost, "symbol": sp["symbol"]}
                        # net credit from mid down to 1 cent: a roll is only worth doing for a credit
                        oid, o = work_order(broker, legs, sp["qty"], ladder(-round(net, 2), -0.01))
                        if o["filled_qty"] > 0:
                            apply_fill(st, p, o, now)
                        else:
                            st["pending"][oid] = p
                    continue
                reason = "touched short strike (no roll for a credit)"
            # close
            if reason == "target":      # mid, then halfway to the natural price (still a profit)
                limits = ladder(round(cost, 2), round(min(max(cost, natural), sp["credit"] * 0.75), 2), 2)
            else:                       # stop / defensive closes: pay up to get out
                limits = [round(max(cost, natural) + 0.05, 2)]
            log(f"CLOSE {sp['symbol']} ({reason}): cost ${cost:.2f}, limits {limits}")
            if not dry:
                p = {"kind": "close", "spread_id": sp["id"], "reason": reason.split(" (")[0], "price": cost,
                     "symbol": sp["symbol"]}
                oid, o = work_order(broker, [(sp["short"], "buy", "buy_to_close"), (sp["long"], "sell", "sell_to_close")],
                                    sp["qty"], [max(0.01, x) for x in limits])
                if o["filled_qty"] > 0:
                    apply_fill(st, p, o, now)
                else:
                    st["pending"][oid] = p

    # ---- open new spreads
    t = now.strftime("%H:%M")
    if dry or settings["OPEN_START_ET"] <= t <= settings["OPEN_END_ET"]:
        open_new(broker, settings, st, today, now, dry)
    save_state(st)
    return st


def pick_roll(chain, sp, cost, today, dte_lo, dte_hi, dte_target):
    """Roll down and out: same width, short strike at or below the current one, on the expiration
    nearest dte_target. Picks the LOWEST strike whose credit still beats the cost to close."""
    exps = sorted({c["expiration"] for c in chain.values() if dte_lo <= (c["expiration"] - today).days <= dte_hi})
    if not exps:
        return None
    exp = min(exps, key=lambda e: abs((e - today).days - dte_target))
    by_strike = {c["strike"]: (s, c) for s, c in chain.items() if c["expiration"] == exp}
    w = sp["short_strike"] - sp["long_strike"]
    for k in sorted(by_strike):
        if k > sp["short_strike"] or (k - w) not in by_strike:
            continue
        (ssym, sc), (lsym, lc) = by_strike[k], by_strike[k - w]
        if sc["bid"] <= 0:
            continue
        credit = mid(sc["bid"], sc["ask"]) - mid(lc["bid"], lc["ask"])
        if credit > cost + 0.05:
            return {"short": ssym, "long": lsym, "short_strike": k, "long_strike": k - w, "width": w,
                    "expiration": exp.isoformat(), "credit": round(credit, 2),
                    "delta": round(abs(sc["delta"]), 3) if sc["delta"] is not None else "",
                    "natural": round(sc["bid"] - lc["ask"], 2)}
    return None


def roll_candidate(broker, settings, sp, price, cost, today):
    if price is None:
        return None
    lo = max(settings["ROLL_TARGET_DTE"] - 7, (date.fromisoformat(sp["expiration"]) - today).days + 7)
    hi = max(settings["ROLL_TARGET_DTE"] + 15, lo + 14)
    try:
        chain = broker.put_chain(sp["symbol"], today + timedelta(days=lo), today + timedelta(days=hi))
    except Exception as e:
        log(f"roll chain {sp['symbol']}: {e}")
        return None
    pick = pick_roll(chain, sp, cost, today, lo, hi, settings["ROLL_TARGET_DTE"])
    if not pick or size(settings, pick["width"], pick["credit"] - cost) < sp["qty"]:
        return None                     # no credit roll, or the same size would break the max-loss cap
    new = new_spread(settings, sp["symbol"], pick, sp["qty"], today)
    new.update(rolls=sp.get("rolls", 0) + 1, rolled_on=today.isoformat(), opened_at=sp["opened_at"])
    return new


def new_spread(settings, sym, pick, qty, today):
    return {"id": f"{sym}-{pick['expiration']}-{pick['short_strike']:g}-{datetime.now(ET):%H%M%S}",
            "symbol": sym, "sector": sector_of(settings, sym), "short": pick["short"], "long": pick["long"],
            "short_strike": pick["short_strike"], "long_strike": pick["long_strike"], "width": pick["width"],
            "expiration": pick["expiration"], "qty": qty, "credit": pick["credit"], "delta": pick["delta"],
            "opened_at": today.isoformat()}


def open_new(broker, settings, st, today, now, dry):
    open_sp = list(st["spreads"].values()) + [p["spread"] for p in st["pending"].values() if p["kind"] in ("open", "roll")]
    def day_full():                     # unfilled entry orders still count toward the daily cap
        working = sum(1 for p in st["pending"].values() if p["kind"] == "open")
        return st["opened_today"] + working >= settings["MAX_NEW_PER_DAY"]

    if len(open_sp) >= settings["MAX_OPEN_SPREADS"] or day_full():
        return
    earnings, earn_ok = load_earnings(today, settings) if settings["AVOID_EARNINGS"] else ({}, True)
    skipped = {s["symbol"]: s for s in st["skipped"]}
    per_sym = {}
    per_sec = {}
    for sp in open_sp:
        per_sym[sp["symbol"]] = per_sym.get(sp["symbol"], 0) + 1
        per_sec[sp["sector"]] = per_sec.get(sp["sector"], 0) + 1

    def skip(sym, why):
        skipped[sym] = {"symbol": sym, "reason": why, "at": now.strftime("%H:%M")}

    # least-used symbols first so the book spreads out
    order = sorted(settings["WATCHLIST"], key=lambda s: (per_sym.get(s, 0), list(settings["WATCHLIST"]).index(s)))
    for sym in order:
        if len(open_sp) >= settings["MAX_OPEN_SPREADS"] or day_full():
            break
        sec = sector_of(settings, sym)
        if per_sym.get(sym, 0) >= settings["MAX_PER_SYMBOL"]:
            continue
        if per_sec.get(sec, 0) >= settings["MAX_PER_SECTOR"]:
            skip(sym, f"{sec} already has {per_sec[sec]} open spreads")
            continue
        if any(p.get("symbol") == sym for p in st["pending"].values()):
            continue
        try:
            price = broker.stock_price(sym)
            chain = broker.put_chain(sym, today + timedelta(days=settings["MIN_DTE"]),
                                     today + timedelta(days=settings["MAX_DTE"]))
        except Exception as e:
            skip(sym, f"data error: {e}"[:120])
            continue
        pick, why = pick_spread(settings, chain, price, today, settings["MIN_DTE"], settings["MAX_DTE"],
                                settings["TARGET_DTE"])
        if not pick:
            skip(sym, why)
            continue
        if settings["AVOID_EARNINGS"] and "ETF" not in settings["WATCHLIST"][sym]:
            ed = earnings.get(sym)
            if ed and ed <= pick["expiration"]:
                skip(sym, f"earnings {ed} before expiration {pick['expiration']}")
                continue
            if not earn_ok and not ed:
                skip(sym, "earnings calendar unavailable today")
                continue
        floor = settings["MIN_CREDIT_PCT_OF_WIDTH"] * pick["width"]
        worst = max(pick["natural"], floor, 0.01)   # the lowest credit we'll accept (last price tried)
        # size on the worst accepted credit so a fill anywhere in the ladder stays under the max-loss cap
        qty = size(settings, pick["width"], min(worst, pick["credit"]))
        if qty < 1:
            skip(sym, "too small to size")
            continue
        sp = new_spread(settings, sym, pick, qty, today)
        # ask for the mid credit first, then step toward the natural price, never below the minimum credit
        prices = [-c for c in ladder(pick["credit"], min(worst, pick["credit"]), 3)]
        log(f"OPEN {sym} {pick['short_strike']:g}/{pick['long_strike']:g} put exp {pick['expiration']} x{qty} "
            f"credit ${pick['credit']:.2f} (natural ${pick['natural']:.2f}) delta {pick['delta']} "
            f"max loss ${(pick['width'] - pick['credit']) * 100 * qty:,.0f}")
        skipped.pop(sym, None)
        if dry:
            continue
        p = {"kind": "open", "spread": sp, "symbol": sym}
        try:
            oid, o = work_order(broker, [(pick["short"], "sell", "sell_to_open"), (pick["long"], "buy", "buy_to_open")],
                                qty, prices)
        except Exception as e:
            skip(sym, f"order rejected: {e}"[:160])
            continue
        if o["filled_qty"] > 0:
            apply_fill(st, p, o, now)
        else:
            st["pending"][oid] = p
        open_sp.append(sp)
        per_sym[sym] = per_sym.get(sym, 0) + 1
        per_sec[sec] = per_sec.get(sec, 0) + 1
    st["skipped"] = list(skipped.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="scan and log decisions without placing orders")
    args = ap.parse_args()
    key, secret = os.getenv("SPREADS_APCA_API_KEY_ID"), os.getenv("SPREADS_APCA_API_SECRET_KEY")
    if not key or not secret:
        sys.exit("SPREADS_APCA_API_KEY_ID / SPREADS_APCA_API_SECRET_KEY not set")
    if key == os.getenv("APCA_API_KEY_ID"):
        sys.exit("Spreads keys are the same as the day bot's keys - refusing to trade.")
    broker = Broker(key, secret)
    run(broker, load_settings(), dry=args.dry_run)


if __name__ == "__main__":
    main()
