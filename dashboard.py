"""
Generates a static HTML dashboard (docs/index.html) summarizing the paper
trading account: current equity, an equity curve, open positions, and recent
order history. Meant to be run as a step in the GitHub Actions workflow after
each bot session, with the output committed and served via GitHub Pages.

This is read-only — it never places or modifies orders.
"""
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

ET = ZoneInfo("America/New_York")
OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.html")

# Optional "Refresh" button on the dashboard: pulls live account/positions/
# orders on demand via a small read-only PHP proxy (holds the Alpaca keys
# server-side, e.g. hosted on Bluehost) rather than only showing the snapshot
# from whenever the bot last pushed. Both come from GitHub Actions repo
# variables (Settings -> Secrets and variables -> Actions -> Variables) —
# not secrets, since the token ends up embedded in this public page anyway;
# it only deters casual random hits, it's not a real access boundary. Leave
# DASHBOARD_REFRESH_URL unset to hide the button entirely (default today).
REFRESH_ENDPOINT = os.getenv("DASHBOARD_REFRESH_URL", "")
REFRESH_TOKEN = os.getenv("DASHBOARD_REFRESH_TOKEN", "")


def get_client():
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID / APCA_API_SECRET_KEY first.")
    return TradingClient(key, secret, paper=True)


def build_daily_pl_chart(points, width=900, height=240, pad_left=72, pad_right=16, pad_top=16, pad_bottom=36):
    """points: list of (date_str, float dollar P&L), oldest first, one entry per
    trading day over the lookback window. Returns an inline SVG bar chart with a
    dollar Y axis and a date X axis (green bars for up days, red for down days).
    """
    if not points:
        return "<p class='muted'>Not enough history yet for a chart.</p>"

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    lo = min(lo, 0.0)
    hi = max(hi, 0.0)
    span = (hi - lo) or 1.0
    n = len(points)

    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    gap = plot_w / n
    bar_w = max(gap * 0.7, 1.0)

    def y(v):
        return pad_top + (hi - v) / span * plot_h

    zero_y = y(0.0)

    bars = ""
    for i, (d, v) in enumerate(points):
        x = pad_left + i * gap + (gap - bar_w) / 2
        top = y(max(v, 0.0))
        bottom = y(min(v, 0.0))
        h = max(bottom - top, 1.0)
        color = "#16a34a" if v >= 0 else "#dc2626"
        # data-date/data-pl feed the JS hover tooltip below; the <title> is a
        # native-tooltip fallback for anyone viewing the raw SVG.
        bars += (
            f'<rect class="pl-bar" x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="{color}" data-date="{d}" data-pl="{fmt_money(v)}"><title>{d}: {fmt_money(v)}</title></rect>'
        )

    # Sample ~8 evenly-spaced date labels across the X axis rather than one per
    # bar — a year of trading days is far too many to label individually.
    n_labels = min(8, n)
    label_idxs = sorted({round(i * (n - 1) / (n_labels - 1)) for i in range(n_labels)}) if n_labels > 1 else [0]
    x_labels = ""
    for i in label_idxs:
        d, _ = points[i]
        lx = pad_left + i * gap + gap / 2
        x_labels += (
            f'<text x="{lx:.1f}" y="{height - pad_bottom + 18}" font-size="10" '
            f'fill="currentColor" text-anchor="middle" opacity="0.65">{d}</text>'
        )

    zero_line = (
        f'<line x1="{pad_left}" y1="{zero_y:.1f}" x2="{width - pad_right}" y2="{zero_y:.1f}" '
        f'stroke="currentColor" stroke-opacity="0.3" stroke-width="1" />'
    )
    y_hi_label = (
        f'<text x="{pad_left - 8}" y="{pad_top + 8}" font-size="10" fill="currentColor" '
        f'text-anchor="end" opacity="0.65">{fmt_money(hi)}</text>'
    )
    y_lo_label = (
        f'<text x="{pad_left - 8}" y="{height - pad_bottom}" font-size="10" fill="currentColor" '
        f'text-anchor="end" opacity="0.65">{fmt_money(lo)}</text>'
    )
    y_zero_label = (
        f'<text x="{pad_left - 8}" y="{zero_y + 3:.1f}" font-size="10" fill="currentColor" '
        f'text-anchor="end" opacity="0.65">$0</text>'
    )

    return f"""
    <svg viewBox="0 0 {width} {height}" class="pl-chart" style="color: var(--muted);">
      {zero_line}
      {bars}
      {x_labels}
      {y_hi_label}
      {y_zero_label if abs(zero_y - y(hi)) > 12 and abs(zero_y - y(lo)) > 12 else ""}
      {y_lo_label}
    </svg>
    """


def fmt_money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def raw_num(v):
    """Numeric value for a td's data-value attribute (sortable), or '' if unavailable."""
    try:
        return f"{float(v):.6f}"
    except (TypeError, ValueError):
        return ""


