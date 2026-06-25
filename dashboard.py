"""
dashboard.py
------------
Reads market_data.db and renders four tabs:
  1. Macro context (VIX gauge + history + averages, yields/Brent/USDNOK,
     US-Norway yield spread)
  2. Oslo Børs valuation (honest: shows history as it accrues)
  3. S&P 500 valuation (monthly history from multpl, with percentile readout)
  4. Price trends

Design principle: this tool DESCRIBES where things sit (e.g. "P/E is at the
88th percentile of its 5-yr history"). It does NOT tell you to buy or sell.
Free, monthly-lagged, single-metric data isn't a basis for trade decisions —
it's a basis for understanding. You bring the judgment.

Run:  streamlit run dashboard.py
"""

import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import create_engine, text

# If running on Streamlit Cloud, the FRED key lives in Streamlit secrets.
# Push it into the environment BEFORE importing config, which reads it from
# os.environ. Wrapped in try/except so local/GitHub runs (no st.secrets) are
# unaffected. The hosted dashboard only displays data, so it doesn't strictly
# need the key — but this keeps everything consistent.
try:
    if "FRED_API_KEY" in st.secrets:
        os.environ["FRED_API_KEY"] = st.secrets["FRED_API_KEY"]
except Exception:
    pass

import config

engine = create_engine(config.DB_PATH)


def _safe_read(q, params, parse_dates, numeric_cols=None):
    """Empty frame instead of a crash if the table doesn't exist yet.
    Coerces numeric_cols to real numbers — SQLite doesn't enforce types,
    so values can come back as strings (e.g. from the multpl backfill),
    which breaks .mean()/plotting. errors='coerce' turns junk into NaN."""
    try:
        with engine.connect() as c:
            df = pd.read_sql(q, c, params=params, parse_dates=parse_dates)
    except Exception:
        return pd.DataFrame()
    for col in (numeric_cols or []):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


st.set_page_config(page_title="Market Context", layout="wide", page_icon="📊")

st.markdown("""
<style>
  h1 { font-weight: 700; letter-spacing: -0.5px; }
  div[data-testid="stPlotlyChart"] {
      background: #1A2433; border: 1px solid #26334A;
      border-radius: 10px; padding: 8px 10px;
  }
  button[data-baseweb="tab"] { font-size: 1rem; padding: 10px 18px; }
  div[data-testid="stMetricValue"] { font-size: 1.4rem; }
  .block-container { padding-top: 2.2rem; }
  /* readout box */
  .readout { background:#16202E; border-left:3px solid #3DA5D9;
             border-radius:6px; padding:10px 14px; margin:6px 0 2px 0;
             font-size:0.92rem; color:#C7D0DC; }
</style>
""", unsafe_allow_html=True)

CHART_BG = "#1A2433"
ACCENT = "#3DA5D9"
GRID = "#26334A"
MUTED = "#8A97AB"


def style_fig(fig, height=320):
    fig.update_layout(
        template="plotly_dark", paper_bgcolor=CHART_BG, plot_bgcolor=CHART_BG,
        font=dict(color="#E6EAF0", size=12), height=height,
        margin=dict(t=44, b=24, l=10, r=10),
        xaxis=dict(gridcolor=GRID, zeroline=False),
        yaxis=dict(gridcolor=GRID, zeroline=False), showlegend=False,
    )
    return fig


def readout(text_html):
    st.markdown(f'<div class="readout">{text_html}</div>', unsafe_allow_html=True)


def percentile_of_last(series, value):
    """What fraction of historical values are below `value` (0-100)."""
    s = series.dropna()
    if len(s) < 2:
        return None
    return round((s < value).mean() * 100)


def yoy_from_index(df, date_col, value_col):
    """Convert a price/CPI *index* series into year-over-year % change.
    Matches each month to the value ~12 months earlier."""
    if df.empty or value_col not in df.columns:
        return pd.DataFrame()
    d = df.dropna(subset=[value_col]).sort_values(date_col).copy()
    if len(d) < 13:
        return pd.DataFrame()
    d["yoy"] = d[value_col].pct_change(periods=12) * 100
    return d.dropna(subset=["yoy"])


def recent_window(df, date_col="obs_date", days=180):
    """Trim a dated frame to the last `days` for display. Keeps full frame
    if trimming would leave too few points."""
    if df.empty:
        return df
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=days)
    trimmed = df[df[date_col] >= cutoff]
    return trimmed if len(trimmed) >= 2 else df


