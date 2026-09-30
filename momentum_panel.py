"""
Dashboard panel for the QQQ intraday momentum strategy: today's price with the
Concretum Bands (the strategy's "noise area"), VWAP, the 30-minute check
points, the bot's QQQ fills, and its current position/stop.

Everything is computed with momentum_strategy.py — the same code live_bot.py
trades with — so the chart always matches what the bot sees. Read-only.
"""
import html
import json
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import config
import momentum_strategy as ms

ET = ZoneInfo("America/New_York")


def _bars(data_client, sym, start, end, feed):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    req = StockBarsRequest(symbol_or_symbols=[sym], timeframe=TimeFrame.Minute, start=start, end=end, feed=feed)
    df = data_client.get_stock_bars(req).df
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(ET)
    return df.set_index("timestamp").sort_index().between_time("09:30", "15:59")


def load_session(data_client, sym, now):
    """Returns today's session (or the most recent one, outside market hours)
    with its bands, or None if there isn't enough data."""
    hist = _bars(data_client, sym, now - timedelta(days=45), now - timedelta(minutes=16), "sip")
    if hist.empty:
        return None
    today = now.date()
    today_iex = pd.DataFrame()
    if now.weekday() < 5 and now.time() >= dtime(9, 31):
        try:
            today_iex = _bars(data_client, sym, datetime.combine(today, dtime(9, 30), tzinfo=ET), now, "iex")
        except Exception:
            today_iex = pd.DataFrame()
    sessions = {d: g for d, g in hist.groupby(hist.index.date)}
    if not today_iex.empty:
        day, raw = today, today_iex   # same data source the live bot uses for today
        sip_today = sessions.get(today)
        day_open = float(sip_today["open"].iloc[0]) if sip_today is not None else float(raw["open"].iloc[0])
    else:
        full = [d for d, g in sessions.items() if len(g) >= 300]
        if not full:
            return None
        day = max(full)
        raw = sessions[day]
        day_open = float(raw["open"].iloc[0])
    prior = [ms.prepare_day(sessions[d])[0] for d in sorted(sessions) if d < day and len(sessions[d]) >= 300]
    if len(prior) < ms.LOOKBACK + 1:
        return None
    prepared, _ = ms.prepare_day(raw)
    last_label = raw.index[-1].strftime("%H:%M")
    closes = [float(p["close"].iloc[-1]) for p in prior]
    sigma = ms.sigma_profile(prior)
    upper, lower = ms.bands(day_open, closes[-1], sigma)
    return {
        "date": day, "is_today": day == today, "bars": prepared, "last": last_label,
        "open": day_open, "prev_close": closes[-1], "upper": upper, "lower": lower,
        "vol": ms.daily_vol(closes),
    }


