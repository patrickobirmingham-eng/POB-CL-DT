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
    out = {
        "date": day, "is_today": day == today, "bars": prepared, "last": last_label,
        "open": day_open, "prev_close": closes[-1], "upper": upper, "lower": lower,
        "vol": ms.daily_vol(closes),
    }
    if day != today and len(prepared) >= 300:
        # Showing the last session: also send what the NEXT session's bands need
        # (all but its opening price), so the page's Refresh button can draw
        # today's chart live once trading starts.
        out["next_sigma"] = ms.sigma_profile(prior + [prepared])
        out["next_prev_close"] = float(prepared["close"].iloc[-1])
    return out


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
    # Hover readout: a vertical guide + one dot per line, filled in by MOM_JS.
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
        # Bot fills: [minute index, "buy"/"sell", price, shares, "10:00 am"]
        "f": [[idx[lbl], side, round(fp, 2), fq, ft.strftime("%I:%M %p").lstrip("0").lower()]
              for lbl, side, fp, fq, ft in sorted(fills, key=lambda f: f[4])],
        # For the Refresh button's live redraw (MOM_JS): session date, symbol,
        # decision times and — when showing the last session — next day's inputs.
        "date": s["date"].isoformat(), "sym": sym,
        "c": [[idx[ms.CHECK_BARS[t]], t] for t in ms.CHECK_TIMES],
    }
    if "next_sigma" in s:
        hover["nx"] = {"sig": [round(float(s["next_sigma"][t]), 6) for t in grid],
                       "pc": round(s["next_prev_close"], 4)}
    hover_json = html.escape(json.dumps(hover, separators=(",", ":")), quote=True)

    when = "Today" if s["is_today"] else f"Last session ({s['date'].strftime('%a %b %d')})"
    status = "" if enabled else ' <span class="mom-badge neg">strategy OFF in Settings</span>'
    return f"""
    <div class="panel" id="momentumPanel">
      <div class="panel-head">
        <h2>{title}</h2>
        <span class="muted ai-summary" id="momAsOf">{when} &middot; as of {int(last[:2]) % 12 or 12}:{last[3:]} {"am" if int(last[:2]) < 12 else "pm"} ET{status}</span>
      </div>
      <div class="mom-status">
        <div id="momQuote"><span class="label">{sym}</span> <strong>{_money(px)}</strong> &middot; band {_money(lb)} – {_money(ub)} &middot; VWAP {_money(vw)}</div>
        <div id="momZone">{zone_html}</div>
        <div><span class="label">Bot</span> <span id="momPos">{pos_html}</span></div>
        <div class="muted">Size today: {shares_today:,} shares (~{_money(shares_today * s['open'])}) &middot; {sym} daily volatility {s['vol']:.2%}</div>
      </div>
      <div class="table-scroll"><div class="chart-wrap mom-wrap" data-hover="{hover_json}">{"".join(svg)}<div class="mom-tip" style="display:none"></div></div></div>
      <div class="mom-legend">
        <span><i class="sw price"></i>{sym} price</span><span><i class="sw band"></i>Concretum Bands (noise area)</span>
        <span><i class="sw vwap"></i>VWAP</span><span><i class="sw check"></i>30-min checks</span>
        <span class="pos">▲ buy</span><span class="neg">▼ sell</span>
      </div>
    </div>
    <script>{MOM_JS}</script>"""