def rsi(series, period=14):
    """Classic Wilder RSI. Returns a series aligned to the input."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def describe_trend(df, value_col, date_col, unit="", higher_means=None,
                   lower_means=None):
    """Auto-generate a plain-language reading of where a series is heading.
    Compares the latest value to ~30 points ago, classifies the move, and
    optionally attaches what rising/falling tends to signal."""
    if df.empty or value_col not in df.columns:
        return "Not enough data yet to read a trend."
    d = df.dropna(subset=[value_col]).sort_values(date_col)
    if len(d) < 5:
        return "Not enough data yet to read a trend."
    cur = d[value_col].iloc[-1]
    window = min(30, len(d) - 1)
    past = d[value_col].iloc[-window - 1]
    chg = cur - past
    pct_chg = (chg / past * 100) if past else 0
    # classify magnitude relative to the series' own variability
    vol = d[value_col].tail(window).std() or 0.0001
    z = chg / vol
    # Also require the % move to be non-trivial before calling a trend,
    # so tiny noise on a quiet series reads as flat rather than "drifting".
    if abs(z) < 0.8 or abs(pct_chg) < 1.0:
        direction = "broadly flat / sideways"
    elif z >= 1.5:
        direction = "rising sharply"
    elif z > 0:
        direction = "drifting up"
    elif z <= -1.5:
        direction = "falling sharply"
    else:
        direction = "drifting down"

    msg = (f"Over the last {window} readings it's <b>{direction}</b> "
           f"({cur:.2f}{unit} now vs {past:.2f}{unit} then, "
           f"{pct_chg:+.1f}%).")
    if chg > 0 and higher_means:
        msg += f" Rising values typically mean: {higher_means}"
    elif chg < 0 and lower_means:
        msg += f" Falling values typically mean: {lower_means}"
    return msg


# Static plain-language explainers: what each indicator IS and how to read it.
EXPLAINERS = {
    "US_10Y": (
        "**What it is:** the interest rate the US government pays to borrow "
        "for 10 years — the world's benchmark 'safe' return.\n\n"
        "**Why it matters:** it sets the gravity for almost everything. When "
        "this rises, borrowing gets pricier everywhere and investors demand "
        "more from stocks, which often pressures share prices (especially "
        "growth/tech). When it falls, the opposite.\n\n"
        "**Reading the line:** a steadily *rising* line = the market expects "
        "stronger growth or stickier inflation. A *falling* line = expectations "
        "of slowing growth, cooling inflation, or rate cuts ahead. Flat = "
        "the market is waiting."),
    "NO_10Y": (
        "**What it is:** the same idea as the US 10Y, but for Norwegian "
        "government debt — the benchmark safe rate in NOK.\n\n"
        "**Why it matters:** it reflects what markets expect from Norway's "
        "central bank (Norges Bank) and inflation. It also drives Norwegian "
        "mortgage and corporate borrowing costs.\n\n"
        "**Reading the line:** rising = tighter money / inflation worry in "
        "Norway; falling = easing expected. Compare its slope to the US line "
        "— if they diverge, that's where the yield-spread chart below helps."),
    "USD_NOK": (
        "**What it is:** how many Norwegian kroner one US dollar buys.\n\n"
        "**Why it matters:** for a Norway-based business this is huge — a "
        "*higher* number means a weaker krone (USD costs more NOK), which "
        "makes dollar-priced imports and any USD costs more expensive, but "
        "helps Norwegian exporters.\n\n"
        "**Reading the line:** line going *up* = krone weakening vs the dollar. "
        "Line going *down* = krone strengthening. Oil price and the "
        "US–Norway rate gap are the big drivers."),
    "BRENT": (
        "**What it is:** the global benchmark price for a barrel of crude oil, "
        "in US dollars.\n\n"
        "**Why it matters:** Norway is an oil economy — Brent moves the krone, "
        "the Oslo index (heavy in energy names), and the country's fiscal "
        "picture. It's also a global inflation input.\n\n"
        "**Reading the line:** rising oil often = stronger krone and a "
        "tailwind for Oslo-listed energy; falling oil = the reverse. Sharp "
        "spikes usually mean supply shocks or geopolitics."),
    "VIX": (
        "**What it is:** the 'fear gauge' — the market's expectation of how "
        "bumpy US stocks will be over the next 30 days.\n\n"
        "**Why it matters:** it's a thermometer for nerves. Calm markets sit "
        "low; panic spikes it high. It tells you the *mood* behind the prices.\n\n"
        "**Reading the line:** below ~15 = complacent/calm, 15–25 = normal, "
        "above 25 = stress, above 35 = real fear. A sudden vertical spike "
        "almost always coincides with a sharp stock selloff."),
    "CURVE": (
        "**What it is:** the 10-year yield minus the 2-year yield — the "
        "'slope' of the US yield curve.\n\n"
        "**Why it matters:** normally long-term rates sit *above* short-term "
        "(positive = healthy). When this goes *negative* ('inverted'), it "
        "means markets expect rate cuts ahead, and historically it's been one "
        "of the most reliable recession warning signs — though often a year+ "
        "early.\n\n"
        "**Reading the line:** above zero = normal/expansion. Below zero = "
        "inverted, a caution flag. The *direction* matters too: a curve "
        "'steepening' back up from inversion has often preceded the actual "
        "downturn."),
    "HY_SPREAD": (
        "**What it is:** the extra yield investors demand to hold risky "
        "('junk') US corporate bonds instead of safe government bonds.\n\n"
        "**Why it matters:** it's the bond market's stress meter, and often "
        "more honest than stocks. When it's low and stable, credit markets "
        "are relaxed. When it jumps, lenders are getting nervous about "
        "defaults — which tends to lead trouble in equities.\n\n"
        "**Reading the line:** low and flat (say under ~3.5%) = calm credit "
        "conditions. Rising = risk appetite fading. A sharp spike is a "
        "genuine warning the financial system is tightening up."),
}


def macro_block(series_id, label, col, unit="",
                higher_means=None, lower_means=None, display_days=180):
    """Render one macro chart + its explainer expander + live trend readout.
    display_days limits what the CHART shows (recent window for readability),
    while the database keeps full history for calculations elsewhere."""
    d = load_macro(series_id)
    if d.empty:
        return
    # Trim to the recent window for display only.
    if display_days:
        cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=display_days)
        dd = d[d["obs_date"] >= cutoff]
        if len(dd) >= 2:        # only trim if enough points remain
            d = dd
    f = go.Figure(go.Scatter(x=d["obs_date"], y=d["value"], mode="lines",
                             line=dict(color=ACCENT, width=2)))
    add_calendar_markers(f, d["obs_date"])
    f.update_layout(title=label)
    col.plotly_chart(style_fig(f, height=200), use_container_width=True,
                     key=f"macro_{series_id}")
    with col.expander(f"How to read {label}"):
        st.markdown(EXPLAINERS.get(series_id, ""))
    col.markdown(
        f'<div class="readout">{describe_trend(d, "value", "obs_date", unit, higher_means, lower_means)}</div>',
        unsafe_allow_html=True)


def _calendar_df():
    """Calendar config -> tidy dataframe with parsed dates."""
    rows = getattr(config, "CALENDAR", [])
    if not rows:
        return pd.DataFrame(columns=["date", "type", "label"])
    df = pd.DataFrame(rows, columns=["date", "type", "label"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"]).sort_values("date")


def add_calendar_markers(fig, date_series):
    """Draw faint vertical lines on a time chart for events that fall within
    the chart's visible date range. Shows events; implies no causation."""
    cal = _calendar_df()
    if cal.empty or date_series.empty:
        return
    lo, hi = date_series.min(), date_series.max()
    colors = getattr(config, "CALENDAR_COLORS", {})
    visible = cal[(cal["date"] >= lo) & (cal["date"] <= hi)]
    for _, ev in visible.iterrows():
        fig.add_vline(x=ev["date"], line_width=1, line_dash="dot",
                      line_color=colors.get(ev["type"], MUTED), opacity=0.5)