def build_closed_trades(orders):
    """FIFO-matches filled buy orders against filled sell orders (per symbol, in
    chronological order) to produce a list of closed round-trip trades, each
    with its own P&L. A single buy can be closed by multiple partial sells (or
    vice versa) — each matched chunk becomes its own closed-trade row, sized to
    whichever side had fewer remaining shares.

    Only orders present in the `orders` list are considered, so this reflects
    closed trades within the same "last 50 orders" window shown elsewhere on
    the dashboard — a buy that fell off that window won't be matched.
    """
    filled = [o for o in orders if o.status and o.status.value == "filled" and o.side and o.filled_avg_price]
    filled.sort(key=lambda o: o.filled_at or o.submitted_at)

    buy_queues = defaultdict(deque)  # symbol -> deque[[qty, price, filled_at]]
    closed = []

    for o in filled:
        qty = float(o.qty)
        price = float(o.filled_avg_price)
        filled_at = o.filled_at or o.submitted_at
        side = o.side.value

        if side == "buy":
            buy_queues[o.symbol].append([qty, price, filled_at])
            continue

        if side != "sell":
            continue

        remaining = qty
        queue = buy_queues[o.symbol]
        while remaining > 1e-9 and queue:
            lot = queue[0]
            matched = min(remaining, lot[0])
            pl = matched * (price - lot[1])
            gain_pct = (price - lot[1]) / lot[1] * 100 if lot[1] else 0.0
            closed.append({
                "symbol": o.symbol,
                "shares": matched,
                "buy_price": lot[1],
                "sell_price": price,
                "pl": pl,
                "gain_pct": gain_pct,
                "transaction_date": filled_at,
            })
            lot[0] -= matched
            remaining -= matched
            if lot[0] <= 1e-9:
                queue.popleft()
        # A sell with no matching buy in this order window is skipped rather
        # than shown with a fabricated entry price.

    closed.sort(key=lambda t: t["transaction_date"], reverse=True)
    return closed


