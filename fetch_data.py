"""
fetch_data.py
-------------
Daily ingestion. Pulls three things into market_data.db:

  1. Index/ETF PRICES from yfinance (real daily history, free).
  2. MACRO context from FRED (VIX, yields, USD/NOK, Brent).
  3. A daily snapshot of current valuation ratios (P/E, P/B, fwd P/E)
     for the SPY and OBXD.OL proxies, stamped with today's date so a
     real history accrues over time.

Plus a one-time S&P 500 monthly history backfill from multpl, so the
US valuation charts are populated from day one instead of waiting a year.

Run:  python fetch_data.py
Designed to be safe to run repeatedly (upserts, no duplicate rows).
"""

import datetime
import io
import sys

import pandas as pd
import requests
import yfinance as yf
from sqlalchemy import create_engine, text

import config

engine = create_engine(config.DB_PATH)


# ---------------------------------------------------------------------------
# 1. PRICES (yfinance)
# ---------------------------------------------------------------------------
def fetch_prices():
    print("Fetching index/ETF prices from yfinance...")
    raw = list(config.PRICE_TICKERS.keys())

    # Pull 2 years so the 200-day average, 52-week range, Bollinger Bands
    # and RSI all have enough history to compute. yfinance returns the full
    # range every time; the upsert dedupes, so re-running is cheap and safe.
    # (auto_adjust=False guarantees an 'Adj Close' column exists — newer
    #  yfinance defaults to True and DROPS that column, causing a KeyError.)
    data = yf.download(
        raw, period="2y", group_by="ticker",
        auto_adjust=False, progress=False,
    )

    rows = []
    for raw_ticker in raw:
        try:
            tdf = data[raw_ticker].dropna(how="all")
        except (KeyError, TypeError):
            print(f"  -> no data returned for {raw_ticker}, skipping")
            continue

        ticker_id = config.PRICE_TICKERS[raw_ticker]
        for dt, r in tdf.iterrows():
            # Adj Close may or may not be present depending on yf version;
            # fall back to Close so we never crash.
            adj = r["Adj Close"] if "Adj Close" in r.index and pd.notna(r["Adj Close"]) else r["Close"]
            if pd.isna(r["Close"]):
                continue
            rows.append({
                "ticker_id": ticker_id,
                "trade_date": dt.date(),
                "close": float(r["Close"]),
                "adj_close": float(adj),
                "volume": int(r["Volume"]) if pd.notna(r["Volume"]) else 0,
            })

    _upsert(rows, "daily_prices", keys=["ticker_id", "trade_date"])
    print(f"  prices: {len(rows)} rows processed")