def upcoming_events_panel(n=6):
    """Top-of-page panel listing the next n scheduled events."""
    cal = _calendar_df()
    if cal.empty:
        return
    today = pd.Timestamp.today().normalize()
    upcoming = cal[cal["date"] >= today].head(n)
    if upcoming.empty:
        st.caption("No upcoming calendar events configured (update config.py).")
        return
    colors = getattr(config, "CALENDAR_COLORS", {})
    chips = []
    for _, ev in upcoming.iterrows():
        days = (ev["date"] - today).days
        when = "today" if days == 0 else f"in {days}d"
        c = colors.get(ev["type"], MUTED)
        chips.append(
            f'<span style="display:inline-block;margin:3px 6px 3px 0;'
            f'padding:4px 10px;border-radius:6px;background:#16202E;'
            f'border-left:3px solid {c};font-size:0.85rem;">'
            f'<b>{ev["date"].strftime("%d %b")}</b> · {ev["type"]} — '
            f'{ev["label"]} <span style="color:{MUTED}">({when})</span></span>')
    st.markdown("".join(chips), unsafe_allow_html=True)


# --- loaders ---
@st.cache_data(ttl=300)
def load_prices(ticker_id):
    q = text("SELECT trade_date, adj_close, volume FROM daily_prices "
             "WHERE ticker_id=:t ORDER BY trade_date ASC")
    return _safe_read(q, {"t": ticker_id}, ["trade_date"],
                      numeric_cols=["adj_close", "volume"])


@st.cache_data(ttl=300)
def load_valuation(ticker_id):
    q = text("SELECT trade_date, pe_ratio, forward_pe, pb_ratio, source "
             "FROM valuation_history WHERE ticker_id=:t ORDER BY trade_date ASC")
    return _safe_read(q, {"t": ticker_id}, ["trade_date"],
                      numeric_cols=["pe_ratio", "forward_pe", "pb_ratio"])


@st.cache_data(ttl=300)
def load_macro(series):
    q = text("SELECT obs_date, value FROM macro_series "
             "WHERE series_id=:s ORDER BY obs_date ASC")
    return _safe_read(q, {"s": series}, ["obs_date"], numeric_cols=["value"])


@st.cache_data(ttl=300)
def load_dividends(ticker_id):
    q = text("SELECT ex_date, amount FROM dividend_events "
             "WHERE ticker_id=:t ORDER BY ex_date ASC")
    return _safe_read(q, {"t": ticker_id}, ["ex_date"], numeric_cols=["amount"])


@st.cache_data(ttl=300)
def load_earnings(ticker_id):
    q = text("SELECT earnings_date, surprise_pct, reported_eps FROM earnings_events "
             "WHERE ticker_id=:t ORDER BY earnings_date ASC")
    return _safe_read(q, {"t": ticker_id}, ["earnings_date"],
                      numeric_cols=["surprise_pct", "reported_eps"])


@st.cache_data(ttl=3600)
def upcoming_holding_events(ticker_id):
    """Next earnings + ex-dividend dates, live from yfinance, cached hourly.
    Values may be None if Yahoo has nothing. Earnings dates are provisional —
    Yahoo's small-cap coverage is patchy — so they're shown as estimates and
    never plotted on charts."""
    out = {"earnings": None, "ex_div": None}
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker_id).calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, (list, tuple)) and ed:
                out["earnings"] = str(ed[0])
            elif ed:
                out["earnings"] = str(ed)
            xd = cal.get("Ex-Dividend Date")
            if xd:
                out["ex_div"] = str(xd)
    except Exception:
        pass
    return out


@st.cache_data(ttl=300)
def last_updated():
    """Most recent date across all tables, to flag stale data."""
    dates = []
    for q in ["SELECT MAX(trade_date) FROM daily_prices",
              "SELECT MAX(obs_date) FROM macro_series",
              "SELECT MAX(trade_date) FROM valuation_history"]:
        try:
            with engine.connect() as c:
                r = c.execute(text(q)).scalar()
                if r:
                    dates.append(str(r)[:10])
        except Exception:
            pass
    return max(dates) if dates else None


def banded_chart(df, ycol, low, high, title, mode="lines+markers"):
    fig = go.Figure()
    if df.empty or ycol not in df.columns or df[ycol].dropna().empty:
        fig.add_annotation(text="No data yet — accrues as the daily job runs",
                           showarrow=False, font=dict(size=14, color=MUTED))
        fig.update_layout(title=title)
        return style_fig(fig)
    d = df.dropna(subset=[ycol])
    fig.add_trace(go.Scatter(x=d["trade_date"], y=d[ycol], mode=mode, name=ycol,
                             line=dict(color=ACCENT, width=2.5),
                             marker=dict(size=6, color=ACCENT)))
    fig.add_hrect(y0=low["low"], y1=low["high"], line_width=0,
                  fillcolor="#2ECC71", opacity=0.10, annotation_text="cheap band",
                  annotation_font_color=MUTED)
    fig.add_hrect(y0=high["low"], y1=high["high"], line_width=0,
                  fillcolor="#E74C3C", opacity=0.10, annotation_text="rich band",
                  annotation_font_color=MUTED)
    fig.update_layout(title=title)
    return style_fig(fig)


def valuation_readout(df, metric_col, label):
    """Factual 'where it sits' line — percentile + vs own average. No advice."""
    if df.empty or metric_col not in df.columns:
        return
    d = df.dropna(subset=[metric_col])
    if d.empty:
        return
    cur = d[metric_col].iloc[-1]
    pct = percentile_of_last(d[metric_col], cur)
    avg = d[metric_col].mean()
    n = len(d)
    pct_txt = (f"the <b>{pct}th percentile</b> of its {n}-point history"
               if pct is not None else "n/a (not enough history yet)")
    rel = "above" if cur > avg else "below"
    readout(f"<b>{label}</b>: currently <b>{cur:.1f}</b> — {pct_txt}, "
            f"and {rel} its historical average of {avg:.1f}. "
            f"<span style='color:{MUTED}'>Descriptive only, not advice.</span>")


# ===================== HEADER + FRESHNESS =====================
st.title("Market Context Dashboard")
lu = last_updated()
if lu:
    today = pd.Timestamp.today().normalize()
    age = (today - pd.Timestamp(lu)).days
    if age <= 4:
        st.caption(f"For market understanding — not trading signals. "
                   f"🟢 Data current as of **{lu}**.")
    else:
        st.warning(f"⚠️ Data last updated **{lu}** ({age} days ago). "
                   "The daily job may have failed — check GitHub Actions, "
                   "or run fetch_data.py locally.")
else:
    st.caption("For market understanding — not trading signals. "
               "No data yet — run fetch_data.py.")