def _money(v):
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def build_momentum_panel(data_client, positions, orders, equity, now=None):
    """HTML for the dashboard panel (or a short notice if data is unavailable)."""
    sym = getattr(config, "MOMENTUM_SYMBOL", "QQQ")
    enabled = getattr(config, "MOMENTUM_ENABLED", False)
    now = now or datetime.now(ET)
    title = f"{sym} Momentum Strategy — Concretum Bands"
    try:
        s = load_session(data_client, sym, now)
    except Exception as e:
        s = None
        print(f"momentum panel: data unavailable ({e})")
    if s is None:
        return (f'<div class="panel"><div class="panel-head"><h2>{title}</h2></div>'
                f'<p class="muted">Band data is not available right now.</p></div>')

    b, last = s["bars"], s["last"]
    grid = list(b.index)
    upto = grid[:grid.index(last) + 1]
    price = b.loc[upto, "close"]
    vwap = b.loc[upto, "vwap"]
    upper, lower = s["upper"], s["lower"]
    px, vw, ub, lb = float(price.iloc[-1]), float(vwap.iloc[-1]), float(upper[last]), float(lower[last])
    zone = "above" if px > ub else ("below" if px < lb else "inside")

    # ----- position / sizing
    held = next((p for p in positions if p.symbol == sym), None)
    qty = float(held.qty) if held is not None else 0.0
    shares_today = ms.position_size(float(equity), s["open"], s["vol"],
                                    float(getattr(config, "MOMENTUM_TARGET_VOL", 0.01)),
                                    float(getattr(config, "MOMENTUM_MAX_LEVERAGE", 2.0)))
    if qty > 0:
        stop = max(ub, vw)
        pos_html = (f'<span class="mom-badge pos">LONG {qty:,.0f} {sym}</span> entry {_money(float(held.avg_entry_price))}, '
                    f'P&amp;L <span class="{"pos" if float(held.unrealized_pl) >= 0 else "neg"}">{_money(float(held.unrealized_pl))}</span>, '
                    f'exits if price &lt; {_money(stop)}')
    elif qty < 0:
        stop = min(lb, vw)
        pos_html = (f'<span class="mom-badge neg">SHORT {abs(qty):,.0f} {sym}</span> entry {_money(float(held.avg_entry_price))}, '
                    f'P&amp;L <span class="{"pos" if float(held.unrealized_pl) >= 0 else "neg"}">{_money(float(held.unrealized_pl))}</span>, '
                    f'exits if price &gt; {_money(stop)}')
    else:
        # A just-sent order is not a position yet — say so instead of "FLAT".
        open_status = ("new", "pending_new", "accepted", "partially_filled")
        def _val(v):
            return str(getattr(v, "value", v) or "").lower()
        pending = next((o for o in orders or []
                        if getattr(o, "symbol", None) == sym
                        and _val(getattr(o, "status", None)) in open_status), None)
        if pending is not None:
            side = _val(getattr(pending, "side", None)).upper()
            pos_html = (f'<span class="mom-badge {"pos" if side == "BUY" else "neg"}">ORDER SENT</span> '
                        f'{side} {float(pending.qty or 0):,.0f} {sym} — waiting for the fill')
        else:
            pos_html = '<span class="mom-badge">FLAT</span> no position'
    zone_html = {
        "above": f'<span class="mom-badge pos">Above the band</span> breakout up (long signal at the next check)',
        "below": f'<span class="mom-badge neg">Below the band</span> breakout down (short signal at the next check)',
        "inside": '<span class="mom-badge">Inside the band</span> normal noise — no signal',
    }[zone]

    # ----- chart geometry
    W, H, L, R, T, B = 900, 300, 64, 16, 14, 30
    n = len(grid)
    vis = list(price) + list(vwap) + list(upper) + list(lower)
    lo, hi = min(vis), max(vis)
    pad = (hi - lo) * 0.06 or 0.5
    lo, hi = lo - pad, hi + pad
    x = lambda i: L + i * (W - L - R) / (n - 1)
    y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)
    idx = {t: i for i, t in enumerate(grid)}

    def path(series, labels):
        return " ".join(f"{'M' if k == 0 else 'L'}{x(idx[t]):.1f},{y(float(v)):.1f}" for k, (t, v) in enumerate(zip(labels, series)))

    band_area = (path(upper, grid) + " " +
                 " ".join(f"L{x(idx[t]):.1f},{y(float(lower[t])):.1f}" for t in reversed(grid)) + " Z")
    svg = [f'<svg viewBox="0 0 {W} {H}" class="mom-chart" role="img" aria-label="{sym} price with Concretum Bands">']
    for k in range(5):  # horizontal grid + price labels
        v = lo + (hi - lo) * k / 4
        svg.append(f'<line class="pl-grid" x1="{L}" y1="{y(v):.1f}" x2="{W - R}" y2="{y(v):.1f}"/>'
                   f'<text class="pl-axis" x="{L - 8}" y="{y(v) + 3:.1f}" text-anchor="end">{v:,.2f}</text>')
    for t in ms.CHECK_TIMES:  # 30-minute check points
        bar = ms.CHECK_BARS[t]
        xx = x(idx[bar])
        svg.append(f'<line class="mom-check" x1="{xx:.1f}" y1="{T}" x2="{xx:.1f}" y2="{H - B}"/>')
        if t.endswith(":00"):
            svg.append(f'<text class="pl-axis" x="{xx:.1f}" y="{H - B + 16}" text-anchor="middle">{int(t[:2]) % 12 or 12}{"am" if int(t[:2]) < 12 else "pm"}</text>')
    # Transparent hit area under the lines so the whole plot responds to the pointer.
    svg.append(f'<rect class="mom-hit" x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}"/>')
    svg.append(f'<path class="mom-band" d="{band_area}"/>')
    svg.append(f'<path class="mom-bound" d="{path(upper, grid)}"/><path class="mom-bound" d="{path(lower, grid)}"/>')
    svg.append(f'<path class="mom-vwap" d="{path(vwap, upto)}"/>')
    svg.append(f'<path class="mom-price" d="{path(price, upto)}"/>')

    # bot fills for this symbol on the session day
    fills = []
    for o in orders or []:
        try:
            if o.symbol != sym or not o.filled_at or not o.filled_avg_price:
                continue
            ft = o.filled_at.astimezone(ET)
            if ft.date() != s["date"]:
                continue
            lbl = max("09:30", min(ft.strftime("%H:%M"), "15:59"))
            fills.append((lbl, o.side.value, float(o.filled_avg_price), float(o.filled_qty or o.qty or 0), ft))
        except Exception:
            continue
    for lbl, side, fp, fq, ft in sorted(fills, key=lambda f: f[4]):
        xx, yy = x(idx[lbl]), y(fp)
        shape = (f"{xx:.1f},{yy - 9:.1f} {xx - 7:.1f},{yy + 5:.1f} {xx + 7:.1f},{yy + 5:.1f}" if side == "buy"
                 else f"{xx:.1f},{yy + 9:.1f} {xx - 7:.1f},{yy - 5:.1f} {xx + 7:.1f},{yy - 5:.1f}")
        svg.append(f'<polygon class="mom-fill {"buy" if side == "buy" else "sell"}" points="{shape}">'
                   f'<title>{side.upper()} {fq:,.0f} @ {_money(fp)} — {ft.strftime("%I:%M %p")}</title></polygon>')
    # Hover readout: a vertical guide + one dot per line, filled in by MOM_HOVER_JS.
    svg.append(f'<g class="mom-hover" style="display:none"><line class="mom-guide" x1="0" y1="{T}" x2="0" y2="{H - B}"/>'
               '<circle class="mom-dot price" r="4"/><circle class="mom-dot band up" r="3.5"/>'
               '<circle class="mom-dot band lo" r="3.5"/><circle class="mom-dot vwap" r="3.5"/></g>')
    svg.append("</svg>")
    upto_set = set(upto)
    hover = {
        "geo": [W, H, L, R, T, B, lo, hi], "t": grid,
        "p": [round(float(b.at[t, "close"]), 2) if t in upto_set else None for t in grid],
        "u": [round(float(upper[t]), 2) for t in grid], "l": [round(float(lower[t]), 2) for t in grid],
        "v": [round(float(b.at[t, "vwap"]), 2) if t in upto_set else None for t in grid],
    }
    hover_json = html.escape(json.dumps(hover, separators=(",", ":")), quote=True)

    # ----- check-point table
    rows = ""
    for t in ms.CHECK_TIMES:
        bar = ms.CHECK_BARS[t]
        if bar > last:
            break
        p, u, l_, v = float(b.at[bar, "close"]), float(upper[bar]), float(lower[bar]), float(b.at[bar, "vwap"])
        z = "Above band" if p > u else ("Below band" if p < l_ else "Inside")
        zc = "pos" if p > u else ("neg" if p < l_ else "muted")
        hh = int(t[:2]) % 12 or 12
        rows += (f'<tr><td>{hh}:{t[3:]} {"am" if int(t[:2]) < 12 else "pm"}</td><td class="num">{_money(p)}</td>'
                 f'<td class="num">{_money(u)}</td><td class="num">{_money(l_)}</td><td class="num">{_money(v)}</td>'
                 f'<td class="{zc}">{z}</td></tr>')
    if not rows:
        rows = '<tr><td colspan="6" class="muted">First check is at 10:00 am ET.</td></tr>'

    when = "Today" if s["is_today"] else f"Last session ({s['date'].strftime('%a %b %d')})"
    status = "" if enabled else ' <span class="mom-badge neg">strategy OFF in Settings</span>'
    return f"""
    <div class="panel" id="momentumPanel">
      <div class="panel-head">
        <h2>{title}</h2>
        <span class="muted ai-summary">{when} &middot; as of {int(last[:2]) % 12 or 12}:{last[3:]} {"am" if int(last[:2]) < 12 else "pm"} ET{status}</span>
      </div>
      <div class="mom-status">
        <div><span class="label">{sym}</span> <strong>{_money(px)}</strong> &middot; band {_money(lb)} – {_money(ub)} &middot; VWAP {_money(vw)}</div>
        <div>{zone_html}</div>
        <div><span class="label">Bot</span> {pos_html}</div>
        <div class="muted">Size today: {shares_today:,} shares (~{_money(shares_today * s['open'])}) &middot; {sym} daily volatility {s['vol']:.2%}</div>
      </div>
      <div class="table-scroll"><div class="chart-wrap mom-wrap" data-hover="{hover_json}">{"".join(svg)}<div class="mom-tip" style="display:none"></div></div></div>
      <div class="mom-legend">
        <span><i class="sw price"></i>{sym} price</span><span><i class="sw band"></i>Concretum Bands (noise area)</span>
        <span><i class="sw vwap"></i>VWAP</span><span><i class="sw check"></i>30-min checks</span>
        <span class="pos">▲ buy</span><span class="neg">▼ sell</span>
      </div>
      <div class="table-scroll">
      <table>
        <thead><tr><th>Check</th><th>Price</th><th>Upper band</th><th>Lower band</th><th>VWAP</th><th>Signal</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>
    </div>
    <script>{MOM_HOVER_JS}</script>"""