# Chart script: draws the Concretum chart from the embedded per-minute data,
# shows the hover readout, and (window.momLiveRefresh, called by the dashboard's
# Refresh button) redraws it from today's live minute bars fetched through
# orb-bars.php on the same server as the refresh endpoint.
MOM_JS = r"""
(function () {
  var panel = document.getElementById('momentumPanel');
  if (!panel) return;
  var wrap = panel.querySelector('.mom-wrap');
  var svg = wrap.querySelector('svg');
  var tip = wrap.querySelector('.mom-tip');
  var d = JSON.parse(wrap.getAttribute('data-hover'));
  var W = d.geo[0], H = d.geo[1], L = d.geo[2], R = d.geo[3], T = d.geo[4], B = d.geo[5];
  var lo = d.geo[6], hi = d.geo[7];
  var n = d.t.length;

  function X(i) { return L + i * (W - L - R) / (n - 1); }
  function Y(v) { return T + (hi - v) / (hi - lo) * (H - T - B); }
  function f1(v) { return v.toFixed(1); }
  function num(v) { return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function money(v) { return v == null ? '—' : (v < 0 ? '-$' : '$') + num(Math.abs(v)); }
  function hm(t) { var h = +t.slice(0, 2); return (h % 12 || 12) + ':' + t.slice(3) + (h < 12 ? ' am' : ' pm'); }
  function lastIdx() { for (var i = n - 1; i >= 0; i--) if (d.p[i] != null) return i; return -1; }
  function path(arr, upto) {
    var s = '', k = 0;
    for (var i = 0; i <= upto; i++) {
      if (arr[i] == null) continue;
      s += (k++ ? 'L' : 'M') + f1(X(i)) + ',' + f1(Y(arr[i])) + ' ';
    }
    return s;
  }

  function render() {
    var li = lastIdx(), vals = [];
    for (var i = 0; i < n; i++) {
      vals.push(d.u[i], d.l[i]);
      if (i <= li) { if (d.p[i] != null) vals.push(d.p[i]); if (d.v[i] != null) vals.push(d.v[i]); }
    }
    lo = Math.min.apply(null, vals); hi = Math.max.apply(null, vals);
    var pad = (hi - lo) * 0.06 || 0.5;
    lo -= pad; hi += pad;
    var s = '';
    for (var k = 0; k < 5; k++) {
      var gv = lo + (hi - lo) * k / 4, gy = f1(Y(gv));
      s += '<line class="pl-grid" x1="' + L + '" y1="' + gy + '" x2="' + (W - R) + '" y2="' + gy + '"/>' +
           '<text class="pl-axis" x="' + (L - 8) + '" y="' + f1(Y(gv) + 3) + '" text-anchor="end">' + num(gv) + '</text>';
    }
    (d.c || []).forEach(function (c) {
      var xx = f1(X(c[0])), h = +c[1].slice(0, 2);
      s += '<line class="mom-check" x1="' + xx + '" y1="' + T + '" x2="' + xx + '" y2="' + (H - B) + '"/>';
      if (c[1].slice(3) === '00') {
        s += '<text class="pl-axis" x="' + xx + '" y="' + (H - B + 16) + '" text-anchor="middle">' + (h % 12 || 12) + (h < 12 ? 'am' : 'pm') + '</text>';
      }
    });
    s += '<rect class="mom-hit" x="' + L + '" y="' + T + '" width="' + (W - L - R) + '" height="' + (H - T - B) + '"/>';
    var band = path(d.u, n - 1);
    for (var j = n - 1; j >= 0; j--) band += 'L' + f1(X(j)) + ',' + f1(Y(d.l[j])) + ' ';
    s += '<path class="mom-band" d="' + band + 'Z"/>';
    s += '<path class="mom-bound" d="' + path(d.u, n - 1) + '"/><path class="mom-bound" d="' + path(d.l, n - 1) + '"/>';
    if (li >= 0) {
      s += '<path class="mom-vwap" d="' + path(d.v, li) + '"/>';
      s += '<path class="mom-price" d="' + path(d.p, li) + '"/>';
    }
    (d.f || []).forEach(function (f) {
      var xx = X(f[0]), yy = Y(f[2]), buy = f[1] === 'buy';
      var tipY = buy ? yy - 9 : yy + 9, baseY = buy ? yy + 5 : yy - 5;
      var pts = f1(xx) + ',' + f1(tipY) + ' ' + f1(xx - 7) + ',' + f1(baseY) + ' ' + f1(xx + 7) + ',' + f1(baseY);
      s += '<polygon class="mom-fill ' + (buy ? 'buy' : 'sell') + '" points="' + pts + '">' +
           '<title>' + (buy ? 'BUY ' : 'SELL ') + Math.round(f[3]).toLocaleString('en-US') + ' @ ' + money(f[2]) + ' — ' + f[4] + '</title></polygon>';
    });
    s += '<g class="mom-hover" style="display:none"><line class="mom-guide" x1="0" y1="' + T + '" x2="0" y2="' + (H - B) + '"/>' +
         '<circle class="mom-dot price" r="4"/><circle class="mom-dot band up" r="3.5"/>' +
         '<circle class="mom-dot band lo" r="3.5"/><circle class="mom-dot vwap" r="3.5"/></g>';
    svg.innerHTML = s;
  }

  // ----- hover readout (nearest minute to the pointer; mouse or touch)
  function show(ev) {
    var g = svg.querySelector('.mom-hover');
    if (!g) return;
    var r = svg.getBoundingClientRect();
    var vx = (ev.clientX - r.left) / r.width * W;
    var i = Math.max(0, Math.min(n - 1, Math.round((vx - L) / (W - L - R) * (n - 1))));
    var x = X(i), guide = g.querySelector('.mom-guide');
    g.style.display = '';
    guide.setAttribute('x1', x); guide.setAttribute('x2', x);
    [['p', '.price'], ['u', '.up'], ['l', '.lo'], ['v', '.vwap']].forEach(function (kv) {
      var dot = g.querySelector('.mom-dot' + kv[1]), v = d[kv[0]][i];
      dot.style.display = v == null ? 'none' : '';
      if (v != null) { dot.setAttribute('cx', x); dot.setAttribute('cy', Y(v)); }
    });
    tip.innerHTML = '<div class="mom-tip-time">' + hm(d.t[i]) + ' ET</div>' +
      '<div><i class="sw price"></i>Price <b>' + money(d.p[i]) + '</b></div>' +
      '<div><i class="sw band"></i>Upper band <b>' + money(d.u[i]) + '</b></div>' +
      '<div><i class="sw band"></i>Lower band <b>' + money(d.l[i]) + '</b></div>' +
      '<div><i class="sw vwap"></i>VWAP <b>' + money(d.v[i]) + '</b></div>' +
      // Bot trades within a few minutes of the pointer (the markers are small targets).
      (d.f || []).filter(function (f) { return Math.abs(f[0] - i) <= 3; }).map(function (f) {
        var buy = f[1] === 'buy';
        return '<div class="mom-tip-fill ' + (buy ? 'pos' : 'neg') + '">' + (buy ? '▲ BUY ' : '▼ SELL ') +
          Math.round(f[3]).toLocaleString('en-US') + ' @ <b>' + money(f[2]) + '</b> <span class="muted">' + f[4] + '</span></div>';
      }).join('');
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
  function hide() {
    var g = svg.querySelector('.mom-hover');
    if (g) g.style.display = 'none';
    tip.style.display = 'none';
  }
  svg.addEventListener('pointermove', show);
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('pointerleave', hide);

  // ----- status lines + check table from the current data
  function updateText(positions, orders) {
    var li = lastIdx();
    if (li < 0) return;
    var px = d.p[li], ub = d.u[li], lb = d.l[li], vw = d.v[li];
    var today = d.date === new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    var asOf = document.getElementById('momAsOf');
    if (asOf) asOf.innerHTML = (today ? 'Today' : 'Last session') + ' &middot; as of ' + hm(d.t[li]) + ' ET (live)';
    var q = document.getElementById('momQuote');
    if (q) q.innerHTML = '<span class="label">' + d.sym + '</span> <strong>' + money(px) + '</strong> &middot; band ' +
      money(lb) + ' – ' + money(ub) + ' &middot; VWAP ' + money(vw);
    var z = document.getElementById('momZone');
    if (z) z.innerHTML = px > ub ? '<span class="mom-badge pos">Above the band</span> breakout up (long signal at the next check)'
      : px < lb ? '<span class="mom-badge neg">Below the band</span> breakout down (short signal at the next check)'
      : '<span class="mom-badge">Inside the band</span> normal noise — no signal';
    var pe = document.getElementById('momPos');
    if (pe && positions) {
      var held = positions.filter(function (p) { return p.symbol === d.sym; })[0];
      var qty = held ? Number(held.qty) : 0;
      if (qty) {
        var pl = Number(held.unrealized_pl), long = qty > 0;
        pe.innerHTML = '<span class="mom-badge ' + (long ? 'pos' : 'neg') + '">' + (long ? 'LONG ' : 'SHORT ') +
          Math.abs(qty).toLocaleString('en-US') + ' ' + d.sym + '</span> entry ' + money(Number(held.avg_entry_price)) +
          ', P&amp;L <span class="' + (pl >= 0 ? 'pos' : 'neg') + '">' + money(pl) + '</span>, exits if price ' +
          (long ? '&lt; ' + money(Math.max(ub, vw)) : '&gt; ' + money(Math.min(lb, vw)));
      } else {
        var open = ['new', 'pending_new', 'accepted', 'partially_filled'];
        var pend = (orders || []).filter(function (o) { return o.symbol === d.sym && open.indexOf(o.status) >= 0; })[0];
        pe.innerHTML = pend
          ? '<span class="mom-badge ' + (pend.side === 'buy' ? 'pos' : 'neg') + '">ORDER SENT</span> ' + String(pend.side).toUpperCase() +
            ' ' + Number(pend.qty || 0).toLocaleString('en-US') + ' ' + d.sym + ' — waiting for the fill'
          : '<span class="mom-badge">FLAT</span> no position';
      }
    }
  }

  // ----- live redraw for the Refresh button
  // endpoint: the refresh URL (its folder also holds orb-bars.php); data: the
  // refresh response (positions / orders). Returns a short status string.
  window.momLiveRefresh = async function (endpoint, token, data) {
    if (!endpoint) return 'no endpoint';
    var url = endpoint.replace(/[^\/]*$/, 'orb-bars.php') + '?token=' + encodeURIComponent(token) +
      '&symbol=' + encodeURIComponent(d.sym) + '&_=' + Date.now();
    var resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) throw new Error('orb-bars.php HTTP ' + resp.status);
    var bars = (await resp.json()).bars || [];
    var todayStr = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    var byMin = {};
    bars.forEach(function (b) {
      var dt = new Date(b.t);
      if (dt.toLocaleDateString('en-CA', { timeZone: 'America/New_York' }) !== todayStr) return;
      var t = dt.toLocaleTimeString('en-GB', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
      byMin[t] = b;
    });
    var have = d.t.filter(function (t) { return byMin[t]; });
    if (!have.length) return 'no bars yet today';
    if (d.date !== todayStr) {
      // Page was built for the last session: build today's bands from the
      // embedded next-day inputs and today's opening price.
      if (!d.nx) return 'bands for today not available yet';
      var first = byMin[have[0]], o = Number(first.o), pc = d.nx.pc;
      d.u = d.nx.sig.map(function (sg) { return Math.round(Math.max(o, pc) * (1 + sg) * 100) / 100; });
      d.l = d.nx.sig.map(function (sg) { return Math.round(Math.min(o, pc) * (1 - sg) * 100) / 100; });
      d.date = todayStr;
      delete d.nx;
    }
    // Same as momentum_strategy.prepare_day: forward-fill missing minutes,
    // running VWAP of typical price * volume.
    var last = d.t.indexOf(have[have.length - 1]);
    var close = Number(byMin[have[0]].c), cumPV = 0, cumV = 0, vwap = null;
    for (var i = 0; i < n; i++) {
      if (i > last) { d.p[i] = null; d.v[i] = null; continue; }
      var b = byMin[d.t[i]], h, l, v = 0;
      if (b) { close = Number(b.c); h = Number(b.h); l = Number(b.l); v = Number(b.v); } else { h = l = close; }
      cumPV += (h + l + close) / 3 * v; cumV += v;
      if (cumV > 0) vwap = cumPV / cumV;
      d.p[i] = Math.round(close * 100) / 100;
      d.v[i] = Math.round((vwap == null ? close : vwap) * 100) / 100;
    }
    // Today's bot fills from the refresh data.
    if (data && data.orders) {
      d.f = data.orders.filter(function (o) {
        return o.symbol === d.sym && o.filled_at && o.filled_avg_price &&
          new Date(o.filled_at).toLocaleDateString('en-CA', { timeZone: 'America/New_York' }) === todayStr;
      }).map(function (o) {
        var dt = new Date(o.filled_at);
        var t = dt.toLocaleTimeString('en-GB', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
        var i = Math.max(0, d.t.indexOf(t < '09:30' ? '09:30' : (t > '15:59' ? '15:59' : t)));
        return [i, o.side, Math.round(Number(o.filled_avg_price) * 100) / 100, Number(o.filled_qty || o.qty || 0),
                dt.toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit' }).toLowerCase()];
      }).sort(function (a, b) { return a[0] - b[0]; });
    }
    render();
    updateText(data && data.positions, data && data.orders);
    return 'ok';
  };

  render();
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
  .mom-tip-fill { border-top: 1px solid var(--border); margin-top: 4px; padding-top: 4px; font-weight: 600; }
  .mom-tip-fill b { color: var(--text); }
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