with st.expander("ℹ️ How to read this dashboard"):
    st.markdown(
        "- **Macro & VIX** — the market's current mood. VIX is the 'fear "
        "gauge': low = calm, high = stressed. Yields, USD/NOK and Brent give "
        "the broader backdrop.\n"
        "- **Oslo Børs / S&P 500** — how expensive each market is by P/E "
        "(price vs earnings), P/B (price vs book value), forward P/E "
        "(price vs *expected* earnings). Higher = pricier.\n"
        "- **Bands** — green = your 'cheap' zone, red = 'rich' zone. "
        "These are reference rails *you* set in config.py, not verdicts.\n"
        "- **Percentile readouts** — where today sits versus its own history. "
        "88th percentile means today is pricier than 88% of the past readings.\n"
        "- Everything here is *descriptive*. It sharpens your picture; it "
        "doesn't make the call. Data is free and monthly-lagged in places.")

st.markdown("##### 📅 Upcoming market events")
st.caption("Scheduled events that often move markets. Faint dotted lines on "
           "the charts below mark these dates so you can see how past moves "
           "lined up with them — the dashboard shows the timing, you judge the "
           "connection.")
upcoming_events_panel()

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Macro & VIX", "Oslo Børs", "S&P 500", "Price trends", "My Holdings"])