# Crosshair readout for the chart: nearest minute to the pointer (mouse or touch).
MOM_HOVER_JS = """
(function () {
  var wrap = document.querySelector('#momentumPanel .mom-wrap');
  if (!wrap) return;
  var d = JSON.parse(wrap.getAttribute('data-hover'));
  var W = d.geo[0], H = d.geo[1], L = d.geo[2], R = d.geo[3], T = d.geo[4], B = d.geo[5], lo = d.geo[6], hi = d.geo[7];
  var n = d.t.length, svg = wrap.querySelector('svg'), g = wrap.querySelector('.mom-hover');
  var tip = wrap.querySelector('.mom-tip'), guide = g.querySelector('.mom-guide');
  var dots = { p: g.querySelector('.price'), u: g.querySelector('.up'), l: g.querySelector('.lo'), v: g.querySelector('.vwap') };
  function X(i) { return L + i * (W - L - R) / (n - 1); }
  function Y(v) { return T + (hi - v) / (hi - lo) * (H - T - B); }
  function money(v) { return v == null ? '—' : '$' + v.toFixed(2); }
  function label(t) { var h = +t.slice(0, 2); return (h % 12 || 12) + ':' + t.slice(3) + (h < 12 ? ' am' : ' pm'); }
  function show(ev) {
    var r = svg.getBoundingClientRect();
    var vx = (ev.clientX - r.left) / r.width * W;
    var i = Math.max(0, Math.min(n - 1, Math.round((vx - L) / (W - L - R) * (n - 1))));
    var x = X(i);
    g.style.display = '';
    guide.setAttribute('x1', x); guide.setAttribute('x2', x);
    ['p', 'u', 'l', 'v'].forEach(function (k) {
      var v = d[k][i];
      dots[k].style.display = v == null ? 'none' : '';
      if (v != null) { dots[k].setAttribute('cx', x); dots[k].setAttribute('cy', Y(v)); }
    });
    tip.innerHTML = '<div class="mom-tip-time">' + label(d.t[i]) + ' ET</div>' +
      '<div><i class="sw price"></i>Price <b>' + money(d.p[i]) + '</b></div>' +
      '<div><i class="sw band"></i>Upper band <b>' + money(d.u[i]) + '</b></div>' +
      '<div><i class="sw band"></i>Lower band <b>' + money(d.l[i]) + '</b></div>' +
      '<div><i class="sw vwap"></i>VWAP <b>' + money(d.v[i]) + '</b></div>';
    tip.style.display = '';
    // Keep the box inside what's visible (the chart scrolls sideways on phones).
    var px = x / W * r.width, wr = wrap.getBoundingClientRect();
    var view = (wrap.parentElement || wrap).getBoundingClientRect();
    var visL = Math.max(0, view.left - wr.left), visR = Math.min(wr.width, view.right - wr.left);
    var tw = tip.offsetWidth;
    var left = px + 14 + tw <= visR ? px + 14 : px - 14 - tw;
    tip.style.left = Math.max(visL, Math.min(left, visR - tw)) + 'px';
    tip.style.top = (T / H * r.height + 4) + 'px';
  }
  function hide() { g.style.display = 'none'; tip.style.display = 'none'; }
  svg.addEventListener('pointermove', show);
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('pointerleave', hide);
})();
"""