def fetch_holdings():
    """Fetch price history + a valuation snapshot for personal holdings.
    Prices go into daily_prices (same table as indices, so the existing
    price-block charts/RSI/MA work). Valuation (P/E, P/B, yield) goes into
    a per-day snapshot in valuation_history, accruing over time."""
    holdings = getattr(config, "HOLDINGS", {})
    if not holdings:
        return
    print("Fetching personal holdings from yfinance...")
    raw = list(holdings.keys())

    # Prices — 2y history for MA/RSI/52wk, same as indices.
    data = yf.download(raw, period="2y", group_by="ticker",
                       auto_adjust=False, progress=False)
    rows = []
    for raw_ticker in raw:
        try:
            tdf = data[raw_ticker].dropna(how="all") if len(raw) > 1 else data.dropna(how="all")
        except (KeyError, TypeError):
            print(f"  -> no price data for {raw_ticker}, skipping")
            continue
        for dt, r in tdf.iterrows():
            if "Close" not in r.index or pd.isna(r["Close"]):
                continue
            adj = r["Adj Close"] if "Adj Close" in r.index and pd.notna(r["Adj Close"]) else r["Close"]
            rows.append({
                "ticker_id": raw_ticker,        # store under the raw ticker
                "trade_date": dt.date(),
                "close": float(r["Close"]),
                "adj_close": float(adj),
                "volume": int(r["Volume"]) if pd.notna(r["Volume"]) else 0,
            })
    _upsert(rows, "daily_prices", keys=["ticker_id", "trade_date"])
    print(f"  holdings prices: {len(rows)} rows processed")

    # Valuation snapshot (today) — P/E, P/B, dividend yield, name.
    today = datetime.date.today()
    vrows = []
    for raw_ticker in raw:
        try:
            info = yf.Ticker(raw_ticker).info
        except Exception as e:
            print(f"  -> {raw_ticker}: .info failed ({e}), skipping valuation")
            continue
        vrows.append({
            "ticker_id": raw_ticker,
            "trade_date": today,
            "pe_ratio":   _num(info.get("trailingPE")),
            "forward_pe": _num(info.get("forwardPE")),
            "pb_ratio":   _num(info.get("priceToBook")),
            "source":     "yfinance_holding",
        })
    _upsert(vrows, "valuation_history", keys=["ticker_id", "trade_date"])
    print(f"  holdings valuation: {len(vrows)} rows processed")

    # Dividend ex-dates (history). yfinance .dividends returns the ex-dividend
    # date as the index — reliable and deep. These explain a real mechanical
    # move: a stock drops ~the dividend amount on its ex-date.
    drows = []
    for raw_ticker in raw:
        try:
            divs = yf.Ticker(raw_ticker).dividends
        except Exception as e:
            print(f"  -> {raw_ticker}: dividends fetch failed ({e})")
            continue
        if divs is None or len(divs) == 0:
            continue
        for dt, amount in divs.items():
            try:
                d = dt.date()
            except Exception:
                continue
            drows.append({
                "ticker_id": raw_ticker,
                "ex_date": d,
                "amount": float(amount),
            })
    _upsert(drows, "dividend_events", keys=["ticker_id", "ex_date"])
    print(f"  holdings dividends: {len(drows)} ex-dates processed")

    # Earnings dates (history + next). yfinance.get_earnings_dates returns past
    # and upcoming dates with EPS estimate, reported EPS, and surprise %.
    # Depth varies hugely by name (KOG: years; small caps: a row or two), so we
    # store whatever exists and let the dashboard caption state the coverage.
    erows = []
    for raw_ticker in raw:
        try:
            edf = yf.Ticker(raw_ticker).get_earnings_dates(limit=24)
        except Exception as e:
            print(f"  -> {raw_ticker}: earnings dates failed ({e})")
            continue
        if edf is None or len(edf) == 0:
            continue
        for dt, row in edf.iterrows():
            try:
                d = dt.date()
            except Exception:
                continue
            # Surprise % may be NaN (future dates or missing). Keep it nullable.
            surprise = row.get("Surprise(%)")
            erows.append({
                "ticker_id": raw_ticker,
                "earnings_date": d,
                "surprise_pct": _num(surprise),
                "reported_eps": _num(row.get("Reported EPS")),
            })
    _upsert(erows, "earnings_events", keys=["ticker_id", "earnings_date"])
    print(f"  holdings earnings: {len(erows)} dates processed")


# ---------------------------------------------------------------------------
# 2. MACRO (FRED)
# ---------------------------------------------------------------------------
def fetch_macro():
    if config.FRED_API_KEY.startswith("YOUR_"):
        print("Skipping FRED (no API key set in config.py).")
        return

    print("Fetching macro series from FRED...")
    rows = []
    # 3 years of history. The daily series (VIX, yields, spread) only need a
    # few months for their charts, but the monthly CPI index needs 13+ months
    # to compute a year-over-year inflation rate — so we pull a long window
    # for all of them. The upsert dedupes, so re-running stays cheap.
    start = (datetime.date.today() - datetime.timedelta(days=365 * 3)).isoformat()

    for series_id, name in config.FRED_SERIES.items():
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series_id,
            "api_key": config.FRED_API_KEY,
            "file_type": "json",
            "observation_start": start,
        }
        try:
            resp = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            print(f"  -> {name}: request failed ({e})")
            continue

        for obs in resp.get("observations", []):
            val = obs.get("value")
            if val in (".", "", None):   # FRED uses "." for missing
                continue
            rows.append({
                "series_id": name,
                "obs_date": pd.to_datetime(obs["date"]).date(),
                "value": float(val),
            })

    _upsert(rows, "macro_series", keys=["series_id", "obs_date"])
    print(f"  macro: {len(rows)} rows processed")