# ===================== TAB 1: MACRO =====================
with tab1:
    st.header("Volatility & macro context")
    st.caption("VIX measures expected US market volatility. Below ~15 is calm, "
               "15–25 normal, above 25 stressed. The line shows its path; the "
               "gauge shows the latest close.")
    vix = load_macro("VIX")
    if vix.empty:
        st.warning("No VIX data yet. Run fetch_data.py (and set your FRED key).")
    else:
        latest = vix["value"].iloc[-1]
        l30 = vix["value"].tail(30).mean()
        l60 = vix["value"].tail(60).mean()
        l90 = vix["value"].tail(90).mean()
        c1, c2 = st.columns([1, 1])
        with c1:
            g = go.Figure(go.Indicator(
                mode="gauge+number", value=latest,
                title={"text": "VIX (last close)"},
                number={"font": {"color": "#E6EAF0"}},
                gauge={"axis": {"range": [None, 50], "tickcolor": MUTED},
                       "bar": {"color": "#E6EAF0"}, "bgcolor": CHART_BG,
                       "steps": [{"range": [0, 15], "color": "#1E5631"},
                                 {"range": [15, 25], "color": "#8A7A1E"},
                                 {"range": [25, 50], "color": "#7A2E2E"}]}))
            st.plotly_chart(style_fig(g, height=260), use_container_width=True,
                            key="vix_gauge")
            m1, m2, m3 = st.columns(3)
            m1.metric("L30 avg", f"{l30:.1f}", f"{latest - l30:+.1f} vs now",
                      delta_color="inverse")
            m2.metric("L60 avg", f"{l60:.1f}", f"{latest - l60:+.1f} vs now",
                      delta_color="inverse")
            m3.metric("L90 avg", f"{l90:.1f}", f"{latest - l90:+.1f} vs now",
                      delta_color="inverse")
            st.caption("Deltas = today minus each average. Green = calmer than "
                       "that period, red = more volatile.")
        with c2:
            # VIX history line — show recent window only (averages above use full data)
            vix_recent = vix[vix["obs_date"] >= (
                pd.Timestamp.today().normalize() - pd.Timedelta(days=180))]
            if len(vix_recent) < 2:
                vix_recent = vix
            vfig = go.Figure(go.Scatter(x=vix_recent["obs_date"], y=vix_recent["value"],
                                        mode="lines", line=dict(color=ACCENT, width=2)))
            vfig.add_hline(y=l90, line_dash="dot", line_color=MUTED,
                           annotation_text="90-day avg", annotation_font_color=MUTED)
            add_calendar_markers(vfig, vix_recent["obs_date"])
            vfig.update_layout(title="VIX — recent history")
            st.plotly_chart(style_fig(vfig, height=260), use_container_width=True,
                            key="vix_hist")
            with st.expander("How to read VIX"):
                st.markdown(EXPLAINERS["VIX"])
            pct = percentile_of_last(vix["value"].tail(90), latest)
            if pct is not None:
                mood = ("calm" if latest < 15 else
                        "normal" if latest < 25 else "stressed")
                readout(f"VIX <b>{latest:.1f}</b> reads as <b>{mood}</b>. "
                        f"That's the <b>{pct}th percentile</b> of the last 90 "
                        f"days — higher percentile = more fear than usual lately.")
            readout(describe_trend(vix, "value", "obs_date",
                    higher_means="nerves building — often alongside falling "
                    "stocks.", lower_means="nerves settling — markets calming."))

        st.divider()
        st.subheader("Rates, currency & oil")
        st.caption("Backdrop for both markets. The US–Norway yield gap (below) "
                   "is a rough gauge of relative rate pressure between the two.")
        cc1, cc2 = st.columns(2)
        macro_block("US_10Y", "US 10Y yield", cc1, unit="%",
                    higher_means="markets expect stronger growth or stickier "
                    "inflation; tends to pressure stock valuations.",
                    lower_means="markets expect slowing growth, cooling "
                    "inflation, or rate cuts; often supportive for stocks.")
        macro_block("NO_10Y", "Norway 10Y yield", cc2, unit="%",
                    higher_means="tighter money / inflation worry in Norway.",
                    lower_means="easing expected from Norges Bank.")
        macro_block("USD_NOK", "USD/NOK", cc1,
                    higher_means="the krone is weakening vs the dollar — "
                    "dollar costs and imports get pricier for a NOK business.",
                    lower_means="the krone is strengthening vs the dollar.")
        macro_block("BRENT", "Brent crude (USD)", cc2, unit=" USD",
                    higher_means="a tailwind for the krone and Oslo-listed "
                    "energy names.",
                    lower_means="a headwind for the krone and Norwegian energy.")
        macro_block("HY_SPREAD", "US high-yield credit spread", cc1, unit="%",
                    higher_means="credit markets getting nervous about "
                    "defaults — often an early warning ahead of equities.",
                    lower_means="relaxed credit conditions, healthy risk "
                    "appetite.")

        # US - Norway yield spread (relative value)
        us = load_macro("US_10Y").rename(columns={"value": "us"})
        no = load_macro("NO_10Y").rename(columns={"value": "no"})
        if not us.empty and not no.empty:
            sp = pd.merge(us, no, on="obs_date", how="inner")
            sp["spread"] = sp["us"] - sp["no"]
            cur_sp = sp["spread"].iloc[-1]  # current from full data
            sp_disp = recent_window(sp)
            if not sp_disp.empty:
                sfig = go.Figure(go.Scatter(x=sp_disp["obs_date"], y=sp_disp["spread"],
                                            mode="lines", line=dict(color="#E0A458", width=2)))
                sfig.add_hline(y=0, line_dash="dot", line_color=MUTED)
                sfig.update_layout(title="US 10Y minus Norway 10Y (yield spread, %)")
                st.plotly_chart(style_fig(sfig, height=240),
                                use_container_width=True, key="yield_spread")
                side = ("US yields are higher" if cur_sp > 0
                        else "Norway yields are higher")
                readout(f"Current spread <b>{cur_sp:+.2f}%</b> — {side}. "
                        "Widening favours the higher-yielding currency, all "
                        "else equal; useful context for USD/NOK moves.")

        st.divider()
        st.subheader("US yield curve (2s10s) — recession watch")
        st.caption("The 10Y minus 2Y Treasury yield. Below zero ('inverted') "
                   "has historically been an early recession signal.")
        t10 = load_macro("US_10Y").rename(columns={"value": "y10"})
        t2 = load_macro("US_2Y").rename(columns={"value": "y2"})
        if not t10.empty and not t2.empty:
            cv = pd.merge(t10, t2, on="obs_date", how="inner")
            cv["curve"] = cv["y10"] - cv["y2"]
            cv = cv.dropna(subset=["curve"])
            if not cv.empty:
                cur_cv = cv["curve"].iloc[-1]  # current from full data
                cv_disp = recent_window(cv, days=365)  # 1yr: inversions are slow
                cfig = go.Figure(go.Scatter(x=cv_disp["obs_date"], y=cv_disp["curve"],
                                            mode="lines", line=dict(color="#9B8FD9", width=2)))
                cfig.add_hline(y=0, line_dash="dot", line_color="#E74C3C",
                               annotation_text="inversion line",
                               annotation_font_color=MUTED)
                add_calendar_markers(cfig, cv_disp["obs_date"])
                cfig.update_layout(title="US 10Y minus 2Y (%)")
                st.plotly_chart(style_fig(cfig, height=240),
                                use_container_width=True, key="yield_curve")
                with st.expander("How to read the yield curve (2s10s)"):
                    st.markdown(EXPLAINERS["CURVE"])
                state = ("inverted — a historical caution flag" if cur_cv < 0
                         else "positive / normal")
                readout(f"Curve at <b>{cur_cv:+.2f}%</b> — {state}. "
                        + describe_trend(cv, "curve", "obs_date", unit="%",
                          higher_means="steepening (normalising).",
                          lower_means="flattening / inverting (caution)."))
        else:
            st.caption("2Y data not present yet — run fetch_data.py after the "
                       "config update to pull the new series.")

        st.divider()
        st.subheader("Inflation (year-over-year %)")
        st.caption("How fast consumer prices are rising vs a year ago. Central "
                   "banks target ~2%; above that pressures them to keep rates "
                   "high. US from FRED, Norway from Statistics Norway (SSB).")
        ic1, ic2 = st.columns(2)
        # US inflation (computed YoY from CPI index)
        us_cpi = load_macro("US_CPI_INDEX").rename(columns={"obs_date": "d", "value": "v"})
        us_yoy = yoy_from_index(us_cpi, "d", "v")
        if not us_yoy.empty:
            f = go.Figure(go.Scatter(x=us_yoy["d"], y=us_yoy["yoy"],
                                     mode="lines", line=dict(color=ACCENT, width=2)))
            f.add_hline(y=2, line_dash="dot", line_color="#5FB88A",
                        annotation_text="2% target", annotation_font_color=MUTED)
            f.update_layout(title="US CPI inflation (YoY %)")
            ic1.plotly_chart(style_fig(f, height=220), use_container_width=True,
                             key="us_inflation")
            cur = us_yoy["yoy"].iloc[-1]
            ic1.markdown(f'<div class="readout">US inflation <b>{cur:.1f}%</b> '
                         f'(latest {us_yoy["d"].iloc[-1].strftime("%b %Y")}). '
                         + ("above" if cur > 2 else "at/below") +
                         ' the 2% target.</div>', unsafe_allow_html=True)
        else:
            ic1.caption("US CPI needs 13+ months of data to show a "
                        "year-over-year rate. Rebuild the database after the "
                        "fetch update so FRED pulls enough history.")
        # Norway inflation (computed YoY from SSB index)
        no_cpi = load_macro("NO_CPI_INDEX").rename(columns={"obs_date": "d", "value": "v"})
        no_yoy = yoy_from_index(no_cpi, "d", "v")
        if not no_yoy.empty:
            f = go.Figure(go.Scatter(x=no_yoy["d"], y=no_yoy["yoy"],
                                     mode="lines", line=dict(color="#E0A458", width=2)))
            f.add_hline(y=2, line_dash="dot", line_color="#5FB88A",
                        annotation_text="2% target", annotation_font_color=MUTED)
            f.update_layout(title="Norway CPI inflation (YoY %)")
            ic2.plotly_chart(style_fig(f, height=220), use_container_width=True,
                             key="no_inflation")
            cur = no_yoy["yoy"].iloc[-1]
            ic2.markdown(f'<div class="readout">Norway inflation <b>{cur:.1f}%</b> '
                         f'(latest {no_yoy["d"].iloc[-1].strftime("%b %Y")}). '
                         + ("above" if cur > 2 else "at/below") +
                         ' Norges Bank\'s 2% target.</div>', unsafe_allow_html=True)
        else:
            ic2.caption("Norway CPI not present yet — runs after fetch_data.py "
                        "pulls from SSB. If it stays empty, SSB's table 14700 "
                        "may have changed.")