PANEL_CSS = """
  .mom-chart { width: 100%; height: auto; display: block; }
  .mom-wrap { min-width: 620px; position: relative; }
  .mom-hit { fill: transparent; cursor: crosshair; }
  .mom-guide { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; pointer-events: none; }
  .mom-dot { stroke: var(--panel); stroke-width: 1.5; pointer-events: none; }
  .mom-dot.price { fill: var(--text); }
  .mom-dot.band { fill: var(--accent); }
  .mom-dot.vwap { fill: var(--vwap); }
  .mom-tip { position: absolute; pointer-events: none; z-index: 2; background: var(--panel-2); color: var(--text);
             border: 1px solid var(--border-strong); border-radius: 8px; padding: 8px 10px; font-size: 12px;
             line-height: 1.6; white-space: nowrap; box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25); }
  .mom-tip b { font-variant-numeric: tabular-nums; margin-left: 4px; }
  .mom-tip-time { color: var(--muted); font-size: 11px; margin-bottom: 2px; }
  .mom-tip .sw { display: inline-block; width: 12px; height: 3px; margin-right: 6px; vertical-align: middle; }
  .mom-tip .sw.price { background: var(--text); }
  .mom-tip .sw.band { background: var(--accent); }
  .mom-tip .sw.vwap { background: var(--vwap); }
  .mom-band { fill: var(--accent-soft); stroke: none; }
  .mom-bound { fill: none; stroke: var(--accent); stroke-width: 1.3; stroke-dasharray: 5 4; }
  .mom-price { fill: none; stroke: var(--text); stroke-width: 1.6; }
  .mom-vwap { fill: none; stroke: var(--vwap); stroke-width: 1.3; }
  .mom-check { stroke: var(--grid); stroke-width: 1; stroke-dasharray: 2 4; }
  .mom-fill.buy { fill: var(--pos); }
  .mom-fill.sell { fill: var(--neg); }
  .mom-status { display: grid; gap: 6px; font-size: 13px; margin-bottom: 12px; }
  .mom-status .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; margin-right: 4px; }
  .mom-badge { display: inline-block; font-size: 11px; font-weight: 650; padding: 2px 8px; border-radius: 999px;
               border: 1px solid var(--border-strong); background: var(--panel-2); margin-right: 6px; color: var(--muted); }
  .mom-badge.pos { color: var(--pos); border-color: color-mix(in srgb, var(--pos) 45%, transparent); }
  .mom-badge.neg { color: var(--neg); border-color: color-mix(in srgb, var(--neg) 45%, transparent); }
  .mom-legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); margin: 8px 0 12px 0; }
  .mom-legend .sw { display: inline-block; width: 16px; height: 3px; margin-right: 6px; vertical-align: middle; }
  .mom-legend .sw.price { background: var(--text); }
  .mom-legend .sw.band { background: var(--accent); height: 8px; opacity: 0.5; }
  .mom-legend .sw.vwap { background: var(--vwap); }
  .mom-legend .sw.check { border-top: 2px dotted var(--faint); height: 0; }
"""