# ---------------------------------------------------------------------------
# 3. VALUATION SNAPSHOT (yfinance .info — Oslo proxy only)
# ---------------------------------------------------------------------------
def fetch_valuation_snapshot():
    # S&P valuation comes entirely from multpl (consistent index figures).
    # We do NOT snapshot SPY here: yfinance's ETF priceToBook is computed
    # differently from the index P/B multpl reports, so mixing them put a
    # bogus ~1.7 spike on the chart. Oslo has no free history, so it's the
    # only thing that legitimately accrues from snapshots.
    print("Snapshotting current valuation ratios (OBXD.OL only)...")
    proxies = {"OBXD.OL": "OBXD.OL"}
    today = datetime.date.today()
    rows = []

    for yf_ticker, ticker_id in proxies.items():
        try:
            info = yf.Ticker(yf_ticker).info
        except Exception as e:
            print(f"  -> {yf_ticker}: .info failed ({e}), skipping")
            continue

        rows.append({
            "ticker_id": ticker_id,
            "trade_date": today,
            "pe_ratio":   _num(info.get("trailingPE")),
            "forward_pe": _num(info.get("forwardPE")),
            "pb_ratio":   _num(info.get("priceToBook")),
            "source":     "yfinance_live",
        })

    _upsert(rows, "valuation_history", keys=["ticker_id", "trade_date"])
    print(f"  valuation snapshot: {len(rows)} rows processed")


# ---------------------------------------------------------------------------
# 4. ONE-TIME S&P 500 BACKFILL (multpl, monthly)
# ---------------------------------------------------------------------------
def backfill_sp500_history():
    """Seed SPY valuation_history with real monthly P/E & P/B from multpl.
    Safe to run repeatedly; only fills dates not already present."""
    if not config.SP500_BACKFILL:
        return

    # Only backfill if we don't already have a decent history.
    with engine.connect() as c:
        existing = c.execute(text(
            "SELECT COUNT(*) FROM valuation_history "
            "WHERE ticker_id='SPY' AND source='multpl'"
        )).scalar() if _table_exists("valuation_history") else 0
    if existing and existing > 12:
        print("S&P backfill already present, skipping.")
        return

    print("Backfilling S&P 500 monthly P/E & P/B from multpl (one-time)...")
    pe = _scrape_multpl("https://www.multpl.com/s-p-500-pe-ratio/table/by-month", "pe_ratio")
    pb = _scrape_multpl("https://www.multpl.com/s-p-500-price-to-book/table/by-month", "pb_ratio")
    if pe is None and pb is None:
        print("  -> multpl scrape failed (site layout may have changed). "
              "Charts will still build going forward from live snapshots.")
        return

    merged = pd.merge(pe, pb, on="trade_date", how="outer") if (pe is not None and pb is not None) \
        else (pe if pe is not None else pb)
    merged["ticker_id"] = "SPY"
    merged["forward_pe"] = None
    merged["source"] = "multpl"

    # Keep only the recent window (config.SP500_BACKFILL_YEARS) so the chart
    # isn't swamped by 150 years of history.
    cutoff = datetime.date.today() - datetime.timedelta(
        days=365 * config.SP500_BACKFILL_YEARS)
    merged = merged[merged["trade_date"] >= cutoff]

    rows = merged.to_dict("records")
    _upsert(rows, "valuation_history", keys=["ticker_id", "trade_date"])
    print(f"  backfill: {len(rows)} monthly rows added")