# ===================== TAB 2: OSLO =====================
with tab2:
    st.header("Oslo Børs valuation")
    st.caption("How expensive the Oslo market is. P/B = price vs book value, "
               "P/E TTM = price vs trailing earnings, forward P/E = vs expected "
               "earnings. Green band = your cheap zone, red = rich zone.")
    st.info("Free historical Oslo index ratios don't exist, so this series "
            "**accrues from the day you start running the job**. It looks "
            "sparse until weeks build up — that's honest, not broken.")
    v = load_valuation("OBXD.OL")
    b = config.BANDS["OSLO"]
    c1, c2 = st.columns(2)
    with c1:
        st.plotly_chart(banded_chart(v, "pb_ratio",
                        {"low": b["pb"]["low"], "high": b["pb"]["low"]},
                        {"low": b["pb"]["high"], "high": b["pb"]["high"]},
                        "P/B"), use_container_width=True, key="oslo_pb")
        st.plotly_chart(banded_chart(v, "pe_ratio",
                        {"low": b["pe_ttm"]["low"], "high": b["pe_ttm"]["low"]},
                        {"low": b["pe_ttm"]["high"], "high": b["pe_ttm"]["high"]},
                        "P/E TTM"), use_container_width=True, key="oslo_pe")
    with c2:
        st.plotly_chart(banded_chart(v, "forward_pe",
                        {"low": b["fwd_pe"]["low"], "high": b["fwd_pe"]["low"]},
                        {"low": b["fwd_pe"]["high"], "high": b["fwd_pe"]["high"]},
                        "Forward P/E"), use_container_width=True, key="oslo_fpe")
    valuation_readout(v, "pe_ratio", "Oslo P/E TTM")
    valuation_readout(v, "pb_ratio", "Oslo P/B")

# ===================== TAB 3: S&P 500 =====================
with tab3:
    st.header("S&P 500 valuation")
    st.caption("Monthly index figures from multpl. P/B = price vs book value, "
               "P/E TTM = price vs trailing earnings. Bands are your own "
               "reference rails (edit in config.py) — not buy/sell advice.")
    v = load_valuation("SPY")
    b = config.BANDS["SP500"]
    c1, c2 = st.columns(2)
    with c1:
        st.plotly_chart(banded_chart(v, "pb_ratio",
                        {"low": b["pb"]["low"], "high": b["pb"]["low"]},
                        {"low": b["pb"]["high"], "high": b["pb"]["high"]},
                        "P/B (monthly)"), use_container_width=True, key="sp_pb")
    with c2:
        st.plotly_chart(banded_chart(v, "pe_ratio",
                        {"low": b["pe_ttm"]["low"], "high": b["pe_ttm"]["low"]},
                        {"low": b["pe_ttm"]["high"], "high": b["pe_ttm"]["high"]},
                        "P/E TTM (monthly)"), use_container_width=True, key="sp_pe")
    valuation_readout(v, "pe_ratio", "S&P 500 P/E TTM")
    valuation_readout(v, "pb_ratio", "S&P 500 P/B")

    st.divider()
    st.subheader("Stocks vs bonds (the 'Fed model')")
    st.caption("Earnings yield (100 ÷ P/E) is what the S&P 'pays' in earnings "
               "per dollar invested. Comparing it to the 10Y Treasury yield "
               "shows whether stocks are cheap or dear *relative to bonds* — "
               "often more useful than P/E alone.")
    ev = v.dropna(subset=["pe_ratio"]).copy() if (not v.empty and "pe_ratio" in v.columns) else pd.DataFrame()
    bond = load_macro("US_10Y").rename(columns={"obs_date": "trade_date", "value": "y10"})
    if not ev.empty and not bond.empty:
        ev["earnings_yield"] = 100.0 / ev["pe_ratio"]
        # align bond yield to each (monthly) valuation date via nearest-prior
        ev = ev.sort_values("trade_date")
        bond = bond.dropna(subset=["y10"]).sort_values("trade_date")
        merged = pd.merge_asof(ev, bond, on="trade_date", direction="nearest")
        if not merged.empty and merged["y10"].notna().any():
            ef = go.Figure()
            ef.add_trace(go.Scatter(x=merged["trade_date"], y=merged["earnings_yield"],
                                    mode="lines", name="S&P earnings yield",
                                    line=dict(color=ACCENT, width=2)))
            ef.add_trace(go.Scatter(x=merged["trade_date"], y=merged["y10"],
                                    mode="lines", name="US 10Y bond yield",
                                    line=dict(color="#E0A458", width=2)))
            ef.update_layout(title="S&P earnings yield vs US 10Y (%)",
                             showlegend=True,
                             legend=dict(orientation="h", y=1.12, font=dict(size=10)))
            st.plotly_chart(style_fig(ef, height=280), use_container_width=True,
                            key="fed_model")
            ey = merged["earnings_yield"].iloc[-1]
            by = merged["y10"].iloc[-1]
            gap = ey - by
            lean = ("stocks offer more yield than bonds — relatively supportive "
                    "for equities" if gap > 0 else
                    "bonds offer more yield than stocks — a richer/expensive "
                    "reading for equities vs bonds")
            readout(f"S&P earnings yield <b>{ey:.1f}%</b> vs 10Y <b>{by:.1f}%</b> "
                    f"— gap <b>{gap:+.1f}pp</b>. {lean}. "
                    f"<span style='color:{MUTED}'>Context, not advice; the Fed "
                    "model is a rough lens, not a law.</span>")

