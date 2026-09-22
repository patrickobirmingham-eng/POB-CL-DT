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


def get_client():
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID / APCA_API_SECRET_KEY first.")
    return TradingClient(key, secret, paper=True)


def build_equity_sparkline(points, width=600, height=120, pad=10):
    """points: list of (datetime, float equity). Returns an inline SVG string."""
    if len(points) < 2:
        return "<p class='muted'>Not enough history yet for a chart.</p>"

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(points)

    def x(i):
        return pad + (i / (n - 1)) * (width - 2 * pad)

    def y(v):
        return height - pad - ((v - lo) / span) * (height - 2 * pad)

    path_pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(points))
    start_val, end_val = values[0], values[-1]
    color = "#16a34a" if end_val >= start_val else "#dc2626"

    return f"""
    <svg viewBox="0 0 {width} {height}" class="sparkline" preserveAspectRatio="none">
      <polyline fill="none" stroke="{color}" stroke-width="2.5" points="{path_pts}" />
    </svg>
    <div class="spark-range">
      <span>${lo:,.0f}</span><span>${hi:,.0f}</span>
    </div>
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

    # Portfolio history for the sparkline (last 30 calendar days, daily)
    equity_points = []
    try:
        history = client.get_portfolio_history(history_filter=None)
    except Exception:
        history = None
    if history and getattr(history, "timestamp", None) and getattr(history, "equity", None):
        for ts, eq in zip(history.timestamp, history.equity):
            if eq is None:
                continue
            equity_points.append((datetime.fromtimestamp(ts, tz=ET), float(eq)))

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
            snapshots = data_client.get_stock_snapshots(
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

    # Status filter checkboxes — built from whatever statuses actually showed up
    # in the last 50 orders, so the filter row never shows a status with zero
    # matching rows. All start checked (nothing filtered out by default).
    status_filters_html = ""
    for status in sorted(order_statuses_seen):
        label = status.replace("_", " ").title()
        status_filters_html += (
            f'<label class="filter-chip">'
            f'<input type="checkbox" class="status-filter" value="{status}" checked '
            f'onchange="filterOrders()"> {label}</label>'
        )
    if not status_filters_html:
        status_filters_html = '<span class="muted">No orders yet</span>'

    sparkline_html = build_equity_sparkline(equity_points)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ORB Paper Trading Dashboard</title>
<style>
  :root {{
    --bg: #0b0e14; --panel: #131722; --border: #232838;
    --text: #e6e9ef; --muted: #8b93a7; --pos: #16a34a; --neg: #dc2626; --accent: #3b82f6;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 1800px; width: 100%; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
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
  .sparkline {{ width: 100%; height: 120px; }}
  .spark-range {{ display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-top: 4px; }}
  .disclaimer {{ color: var(--muted); font-size: 12px; margin-top: 28px; line-height: 1.5; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 14px; }}
  .filter-chip {{ display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); cursor: pointer; }}
  .filter-chip input {{ accent-color: var(--accent); cursor: pointer; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>ORB Paper Trading Dashboard</h1>
    <div class="updated">Last updated: {generated_at}</div>

    <div class="cards">
      <div class="card"><div class="label">Equity</div><div class="value">{fmt_money(account.equity)}</div></div>
      <div class="card"><div class="label">Cash</div><div class="value">{fmt_money(account.cash)}</div></div>
      <div class="card"><div class="label">Buying Power</div><div class="value">{fmt_money(account.buying_power)}</div></div>
      <div class="card"><div class="label">Open Positions</div><div class="value">{len(positions)}</div></div>
    </div>

    <div class="panel">
      <h2>Equity (last 30 days)</h2>
      {sparkline_html}
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