def _scrape_multpl(url, value_col):
    """multpl publishes a clean single <table>. pandas.read_html does the rest."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}  # multpl blocks empty UA
        html = requests.get(url, headers=headers, timeout=30).text
        tables = pd.read_html(io.StringIO(html))
        df = tables[0]
        df.columns = ["date_raw", "value"]
        df["trade_date"] = pd.to_datetime(df["date_raw"], errors="coerce").dt.date
        df["value"] = (
            df["value"].astype(str).str.replace(",", "").str.extract(r"([\d.]+)")[0].astype(float)
        )
        df = df.dropna(subset=["trade_date", "value"])
        return df[["trade_date", "value"]].rename(columns={"value": value_col})
    except Exception as e:
        print(f"  -> scrape failed for {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # filter NaN
    except (TypeError, ValueError):
        return None


def _table_exists(name):
    with engine.connect() as c:
        r = c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"
        ), {"n": name}).fetchone()
    return r is not None


def _upsert(rows, table, keys):
    """Insert rows, replacing any existing row with the same key.
    Uses pandas for table creation + a dedup so we never write duplicates.
    Parameterised throughout (no f-string SQL)."""
    if not rows:
        return
    df = pd.DataFrame(rows).drop_duplicates(subset=keys, keep="last")

    if not _table_exists(table):
        df.to_sql(table, con=engine, if_exists="append", index=False)
        return

    # Delete the keys we're about to write, then append. Simple last-write-wins.
    with engine.begin() as c:
        cols = " AND ".join(f"{k}=:{k}" for k in keys)
        for _, row in df.iterrows():
            c.execute(text(f"DELETE FROM {table} WHERE {cols}"),
                      {k: row[k] for k in keys})
    df.to_sql(table, con=engine, if_exists="append", index=False)


def fetch_norway_cpi():
    """Pull Norway's all-items CPI index from Statistics Norway (SSB),
    table 14700, via PxWebApi v2 (GET, json-stat2). No API key needed.
    We store the monthly index; the dashboard computes year-over-year %.

    Confirmed from the live table metadata: the consumption-group variable
    is 'VareTjenesteGrp' and the all-items total is code '00' ("Total").
    KpiIndMnd is the monthly index. Fails gracefully to an empty panel if
    SSB ever reorganises (check https://www.ssb.no/statbank/table/14700).
    """
    print("Fetching Norway CPI from Statistics Norway (SSB table 14700)...")
    url = ("https://data.ssb.no/api/pxwebapi/v2/tables/14700/data"
           "?lang=en&valueCodes[VareTjenesteGrp]=00"
           "&valueCodes[ContentsCode]=KpiIndMnd"
           "&valueCodes[Tid]=from(2018M01)&outputFormat=json-stat2")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        js = resp.json()
    except Exception as e:
        print(f"  -> SSB request failed ({e}); Norway CPI will be empty. "
              "If this persists, check the table at ssb.no/statbank/table/14700.")
        return

    if not js.get("value"):
        print("  -> SSB returned no values; the classification may have changed.")
        return

    try:
        tid = js["dimension"]["Tid"]["category"]["index"]
        periods = sorted(tid, key=lambda k: tid[k])
        values = js["value"]
        rows = []
        for code in periods:
            pos = tid[code]
            v = values[pos] if pos < len(values) else None
            if v is None:
                continue
            yr, mo = code.split("M")
            d = datetime.date(int(yr), int(mo), 1)
            rows.append({"series_id": "NO_CPI_INDEX", "obs_date": d,
                         "value": float(v)})
    except Exception as e:
        print(f"  -> SSB parse failed ({e}); response format may have changed.")
        return

    _upsert(rows, "macro_series", keys=["series_id", "obs_date"])
    print(f"  Norway CPI: {len(rows)} monthly rows processed")


def run_all():
    fetch_prices()
    fetch_holdings()
    fetch_macro()
    fetch_norway_cpi()
    fetch_valuation_snapshot()
    backfill_sp500_history()
    print("Database update complete.")


if __name__ == "__main__":
    run_all()