# ===================== TAB 4: PRICES =====================
def price_block(ticker_id, label, color, col, show_dividends=False):
    """Price line + 200-day MA overlay + 52-week hi/lo readout + RSI panel.
    show_dividends overlays ex-dividend date markers (holdings only)."""
    raw = load_prices(ticker_id)
    if raw.empty or "adj_close" not in raw.columns:
        return
    d = raw.dropna(subset=["adj_close"]).sort_values("trade_date")
    if d.empty:
        return
    d = d.reset_index(drop=True)
    d["ma50"] = d["adj_close"].rolling(50, min_periods=10).mean()
    d["ma200"] = d["adj_close"].rolling(200, min_periods=30).mean()
    d["rsi"] = rsi(d["adj_close"])
    # Bollinger Bands: 20-day SMA ± 2 standard deviations
    bb_mid = d["adj_close"].rolling(20, min_periods=10).mean()
    bb_std = d["adj_close"].rolling(20, min_periods=10).std()
    d["bb_mid"] = bb_mid
    d["bb_up"] = bb_mid + 2 * bb_std
    d["bb_lo"] = bb_mid - 2 * bb_std

    # Price + MAs + Bollinger overlay
    pf = go.Figure()
    # Bollinger envelope first (so it sits behind the lines)
    if d["bb_up"].notna().any():
        pf.add_trace(go.Scatter(x=d["trade_date"], y=d["bb_up"], mode="lines",
                                name="Bollinger ±2σ", line=dict(width=0),
                                showlegend=False, hoverinfo="skip"))
        pf.add_trace(go.Scatter(x=d["trade_date"], y=d["bb_lo"], mode="lines",
                                name="Bollinger ±2σ", line=dict(width=0),
                                fill="tonexty", fillcolor="rgba(61,165,217,0.10)",
                                hoverinfo="skip"))
    pf.add_trace(go.Scatter(x=d["trade_date"], y=d["adj_close"], mode="lines",
                            name=label, line=dict(color=color, width=2)))
    if d["ma50"].notna().any():
        pf.add_trace(go.Scatter(x=d["trade_date"], y=d["ma50"], mode="lines",
                                name="50-day avg",
                                line=dict(color="#5FB88A", width=1.3)))
    if d["ma200"].notna().any():
        pf.add_trace(go.Scatter(x=d["trade_date"], y=d["ma200"], mode="lines",
                                name="200-day avg",
                                line=dict(color=MUTED, width=1.5, dash="dash")))
    pf.update_layout(title=f"{label} — price, 50/200-day & Bollinger Bands",
                     showlegend=True,
                     legend=dict(orientation="h", y=1.12, font=dict(size=10)))
    # Event markers (holdings only) — within the visible price range.
    if show_dividends:
        lo, hi = d["trade_date"].min(), d["trade_date"].max()
        divs = load_dividends(ticker_id)
        if not divs.empty:
            vis = divs[(divs["ex_date"] >= lo) & (divs["ex_date"] <= hi)]
            for _, ev in vis.iterrows():
                pf.add_vline(x=ev["ex_date"], line_width=1, line_dash="dot",
                             line_color="#E0A458", opacity=0.5)
        earn = load_earnings(ticker_id)
        if not earn.empty:
            ev_vis = earn[(earn["earnings_date"] >= lo) & (earn["earnings_date"] <= hi)]
            for _, ev in ev_vis.iterrows():
                s = ev.get("surprise_pct")
                # green = beat (positive surprise), red = miss, grey = unknown/future
                if pd.isna(s):
                    c = MUTED
                elif s >= 0:
                    c = "#2ECC71"
                else:
                    c = "#E74C3C"
                pf.add_vline(x=ev["earnings_date"], line_width=1.2, line_dash="solid",
                             line_color=c, opacity=0.55)
    col.plotly_chart(style_fig(pf, height=300), use_container_width=True,
                     key=f"price_{ticker_id}")

    # 52-week range + trend-vs-MA readout
    last = d["adj_close"].iloc[-1]
    window = d.tail(252)
    hi, lo = window["adj_close"].max(), window["adj_close"].min()
    rng_pct = ((last - lo) / (hi - lo) * 100) if hi > lo else 50
    ma = d["ma200"].iloc[-1]
    if pd.notna(ma):
        trend = ("above" if last > ma else "below")
        sig = ("a commonly-watched bullish sign" if last > ma
               else "a commonly-watched bearish/caution sign")
        col.markdown(
            f'<div class="readout">Sits at <b>{rng_pct:.0f}%</b> of its '
            f'52-week range (low {lo:,.0f} → high {hi:,.0f}). Price is '
            f'<b>{trend}</b> its 200-day average — {sig}.</div>',
            unsafe_allow_html=True)

    # 50/200 cross readout
    ma50, ma200v = d["ma50"].iloc[-1], d["ma200"].iloc[-1]
    if pd.notna(ma50) and pd.notna(ma200v):
        if ma50 > ma200v:
            cross = ("The 50-day sits <b>above</b> the 200-day (a 'golden "
                     "cross' configuration) — medium-term trend stronger than "
                     "long-term, generally read as bullish.")
        else:
            cross = ("The 50-day sits <b>below</b> the 200-day (a 'death "
                     "cross' configuration) — medium-term weaker than "
                     "long-term, generally read as bearish.")
        col.markdown(f'<div class="readout">{cross}</div>', unsafe_allow_html=True)

    # Bollinger position readout
    bb_up, bb_lo, bb_mid = (d["bb_up"].iloc[-1], d["bb_lo"].iloc[-1],
                            d["bb_mid"].iloc[-1])
    if pd.notna(bb_up) and pd.notna(bb_lo) and bb_up > bb_lo:
        pos = (last - bb_lo) / (bb_up - bb_lo) * 100
        if last >= bb_up:
            bdesc = "riding/above the upper band — stretched high vs recent volatility"
        elif last <= bb_lo:
            bdesc = "riding/below the lower band — stretched low vs recent volatility"
        else:
            bdesc = f"at {pos:.0f}% of the band width (50% = the 20-day average)"
        # band width as % of price = volatility gauge (squeeze detector)
        width_pct = (bb_up - bb_lo) / bb_mid * 100 if bb_mid else 0
        col.markdown(
            f'<div class="readout">Bollinger: price is {bdesc}. Band width is '
            f'<b>{width_pct:.1f}%</b> of price — narrow bands ("squeeze") mean '
            'calm that often precedes a bigger move; wide bands mean high '
            'volatility now.</div>', unsafe_allow_html=True)

    # RSI panel
    if d["rsi"].notna().any():
        rf = go.Figure(go.Scatter(x=d["trade_date"], y=d["rsi"], mode="lines",
                                  line=dict(color=color, width=1.5)))
        rf.add_hrect(y0=70, y1=100, line_width=0, fillcolor="#E74C3C",
                     opacity=0.10, annotation_text="overbought",
                     annotation_font_color=MUTED)
        rf.add_hrect(y0=0, y1=30, line_width=0, fillcolor="#2ECC71",
                     opacity=0.10, annotation_text="oversold",
                     annotation_font_color=MUTED)
        rf.update_layout(title=f"{label} — 14-day RSI", yaxis=dict(range=[0, 100]))
        col.plotly_chart(style_fig(rf, height=180), use_container_width=True,
                         key=f"rsi_{ticker_id}")
        cur_rsi = d["rsi"].dropna().iloc[-1]
        state = ("overbought (>70) — stretched to the upside" if cur_rsi > 70
                 else "oversold (<30) — stretched to the downside" if cur_rsi < 30
                 else "in the neutral zone")
        col.markdown(f'<div class="readout">RSI <b>{cur_rsi:.0f}</b> — {state}. '
                     'RSI measures recent momentum; extremes often (not always) '
                     'precede a pause or reversal.</div>', unsafe_allow_html=True)


