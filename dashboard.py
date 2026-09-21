"""
Generates a static HTML dashboard (docs/index.html) summarizing the paper
trading account: current equity, an equity curve, open positions, and recent
order history. Meant to be run as a step in the GitHub Actions workflow after
each bot session, with the output committed and served via GitHub Pages.

This is read-only — it never places or modifies orders.
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

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

    # Company names aren't on the Position object itself — look each one up via
    # the assets endpoint (cheap, and there are at most a handful of open
    # positions at once). Falls back to the bare symbol if a lookup fails.
    def company_name(symbol):
        try:
            return client.get_asset(symbol).name
        except Exception:
            return symbol

    positions_rows = ""
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

            pl_class = "pos" if pl >= 0 else "neg"
            gain_class = "pos" if gain_pct >= 0 else "neg"
            daily_class = "pos" if daily_price_change >= 0 else "neg"
            todays_class = "pos" if todays_change >= 0 else "neg"

            positions_rows += f"""
            <tr>
              <td>{p.symbol}</td>
              <td>{company_name(p.symbol)}</td>
              <td>{p.qty}</td>
              <td>{fmt_money(current_price)}</td>
              <td>{fmt_money(avg_entry)}</td>
              <td>{fmt_money(cost_basis)}</td>
              <td>{fmt_money(mkt_value)}</td>
              <td class="{pl_class}">{fmt_money(pl)}</td>
              <td class="{gain_class}">{gain_pct:+.2f}%</td>
              <td class="{daily_class}">{fmt_money(daily_price_change)}</td>
              <td class="{daily_class}">{daily_pct_change:+.2f}%</td>
              <td class="{todays_class}">{fmt_money(todays_change)}</td>
            </tr>"""
    else:
        positions_rows = "<tr><td colspan='12' class='muted'>No open positions</td></tr>"

    orders_rows = ""
    if orders:
        for o in orders:
            submitted = o.submitted_at.astimezone(ET).strftime("%Y-%m-%d %I:%M %p")
            side = o.side.value if o.side else "—"
            status = o.status.value if o.status else "—"
            filled_price = fmt_money(o.filled_avg_price) if o.filled_avg_price else "—"
            side_class = "pos" if side == "buy" else "neg"
            orders_rows += f"""
            <tr>
              <td>{submitted}</td>
              <td>{o.symbol}</td>
              <td class="{side_class}">{side.upper()}</td>
              <td>{o.qty}</td>
              <td>{status}</td>
              <td>{filled_price}</td>
            </tr>"""
    else:
        orders_rows = "<tr><td colspan='6' class='muted'>No orders yet</td></tr>"

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
  .wrap {{ max-width: 900px; margin: 0 auto; }}
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
  td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  .pos {{ color: var(--pos); }}
  .neg {{ color: var(--neg); }}
  .muted {{ color: var(--muted); }}
  .sparkline {{ width: 100%; height: 120px; }}
  .spark-range {{ display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-top: 4px; }}
  .disclaimer {{ color: var(--muted); font-size: 12px; margin-top: 28px; line-height: 1.5; }}
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
      <table>
        <thead><tr>
          <th>Symbol</th><th>Company Name</th><th># of Shares</th><th>Last Price</th>
          <th>Purchase Price</th><th>Cost Basis</th><th>Mkt Value</th><th>Profit / (Loss)</th>
          <th>Gain %</th><th>Daily Price Change</th><th>Daily % Change</th><th>Today's Change</th>
        </tr></thead>
        <tbody>{positions_rows}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <h2>Recent Orders (last 50)</h2>
      <table>
        <thead><tr><th>Submitted</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Filled Price</th></tr></thead>
        <tbody>{orders_rows}</tbody>
      </table>
    </div>

    <div class="disclaimer">
      This is a PAPER TRADING account — no real money is involved. This dashboard
      is regenerated automatically after each trading session and reflects data
      from Alpaca's paper trading API only.
    </div>
  </div>
</body>
</html>"""

    with open(OUTPUT_FILE, "w") as f:
        f.write(html)

    print(f"Dashboard written to {OUTPUT_FILE}")


def main():
    generate()


if __name__ == "__main__":
    main()