def generate(client=None):
    """Regenerates docs/index.html from the current Alpaca account state.

    Pass an already-connected TradingClient to reuse (e.g. from live_bot.py,
    so this doesn't open a second connection); omit it to create one, which
    is what happens when dashboard.py is run standalone.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Tell GitHub Pages not to run this folder through Jekyll (its default site
    # generator) — without this, Jekyll can fail to build a plain hand-written
    # HTML file and the page never deploys. Recreated every run so it's never
    # accidentally missing.
    open(os.path.join(OUTPUT_DIR, ".nojekyll"), "w").close()

    if client is None:
        client = get_client()

    account = client.get_account()
    positions = client.get_all_positions()

    orders_req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=50)
    orders = client.get_orders(orders_req)
    orders = sorted(orders, key=lambda o: o.submitted_at, reverse=True)


    generated_at = datetime.now(ET).strftime("%Y-%m-%d %I:%M %p ET")

    # Company names aren't on the Position/Order objects — look each one up via
    # the assets endpoint. Cached per-symbol since the same symbol often shows
    # up in both the positions table and many rows of the orders table.
    _name_cache = {}

    def company_name(symbol):
        if symbol not in _name_cache:
            try:
                _name_cache[symbol] = client.get_asset(symbol).name
            except Exception:
                _name_cache[symbol] = symbol
        return _name_cache[symbol]

    # Current/previous-close price snapshots for every symbol that appears in
    # the orders table, so "Last Price" / "Daily Change" etc. can be shown for
    # order rows the same way they're shown for open positions (positions get
    # this data directly from the Position object; orders don't carry it).
    order_symbols = sorted({o.symbol for o in orders}) if orders else []
    snapshots = {}
    if order_symbols:
        try:
            data_client = StockHistoricalDataClient(
                os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
            )
            snapshots = data_client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=order_symbols, feed="iex")
            )
        except Exception:
            snapshots = {}

    def snapshot_prices(symbol):
        """Returns (current_price, lastday_price) or (None, None) if unavailable."""
        snap = snapshots.get(symbol)
        if not snap:
            return None, None
        current = None
        if getattr(snap, "latest_trade", None) is not None:
            current = float(snap.latest_trade.price)
        elif getattr(snap, "daily_bar", None) is not None:
            current = float(snap.daily_bar.close)
        lastday = None
        if getattr(snap, "previous_daily_bar", None) is not None:
            lastday = float(snap.previous_daily_bar.close)
        return current, lastday

    positions_rows = ""
    total_cost_basis = total_mkt_value = total_pl = total_todays_change = 0.0
    if positions:
        for p in positions:
            qty = float(p.qty)
            current_price = float(p.current_price)
            avg_entry = float(p.avg_entry_price)
            cost_basis = float(p.cost_basis)
            mkt_value = float(p.market_value)
            pl = float(p.unrealized_pl)
            gain_pct = float(p.unrealized_plpc) * 100

            lastday_price = float(p.lastday_price) if p.lastday_price is not None else current_price
            daily_price_change = current_price - lastday_price
            daily_pct_change = (
                float(p.change_today) * 100 if p.change_today is not None
                else (daily_price_change / lastday_price * 100 if lastday_price else 0.0)
            )
            todays_change = qty * daily_price_change  # today's $ P&L on the position, separate from total unrealized P&L

            total_cost_basis += cost_basis
            total_mkt_value += mkt_value
            total_pl += pl
            total_todays_change += todays_change

            pl_class = "pos" if pl >= 0 else "neg"
            gain_class = "pos" if gain_pct >= 0 else "neg"
            daily_class = "pos" if daily_price_change >= 0 else "neg"
            todays_class = "pos" if todays_change >= 0 else "neg"

            positions_rows += f"""
            <tr>
              <td data-value="{p.symbol}">{p.symbol}</td>
              <td data-value="{company_name(p.symbol)}">{company_name(p.symbol)}</td>
              <td class="num" data-value="{raw_num(qty)}">{p.qty}</td>
              <td class="num" data-value="{raw_num(current_price)}">{fmt_money(current_price)}</td>
              <td class="num" data-value="{raw_num(avg_entry)}">{fmt_money(avg_entry)}</td>
              <td class="num" data-value="{raw_num(cost_basis)}">{fmt_money(cost_basis)}</td>
              <td class="num" data-value="{raw_num(mkt_value)}">{fmt_money(mkt_value)}</td>
              <td class="num {pl_class}" data-value="{raw_num(pl)}">{fmt_money(pl)}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{gain_pct:+.2f}%</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_price_change)}">{fmt_money(daily_price_change)}</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_pct_change)}">{daily_pct_change:+.2f}%</td>
              <td class="num {todays_class}" data-value="{raw_num(todays_change)}">{fmt_money(todays_change)}</td>
            </tr>"""

        total_pl_class = "pos" if total_pl >= 0 else "neg"
        total_todays_class = "pos" if total_todays_change >= 0 else "neg"
        positions_rows += f"""
            <tr class="totals-row">
              <td>Total</td>
              <td></td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num">{fmt_money(total_cost_basis)}</td>
              <td class="num">{fmt_money(total_mkt_value)}</td>
              <td class="num {total_pl_class}">{fmt_money(total_pl)}</td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num {total_todays_class}">{fmt_money(total_todays_change)}</td>
            </tr>"""
    else:
        positions_rows = "<tr><td colspan='12' class='muted'>No open positions</td></tr>"

    orders_rows = ""
    order_statuses_seen = set()
    if orders:
        for o in orders:
            submitted_dt = o.submitted_at.astimezone(ET)
            submitted = submitted_dt.strftime("%Y-%m-%d %I:%M %p")
            side = o.side.value if o.side else "—"
            status = o.status.value if o.status else "—"
            order_statuses_seen.add(status)
            filled_price = float(o.filled_avg_price) if o.filled_avg_price else None
            qty = float(o.qty) if o.qty else 0.0
            side_class = "pos" if side == "buy" else "neg"

            current_price, lastday_price = snapshot_prices(o.symbol)

            # "Purchase Price" / "Cost Basis" mirror the open-positions table: the
            # price this order actually filled at, and qty * that price. Only
            # meaningful for filled orders — unfilled/canceled orders show "—".
            cost_basis = qty * filled_price if filled_price is not None else None
            mkt_value = qty * current_price if current_price is not None else None
            pl = (mkt_value - cost_basis) if (mkt_value is not None and cost_basis is not None) else None
            gain_pct = (pl / cost_basis * 100) if (pl is not None and cost_basis not in (None, 0)) else None

            daily_price_change = (
                current_price - lastday_price
                if (current_price is not None and lastday_price is not None)
                else None
            )
            daily_pct_change = (
                daily_price_change / lastday_price * 100
                if (daily_price_change is not None and lastday_price)
                else None
            )
            todays_change = qty * daily_price_change if daily_price_change is not None else None

            pl_class = "pos" if (pl is not None and pl >= 0) else ("neg" if pl is not None else "")
            gain_class = "pos" if (gain_pct is not None and gain_pct >= 0) else ("neg" if gain_pct is not None else "")
            daily_class = "pos" if (daily_price_change is not None and daily_price_change >= 0) else ("neg" if daily_price_change is not None else "")
            todays_class = "pos" if (todays_change is not None and todays_change >= 0) else ("neg" if todays_change is not None else "")

            orders_rows += f"""
            <tr data-status="{status}">
              <td data-value="{submitted_dt.isoformat()}">{submitted}</td>
              <td data-value="{o.symbol}">{o.symbol}</td>
              <td data-value="{company_name(o.symbol)}">{company_name(o.symbol)}</td>
              <td class="{side_class}" data-value="{side}">{side.upper()}</td>
              <td class="num" data-value="{raw_num(qty)}">{o.qty}</td>
              <td data-value="{status}">{status}</td>
              <td class="num" data-value="{raw_num(current_price)}">{fmt_money(current_price) if current_price is not None else "—"}</td>
              <td class="num" data-value="{raw_num(filled_price)}">{fmt_money(filled_price) if filled_price is not None else "—"}</td>
              <td class="num" data-value="{raw_num(cost_basis)}">{fmt_money(cost_basis) if cost_basis is not None else "—"}</td>
              <td class="num" data-value="{raw_num(mkt_value)}">{fmt_money(mkt_value) if mkt_value is not None else "—"}</td>
              <td class="num {pl_class}" data-value="{raw_num(pl)}">{fmt_money(pl) if pl is not None else "—"}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{f"{gain_pct:+.2f}%" if gain_pct is not None else "—"}</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_price_change)}">{fmt_money(daily_price_change) if daily_price_change is not None else "—"}</td>
              <td class="num {daily_class}" data-value="{raw_num(daily_pct_change)}">{f"{daily_pct_change:+.2f}%" if daily_pct_change is not None else "—"}</td>
              <td class="num {todays_class}" data-value="{raw_num(todays_change)}">{fmt_money(todays_change) if todays_change is not None else "—"}</td>
            </tr>"""
    else:
        orders_rows = "<tr><td colspan='14' class='muted'>No orders yet</td></tr>"

    closed_trades = build_closed_trades(orders)
    closed_rows = ""
    total_closed_pl = 0.0
    total_purchase_cost = 0.0  # sum of shares * buy_price, across all closed trades
    total_sell_proceeds = 0.0  # sum of shares * sell_price, across all closed trades
    if closed_trades:
        for t in closed_trades:
            tx_dt = t["transaction_date"].astimezone(ET)
            pl = t["pl"]
            gain_pct = t["gain_pct"]
            total_closed_pl += pl
            total_purchase_cost += t["shares"] * t["buy_price"]
            total_sell_proceeds += t["shares"] * t["sell_price"]
            pl_class = "pos" if pl >= 0 else "neg"
            gain_class = "pos" if gain_pct >= 0 else "neg"
            closed_rows += f"""
            <tr>
              <td data-value="{tx_dt.isoformat()}">{tx_dt.strftime("%Y-%m-%d %I:%M %p")}</td>
              <td data-value="{t['symbol']}">{t['symbol']}</td>
              <td data-value="{company_name(t['symbol'])}">{company_name(t['symbol'])}</td>
              <td class="num" data-value="{raw_num(t['shares'])}">{t['shares']:g}</td>
              <td class="num" data-value="{raw_num(t['buy_price'])}">{fmt_money(t['buy_price'])}</td>
              <td class="num" data-value="{raw_num(t['sell_price'])}">{fmt_money(t['sell_price'])}</td>
              <td class="num {pl_class}" data-value="{raw_num(pl)}">{fmt_money(pl)}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{gain_pct:+.2f}%</td>
            </tr>"""
        total_class = "pos" if total_closed_pl >= 0 else "neg"
        # Overall % gain/loss for the totals row: total P&L over total capital put
        # in (sum of purchase costs) — the same "$ out vs $ back" basis used for
        # each individual row, just aggregated, rather than an average of the
        # per-trade percentages (which would let a tiny trade's swing skew the
        # total as much as a big one).
        total_gain_pct = (total_closed_pl / total_purchase_cost * 100) if total_purchase_cost else 0.0
        total_gain_class = "pos" if total_gain_pct >= 0 else "neg"
        closed_rows += f"""
            <tr class="totals-row">
              <td>Total</td>
              <td></td>
              <td></td>
              <td></td>
              <td class="num">{fmt_money(total_purchase_cost)}</td>
              <td class="num">{fmt_money(total_sell_proceeds)}</td>
              <td class="num {total_class}">{fmt_money(total_closed_pl)}</td>
              <td class="num {total_gain_class}">{total_gain_pct:+.2f}%</td>
            </tr>"""
    else:
        closed_rows = "<tr><td colspan='8' class='muted'>No closed trades yet</td></tr>"

    # Income by Day — closed_trades rolled up per calendar date (ET), so this
    # reflects the same "last 50 orders" window as the Closed Orders table
    # above it, just aggregated by day instead of shown trade-by-trade.
    daily = defaultdict(lambda: {"trades": 0, "cost_basis": 0.0, "sold_cost": 0.0, "pl": 0.0})
    for t in closed_trades:
        day = t["transaction_date"].astimezone(ET).strftime("%Y-%m-%d")
        d = daily[day]
        d["trades"] += 1
        d["cost_basis"] += t["shares"] * t["buy_price"]
        d["sold_cost"] += t["shares"] * t["sell_price"]
        d["pl"] += t["pl"]

    daily_rows = ""
    if daily:
        for day in sorted(daily.keys(), reverse=True):
            d = daily[day]
            gain_pct = (d["pl"] / d["cost_basis"] * 100) if d["cost_basis"] else 0.0
            pl_class = "pos" if d["pl"] >= 0 else "neg"
            gain_class = "pos" if gain_pct >= 0 else "neg"
            daily_rows += f"""
            <tr>
              <td data-value="{day}">{day}</td>
              <td class="num" data-value="{raw_num(d['trades'])}">{d['trades']}</td>
              <td class="num" data-value="{raw_num(d['cost_basis'])}">{fmt_money(d['cost_basis'])}</td>
              <td class="num" data-value="{raw_num(d['sold_cost'])}">{fmt_money(d['sold_cost'])}</td>
              <td class="num {pl_class}" data-value="{raw_num(d['pl'])}">{fmt_money(d['pl'])}</td>
              <td class="num {gain_class}" data-value="{raw_num(gain_pct)}">{gain_pct:+.2f}%</td>
            </tr>"""
        total_daily_trades = sum(d["trades"] for d in daily.values())
        total_daily_cost_basis = sum(d["cost_basis"] for d in daily.values())
        total_daily_sold_cost = sum(d["sold_cost"] for d in daily.values())
        total_daily_pl = sum(d["pl"] for d in daily.values())
        total_daily_gain_pct = (total_daily_pl / total_daily_cost_basis * 100) if total_daily_cost_basis else 0.0
        total_daily_pl_class = "pos" if total_daily_pl >= 0 else "neg"
        total_daily_gain_class = "pos" if total_daily_gain_pct >= 0 else "neg"
        daily_rows += f"""
            <tr class="totals-row">
              <td>Total</td>
              <td class="num">{total_daily_trades}</td>
              <td class="num">{fmt_money(total_daily_cost_basis)}</td>
              <td class="num">{fmt_money(total_daily_sold_cost)}</td>
              <td class="num {total_daily_pl_class}">{fmt_money(total_daily_pl)}</td>
              <td class="num {total_daily_gain_class}">{total_daily_gain_pct:+.2f}%</td>
            </tr>"""
    else:
        daily_rows = "<tr><td colspan='6' class='muted'>No closed trades yet</td></tr>"

    # Status filter checkboxes — built from whatever statuses actually showed up
    # in the last 50 orders, so the filter row never shows a status with zero
    # matching rows. "Filled" starts checked by default; every other status
    # (canceled, etc.) starts unchecked so the table opens focused on filled
    # orders, and the user can opt back in to the rest.
    status_filters_html = ""
    for status in sorted(order_statuses_seen):
        label = status.replace("_", " ").title()
        checked_attr = "checked " if status == "filled" else ""
        status_filters_html += (
            f'<label class="filter-chip">'
            f'<input type="checkbox" class="status-filter" value="{status}" {checked_attr}'
            f'onchange="filterOrders()"> {label}</label>'
        )
    if not status_filters_html:
        status_filters_html = '<span class="muted">No orders yet</span>'

    # Chart data mirrors the Income by Day table exactly (same `daily` dict,
    # same per-day P&L), just sorted oldest-first for left-to-right plotting.
    daily_pl_points = [(day, daily[day]["pl"]) for day in sorted(daily.keys())]
    daily_pl_chart_html = build_daily_pl_chart(daily_pl_points)

    refresh_button_html = (
        '<button id="refreshBtn" onclick="refreshDashboard()">&#8635; Refresh</button>'
        if REFRESH_ENDPOINT else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude.AI Paper Day Trading</title>
<style>
  :root {{
    --bg: #0b0e14; --panel: #131722; --border: #232838;
    --text: #e6e9ef; --muted: #8b93a7; --pos: #16a34a; --neg: #dc2626; --accent: #3b82f6;
  }}
  :root[data-theme="light"] {{
    --bg: #f5f7fa; --panel: #ffffff; --border: #dde2ec;
    --text: #1a1f2b; --muted: #5b6577; --pos: #158a41; --neg: #c92a2a; --accent: #2563eb;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 24px 24px 76px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    transition: background 0.15s ease, color 0.15s ease;
  }}
  .wrap {{ max-width: 1800px; width: 100%; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  #themeToggleBtn {{
    position: fixed; top: 16px; left: 16px; z-index: 100;
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer;
  }}
  #themeToggleBtn:hover {{ border-color: var(--accent); }}
  .updated {{ color: var(--muted); font-size: 13px; margin-bottom: 24px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }}
  .card .value {{ font-size: 22px; font-weight: 600; }}
  .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
  .panel h2 {{ font-size: 15px; margin: 0 0 14px 0; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: var(--muted); font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:hover {{ color: var(--text); }}
  th.sortable::after {{ content: "⇅"; color: var(--border); margin-left: 6px; font-size: 11px; }}
  th.sortable[data-dir="asc"]::after {{ content: "▲"; color: var(--accent); }}
  th.sortable[data-dir="desc"]::after {{ content: "▼"; color: var(--accent); }}
  td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td.num, th.num {{ text-align: right; }}
  .totals-row td {{ font-weight: 600; border-top: 2px solid var(--border); border-bottom: none; }}
  .pos {{ color: var(--pos); }}
  .neg {{ color: var(--neg); }}
  .muted {{ color: var(--muted); }}
  .pl-chart {{ width: 100%; height: 240px; }}
  .chart-wrap {{ position: relative; }}
  .pl-bar {{ cursor: pointer; }}
  .pl-bar:hover {{ opacity: 0.75; }}
  .pl-tooltip {{
    position: absolute; display: none; pointer-events: none;
    background: #1b2130; border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 10px; font-size: 12px; color: var(--text); white-space: nowrap;
    transform: translate(-50%, -100%); margin-top: -8px; z-index: 10;
  }}
  .disclaimer {{ color: var(--muted); font-size: 12px; margin-top: 28px; line-height: 1.5; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 14px; }}
  .filter-chip {{ display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); cursor: pointer; }}
  .filter-chip input {{ accent-color: var(--accent); cursor: pointer; }}
  #refreshBtn {{
    margin-left: 10px; background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 3px 10px; font-size: 12px; cursor: pointer;
  }}
  #refreshBtn:hover {{ border-color: var(--accent); }}
  #refreshBtn:disabled {{ opacity: 0.5; cursor: default; }}

  @media (max-width: 720px) {{
    body {{ padding: 16px 12px 16px 12px; }}
    #themeToggleBtn {{
      position: static; display: inline-block; margin-bottom: 12px;
    }}
    h1 {{ font-size: 18px; }}
    .cards {{ grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 16px; }}
    .card {{ padding: 12px; }}
    .card .value {{ font-size: 17px; }}
    .panel {{ padding: 12px; margin-bottom: 14px; }}
    .panel h2 {{ font-size: 13px; }}
    table {{ font-size: 12px; }}
    th, td {{ padding: 6px 7px; }}
    .filters {{ gap: 8px; }}
    .updated {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }}
    .pl-chart {{ height: 180px; }}
  }}
</style>
</head>
<body>
  <button id="themeToggleBtn" onclick="toggleTheme()" aria-label="Toggle dark/light theme">&#9728; Light</button>
  <div class="wrap">
    <h1>Claude.AI Paper Day Trading</h1>
    <div class="updated">
      Last updated: <span id="lastUpdated">{generated_at}</span>
      {refresh_button_html}
      <span id="refreshStatus" class="muted"></span>
    </div>

    <div class="cards">
      <div class="card"><div class="label">Equity</div><div class="value">{fmt_money(account.equity)}</div></div>
      <div class="card"><div class="label">Cash</div><div class="value">{fmt_money(account.cash)}</div></div>
      <div class="card"><div class="label">Buying Power</div><div class="value">{fmt_money(account.buying_power)}</div></div>
      <div class="card"><div class="label">Open Positions</div><div class="value">{len(positions)}</div></div>
    </div>

    <div class="panel">
      <h2>Daily Profit / (Loss)</h2>
      <div class="chart-wrap">
        {daily_pl_chart_html}
        <div id="plTooltip" class="pl-tooltip"></div>
      </div>
    </div>

    <div class="panel">
      <h2>Income by Day</h2>
      <div class="table-scroll">
      <table id="dailyTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('dailyTable',0,'text')">Transaction Date</th>
          <th class="sortable num" onclick="sortTable('dailyTable',1,'num')">Total # of Trades</th>
          <th class="sortable num" onclick="sortTable('dailyTable',2,'num')">Total Cost Basis</th>
          <th class="sortable num" onclick="sortTable('dailyTable',3,'num')">Total Sold Cost</th>
          <th class="sortable num" onclick="sortTable('dailyTable',4,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('dailyTable',5,'num')">Gain %</th>
        </tr></thead>
        <tbody>{daily_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <h2>Open Positions</h2>
      <div class="table-scroll">
      <table id="positionsTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('positionsTable',0,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('positionsTable',1,'text')">Company Name</th>
          <th class="sortable num" onclick="sortTable('positionsTable',2,'num')"># of Shares</th>
          <th class="sortable num" onclick="sortTable('positionsTable',3,'num')">Last Price</th>
          <th class="sortable num" onclick="sortTable('positionsTable',4,'num')">Purchase Price</th>
          <th class="sortable num" onclick="sortTable('positionsTable',5,'num')">Cost Basis</th>
          <th class="sortable num" onclick="sortTable('positionsTable',6,'num')">Mkt Value</th>
          <th class="sortable num" onclick="sortTable('positionsTable',7,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('positionsTable',8,'num')">Gain %</th>
          <th class="sortable num" onclick="sortTable('positionsTable',9,'num')">Daily Price Change</th>
          <th class="sortable num" onclick="sortTable('positionsTable',10,'num')">Daily % Change</th>
          <th class="sortable num" onclick="sortTable('positionsTable',11,'num')">Today's Change</th>
        </tr></thead>
        <tbody>{positions_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <h2>Recent Orders (last 50)</h2>
      <div class="filters">{status_filters_html}</div>
      <div class="table-scroll">
      <table id="ordersTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('ordersTable',0,'text')">Submitted</th>
          <th class="sortable" onclick="sortTable('ordersTable',1,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('ordersTable',2,'text')">Company Name</th>
          <th class="sortable" onclick="sortTable('ordersTable',3,'text')">Side</th>
          <th class="sortable num" onclick="sortTable('ordersTable',4,'num')"># of Shares</th>
          <th class="sortable" onclick="sortTable('ordersTable',5,'text')">Status</th>
          <th class="sortable num" onclick="sortTable('ordersTable',6,'num')">Last Price</th>
          <th class="sortable num" onclick="sortTable('ordersTable',7,'num')">Purchase Price</th>
          <th class="sortable num" onclick="sortTable('ordersTable',8,'num')">Cost Basis</th>
          <th class="sortable num" onclick="sortTable('ordersTable',9,'num')">Mkt Value</th>
          <th class="sortable num" onclick="sortTable('ordersTable',10,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('ordersTable',11,'num')">Gain %</th>
          <th class="sortable num" onclick="sortTable('ordersTable',12,'num')">Daily Price Change</th>
          <th class="sortable num" onclick="sortTable('ordersTable',13,'num')">Daily % Change</th>
        </tr></thead>
        <tbody>{orders_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <h2>Closed Orders</h2>
      <div class="table-scroll">
      <table id="closedTable">
        <thead><tr>
          <th class="sortable" onclick="sortTable('closedTable',0,'text')">Transaction Date</th>
          <th class="sortable" onclick="sortTable('closedTable',1,'text')">Symbol</th>
          <th class="sortable" onclick="sortTable('closedTable',2,'text')">Company Name</th>
          <th class="sortable num" onclick="sortTable('closedTable',3,'num')"># of Shares</th>
          <th class="sortable num" onclick="sortTable('closedTable',4,'num')">Purchase Price</th>
          <th class="sortable num" onclick="sortTable('closedTable',5,'num')">Sell Price</th>
          <th class="sortable num" onclick="sortTable('closedTable',6,'num')">Profit / (Loss)</th>
          <th class="sortable num" onclick="sortTable('closedTable',7,'num')">% Gain / Loss</th>
        </tr></thead>
        <tbody>{closed_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="disclaimer">
      This is a PAPER TRADING account — no real money is involved. This dashboard
      is regenerated automatically after each trading session and reflects data
      from Alpaca's paper trading API only.
    </div>
  </div>

  <script>
    function sortTable(tableId, colIdx, type) {{
      const table = document.getElementById(tableId);
      const tbody = table.tBodies[0];
      const rows = Array.from(tbody.querySelectorAll('tr:not(.totals-row)'));
      if (rows.length < 2) return;
      const headerRow = table.tHead.rows[0];
      const th = headerRow.cells[colIdx];
      const asc = th.getAttribute('data-dir') !== 'asc';
      Array.from(headerRow.cells).forEach(c => c.removeAttribute('data-dir'));
      th.setAttribute('data-dir', asc ? 'asc' : 'desc');

      rows.sort((a, b) => {{
        const aCell = a.cells[colIdx], bCell = b.cells[colIdx];
        const av = aCell.getAttribute('data-value') ?? aCell.textContent.trim();
        const bv = bCell.getAttribute('data-value') ?? bCell.textContent.trim();
        let cmp;
        if (type === 'num') {{
          const an = av === '' ? -Infinity : parseFloat(av);
          const bn = bv === '' ? -Infinity : parseFloat(bv);
          cmp = an - bn;
        }} else {{
          cmp = av.localeCompare(bv);
        }}
        return asc ? cmp : -cmp;
      }});

      rows.forEach(r => tbody.appendChild(r));
      // keep the totals row (if any) pinned at the bottom
      const totalsRow = tbody.querySelector('tr.totals-row');
      if (totalsRow) tbody.appendChild(totalsRow);
    }}

    function filterOrders() {{
      const checked = Array.from(document.querySelectorAll('.status-filter:checked')).map(c => c.value);
      document.querySelectorAll('#ordersTable tbody tr[data-status]').forEach(row => {{
        const status = row.getAttribute('data-status');
        row.style.display = checked.includes(status) ? '' : 'none';
      }});
    }}

    (function() {{
      const tooltip = document.getElementById('plTooltip');
      const chartWrap = document.querySelector('.chart-wrap');
      if (!tooltip || !chartWrap) return;
      chartWrap.querySelectorAll('.pl-bar').forEach(bar => {{
        bar.addEventListener('mousemove', e => {{
          const wrapRect = chartWrap.getBoundingClientRect();
          tooltip.textContent = `${{bar.getAttribute('data-date')}}: ${{bar.getAttribute('data-pl')}}`;
          tooltip.style.left = (e.clientX - wrapRect.left) + 'px';
          tooltip.style.top = (e.clientY - wrapRect.top) + 'px';
          tooltip.style.display = 'block';
        }});
        bar.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      }});
    }})();

    // --- Live refresh: pulls fresh account/positions/orders from a small
    // read-only proxy (holds the Alpaca keys server-side) and re-renders the
    // Open Positions / Recent Orders tables + header cards in place. Only
    // active if a proxy URL was configured when this page was generated;
    // Closed Orders / Income by Day / the P&L chart are NOT updated here —
    // those only change once a position actually closes, which requires a
    // real bot session, so they still only refresh via the normal pipeline.
    const REFRESH_ENDPOINT = {REFRESH_ENDPOINT!r};
    const REFRESH_TOKEN = {REFRESH_TOKEN!r};

    function fmtMoneyJS(v) {{
      if (v === null || v === undefined || isNaN(v)) return '—';
      const n = Number(v);
      const sign = n < 0 ? '-' : '';
      return sign + '$' + Math.abs(n).toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    }}
    function pctJS(v) {{
      if (v === null || v === undefined || isNaN(v)) return '—';
      return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
    }}
    function cls(v) {{ return (v === null || v === undefined || isNaN(v)) ? '' : (v >= 0 ? 'pos' : 'neg'); }}

    function snapshotPrices(snapshots, symbol) {{
      const snap = snapshots ? snapshots[symbol] : null;
      if (!snap) return [null, null];
      let current = null;
      if (snap.latestTrade && snap.latestTrade.p != null) current = Number(snap.latestTrade.p);
      else if (snap.dailyBar && snap.dailyBar.c != null) current = Number(snap.dailyBar.c);
      let lastday = null;
      if (snap.prevDailyBar && snap.prevDailyBar.c != null) lastday = Number(snap.prevDailyBar.c);
      return [current, lastday];
    }}

    function buildPositionsRows(positions, names) {{
      if (!positions || positions.length === 0) {{
        return "<tr><td colspan='12' class='muted'>No open positions</td></tr>";
      }}
      let rows = '';
      let totalCost = 0, totalMkt = 0, totalPl = 0, totalToday = 0;
      positions.forEach(p => {{
        const qty = Number(p.qty);
        const current = Number(p.current_price);
        const avgEntry = Number(p.avg_entry_price);
        const costBasis = Number(p.cost_basis);
        const mktValue = Number(p.market_value);
        const pl = Number(p.unrealized_pl);
        const gainPct = Number(p.unrealized_plpc) * 100;
        const lastday = p.lastday_price != null ? Number(p.lastday_price) : current;
        const dailyChange = current - lastday;
        const dailyPct = p.change_today != null ? Number(p.change_today) * 100
          : (lastday ? (dailyChange / lastday * 100) : 0);
        const todaysChange = qty * dailyChange;
        totalCost += costBasis; totalMkt += mktValue; totalPl += pl; totalToday += todaysChange;
        const name = (names && names[p.symbol]) || p.symbol;
        rows += `<tr>
          <td data-value="${{p.symbol}}">${{p.symbol}}</td>
          <td data-value="${{name}}">${{name}}</td>
          <td class="num" data-value="${{qty}}">${{p.qty}}</td>
          <td class="num" data-value="${{current}}">${{fmtMoneyJS(current)}}</td>
          <td class="num" data-value="${{avgEntry}}">${{fmtMoneyJS(avgEntry)}}</td>
          <td class="num" data-value="${{costBasis}}">${{fmtMoneyJS(costBasis)}}</td>
          <td class="num" data-value="${{mktValue}}">${{fmtMoneyJS(mktValue)}}</td>
          <td class="num ${{cls(pl)}}" data-value="${{pl}}">${{fmtMoneyJS(pl)}}</td>
          <td class="num ${{cls(gainPct)}}" data-value="${{gainPct}}">${{pctJS(gainPct)}}</td>
          <td class="num ${{cls(dailyChange)}}" data-value="${{dailyChange}}">${{fmtMoneyJS(dailyChange)}}</td>
          <td class="num ${{cls(dailyPct)}}" data-value="${{dailyPct}}">${{pctJS(dailyPct)}}</td>
          <td class="num ${{cls(todaysChange)}}" data-value="${{todaysChange}}">${{fmtMoneyJS(todaysChange)}}</td>
        </tr>`;
      }});
      rows += `<tr class="totals-row">
        <td>Total</td><td></td><td></td><td></td><td></td>
        <td class="num">${{fmtMoneyJS(totalCost)}}</td>
        <td class="num">${{fmtMoneyJS(totalMkt)}}</td>
        <td class="num ${{cls(totalPl)}}">${{fmtMoneyJS(totalPl)}}</td>
        <td></td><td></td><td></td>
        <td class="num ${{cls(totalToday)}}">${{fmtMoneyJS(totalToday)}}</td>
      </tr>`;
      return rows;
    }}

    function buildOrdersRows(orders, names, snapshots) {{
      if (!orders || orders.length === 0) {{
        return "<tr><td colspan='14' class='muted'>No orders yet</td></tr>";
      }}
      let rows = '';
      orders.forEach(o => {{
        const submittedDt = new Date(o.submitted_at);
        const submitted = submittedDt.toLocaleString('en-US', {{
          timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
        }});
        const side = o.side || '—';
        const status = o.status || '—';
        const filledPrice = o.filled_avg_price != null ? Number(o.filled_avg_price) : null;
        const qty = o.qty != null ? Number(o.qty) : 0;
        const [current, lastday] = snapshotPrices(snapshots, o.symbol);
        const costBasis = filledPrice != null ? qty * filledPrice : null;
        const mktValue = current != null ? qty * current : null;
        const pl = (mktValue != null && costBasis != null) ? (mktValue - costBasis) : null;
        const gainPct = (pl != null && costBasis) ? (pl / costBasis * 100) : null;
        const dailyChange = (current != null && lastday != null) ? (current - lastday) : null;
        const dailyPct = (dailyChange != null && lastday) ? (dailyChange / lastday * 100) : null;
        const name = (names && names[o.symbol]) || o.symbol;
        rows += `<tr data-status="${{status}}">
          <td data-value="${{submittedDt.toISOString()}}">${{submitted}}</td>
          <td data-value="${{o.symbol}}">${{o.symbol}}</td>
          <td data-value="${{name}}">${{name}}</td>
          <td class="${{side === 'buy' ? 'pos' : 'neg'}}" data-value="${{side}}">${{side.toUpperCase()}}</td>
          <td class="num" data-value="${{qty}}">${{o.qty}}</td>
          <td data-value="${{status}}">${{status}}</td>
          <td class="num" data-value="${{current ?? ''}}">${{current != null ? fmtMoneyJS(current) : '—'}}</td>
          <td class="num" data-value="${{filledPrice ?? ''}}">${{filledPrice != null ? fmtMoneyJS(filledPrice) : '—'}}</td>
          <td class="num" data-value="${{costBasis ?? ''}}">${{costBasis != null ? fmtMoneyJS(costBasis) : '—'}}</td>
          <td class="num" data-value="${{mktValue ?? ''}}">${{mktValue != null ? fmtMoneyJS(mktValue) : '—'}}</td>
          <td class="num ${{cls(pl)}}" data-value="${{pl ?? ''}}">${{pl != null ? fmtMoneyJS(pl) : '—'}}</td>
          <td class="num ${{cls(gainPct)}}" data-value="${{gainPct ?? ''}}">${{gainPct != null ? pctJS(gainPct) : '—'}}</td>
          <td class="num ${{cls(dailyChange)}}" data-value="${{dailyChange ?? ''}}">${{dailyChange != null ? fmtMoneyJS(dailyChange) : '—'}}</td>
          <td class="num ${{cls(dailyPct)}}" data-value="${{dailyPct ?? ''}}">${{dailyPct != null ? pctJS(dailyPct) : '—'}}</td>
        </tr>`;
      }});
      return rows;
    }}

    async function refreshDashboard() {{
      if (!REFRESH_ENDPOINT) return;
      const btn = document.getElementById('refreshBtn');
      const statusEl = document.getElementById('refreshStatus');
      btn.disabled = true;
      statusEl.textContent = ' Refreshing…';
      try {{
        const url = REFRESH_ENDPOINT + '?token=' + encodeURIComponent(REFRESH_TOKEN) + '&_=' + Date.now();
        const resp = await fetch(url);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));

        document.querySelector('.card:nth-child(1) .value').textContent = fmtMoneyJS(data.account.equity);
        document.querySelector('.card:nth-child(2) .value').textContent = fmtMoneyJS(data.account.cash);
        document.querySelector('.card:nth-child(3) .value').textContent = fmtMoneyJS(data.account.buying_power);
        document.querySelector('.card:nth-child(4) .value').textContent = (data.positions || []).length;

        document.querySelector('#positionsTable tbody').innerHTML = buildPositionsRows(data.positions, data.names);
        document.querySelector('#ordersTable tbody').innerHTML = buildOrdersRows(data.orders, data.names, data.snapshots);
        filterOrders();

        const now = new Date().toLocaleString('en-US', {{
          timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
        }});
        document.getElementById('lastUpdated').textContent = now + ' ET (live refresh — Closed Orders/Income by Day still reflect the last full session)';
        statusEl.textContent = '';
      }} catch (e) {{
        statusEl.textContent = ' Refresh failed: ' + e.message;
      }} finally {{
        btn.disabled = false;
      }}
    }}

    // Apply the default status filter (Filled only, checked above) on first
    // load, same as if the user had just toggled the checkboxes themselves.
    filterOrders();

    // --- Dark / light theme toggle ------------------------------------------------
    // Defaults to dark (matches the original look) and remembers the choice
    // per-browser via localStorage. Wrapped in try/catch since localStorage
    // can throw in some browser contexts (private mode, blocked storage).
    function getStoredTheme() {{
      try {{ return localStorage.getItem('orb-dashboard-theme'); }} catch (e) {{ return null; }}
    }}
    function storeTheme(theme) {{
      try {{ localStorage.setItem('orb-dashboard-theme', theme); }} catch (e) {{ /* ignore */ }}
    }}
    function applyTheme(theme) {{
      document.documentElement.setAttribute('data-theme', theme);
      const btn = document.getElementById('themeToggleBtn');
      if (btn) btn.textContent = theme === 'light' ? '☽ Dark' : '☀ Light';
    }}
    function toggleTheme() {{
      const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
      const next = current === 'light' ? 'dark' : 'light';
      applyTheme(next);
      storeTheme(next);
    }}
    applyTheme(getStoredTheme() === 'light' ? 'light' : 'dark');
  </script>
</body>
</html>"""

    with open(OUTPUT_FILE, "w") as f:
        f.write(html)

    print(f"Dashboard written to {OUTPUT_FILE}")


def main():
    generate()


if __name__ == "__main__":
    main()