with tab4:
    st.header("Index price trends")
    st.caption("Price with its 200-day average (the most-watched trend line: "
               "above = uptrend, below = downtrend), plus RSI momentum below. "
               "These are technical context, not signals to act on.")
    with st.expander("How to read these"):
        st.markdown(
            "- **50 & 200-day averages:** the two trend lines. Price above "
            "both = uptrend. When the **50 crosses above the 200** it's a "
            "'golden cross' (bullish); 50 below 200 is a 'death cross' "
            "(bearish). The gap between them shows if the trend is "
            "strengthening or fading.\n"
            "- **Bollinger Bands (shaded):** a 20-day average with an envelope "
            "2 standard deviations wide. Price near the *upper* edge = stretched "
            "high; near the *lower* = stretched low. When the band **pinches "
            "narrow** ('squeeze'), volatility has gone quiet — often before a "
            "bigger move.\n"
            "- **52-week range %:** where today sits between the year's low (0%) "
            "and high (100%).\n"
            "- **RSI:** a 0–100 momentum meter. Above 70 = 'overbought'; below "
            "30 = 'oversold'. Extremes are cautions, not guarantees — strong "
            "trends can stay overbought for a while.\n"
            "- All of this is technical *context*, not a signal to act.")
    c1, c2 = st.columns(2)
    price_block("SPX_INDEX", "S&P 500", ACCENT, c1)
    price_block("OSEBX_INDEX", "OSEBX", "#E0A458", c2)

# ===================== TAB 5: MY HOLDINGS =====================
with tab5:
    st.header("My holdings")
    st.caption("Factual key numbers and the same technical/valuation context "
               "as the index tabs, for your current positions. This is "
               "descriptive only — it does not give buy, sell or hold advice "
               "on any position. Not financial advice.")
    holdings = getattr(config, "HOLDINGS", {})
    if not holdings:
        st.info("No holdings configured. Add tickers to HOLDINGS in config.py.")
    else:
        with st.expander("How to read the event markers"):
            st.markdown(
                "Each holding's price chart has vertical lines marking company "
                "events, so you can see how the price moved around them.\n\n"
                "**Earnings (solid lines):**\n"
                "- 🟢 **Green = beat** — reported earnings came in *above* "
                "analysts' estimate.\n"
                "- 🔴 **Red = miss** — reported earnings came in *below* estimate.\n"
                "- ⚪ **Grey = no data or upcoming** — an earnings date with no "
                "recorded surprise figure yet.\n\n"
                "*A green beat next to a falling price is common and instructive* "
                "— a company can beat on earnings but still drop if its guidance "
                "or outlook disappointed. The colour is the factual result; the "
                "price move is the market's reaction, and they don't always agree.\n\n"
                "**Ex-dividend (amber dotted lines):**\n"
                "On the ex-dividend date the share price *mechanically* drops by "
                "roughly the dividend amount — that's arithmetic, not a market "
                "reaction. If you owned the stock before this date you're entitled "
                "to the dividend; the price simply adjusts down to reflect the cash "
                "leaving the company. This is one of the few price moves with a "
                "clear, known cause.\n\n"
                "**Important:** a marker shows *when* an event happened, not proof "
                "it *caused* a nearby move. And coverage varies — large, long-"
                "listed names (e.g. Kongsberg Gruppen) have years of history; "
                "small or recently-listed names have little. Each holding states "
                "its actual coverage.")
        tickers = list(holdings.items())
        # Two holdings per row
        for i in range(0, len(tickers), 2):
            cols = st.columns(2)
            for j, col in enumerate(cols):
                if i + j >= len(tickers):
                    break
                tkr, name = tickers[i + j]
                col.subheader(name)
                col.caption(tkr)
                # price chart + MA/RSI/Bollinger + ex-dividend markers
                price_block(tkr, name, ACCENT, col, show_dividends=True)
                # valuation readouts (P/E, P/B) accruing from the snapshot
                v = load_valuation(tkr)
                with col.container():
                    valuation_readout(v, "pe_ratio", f"{name} P/E TTM")
                    valuation_readout(v, "pb_ratio", f"{name} P/B")
                    # Upcoming events (provisional, live from yfinance)
                    ev = upcoming_holding_events(tkr)
                    parts = []
                    if ev.get("earnings"):
                        parts.append(f"next earnings ~<b>{ev['earnings'][:10]}</b>")
                    if ev.get("ex_div"):
                        parts.append(f"next ex-dividend ~<b>{ev['ex_div'][:10]}</b>")
                    if parts:
                        col.markdown(
                            f'<div class="readout">Upcoming: {" · ".join(parts)} '
                            f'<span style="color:{MUTED}">(estimated, from Yahoo '
                            "— may shift)</span></div>", unsafe_allow_html=True)
                    # Event-marker legend + coverage (honest about completeness)
                    divs = load_dividends(tkr)
                    earn = load_earnings(tkr)
                    n_div = len(divs)
                    n_earn = len(earn)
                    # Distinguish rich coverage (e.g. KOG) from thin (small/new names)
                    if n_earn <= 2 and n_div <= 2:
                        coverage = (f"<b>Limited event history</b> on record "
                                    f"({n_earn} earnings, {n_div} dividend) — this "
                                    "is a small or recently-listed name, not a "
                                    "complete record. More will accrue as it reports.")
                    else:
                        coverage = (f"Showing <b>{n_earn} earnings</b> and "
                                    f"<b>{n_div} dividend</b> events on record.")
                    col.markdown(
                        f'<div class="readout">{coverage}<br>'
                        '<b>Solid lines = earnings</b> (green = beat estimate, '
                        'red = miss, grey = no data/upcoming). '
                        '<b>Amber dotted = ex-dividend</b> (price mechanically '
                        'drops ~the dividend that day — a known cause, unlike most '
                        'moves). A marker shows <i>timing</i>, not proven cause.'
                        '</div>', unsafe_allow_html=True)
        st.divider()
        st.caption("Notes: newly listed or spun-off names have short price "
                   "history, so their 200-day average, 52-week range, and event "
                   "markers stay sparse until more data accrues. Earnings "
                   "markers come from Yahoo and run deep for large caps but thin "
                   "for small ones — the per-holding line above states the actual "
                   "coverage. Upcoming dates are provisional and may shift.")
