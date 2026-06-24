"""
config.py
---------
Everything you might want to change lives here, so you never have to dig
through the logic files. Edit values, save, done.
"""
import os

# --- Database ---
DB_PATH = "sqlite:///market_data.db"

# --- FRED (macro data: VIX, yields, USD/NOK, Brent) ---
# Get a free key at https://fredaccount.stlouisfed.org/apikeys (instant, no card).
# The key is read from the FRED_API_KEY environment variable. It is set:
#   - Locally: set it in your terminal before running, OR paste it below.
#   - GitHub Actions (the daily robot): from the repo secret FRED_API_KEY.
#   - Streamlit Cloud (the hosted app): from Streamlit secrets (the app
#     pushes st.secrets into the environment before importing this file).
# IMPORTANT: do NOT paste your real key here if this repo is public. Leave the
# placeholder and use the secret stores above.
FRED_API_KEY = os.environ.get("FRED_API_KEY", "YOUR_FRED_KEY_HERE")

# FRED series we pull. Left = FRED's code, right = friendly name stored in DB.
FRED_SERIES = {
    "VIXCLS":      "VIX",            # CBOE Volatility Index, daily
    "DGS10":       "US_10Y",         # US 10-Year Treasury yield
    "DGS2":        "US_2Y",          # US 2-Year Treasury yield (daily)
    "IRLTLT01NOM156N": "NO_10Y",     # Norway long-term govt bond yield (monthly)
    "DEXNOUS":     "USD_NOK",        # USD/NOK exchange rate
    "DCOILBRENTEU":"BRENT",          # Brent crude, USD/barrel
    "BAMLH0A0HYM2": "HY_SPREAD",     # US high-yield credit spread (daily, %)
    "CPIAUCSL":    "US_CPI_INDEX",    # US CPI index (monthly) — YoY computed in dashboard
}

# --- Price tickers (yfinance). Real daily history, free, deep. ---
PRICE_TICKERS = {
    "^GSPC":    "SPX_INDEX",     # S&P 500 index
    "OSEBX.OL": "OSEBX_INDEX",   # Oslo Børs benchmark index
    "SPY":      "SPY",           # S&P 500 ETF (valuation proxy)
    "OBXD.OL":  "OBXD.OL",       # Oslo OBX ETF (valuation proxy)
}

# --- Valuation proxies: where each market's P/E & P/B chart sources its
#     CURRENT value (live) and HISTORICAL series. ---
# S&P history comes free from multpl (monthly). Oslo has no free history,
# so it accrues from today forward via the daily snapshot.
SP500_BACKFILL = True   # scrape multpl once to seed S&P monthly history

# How many years of S&P history to keep. multpl goes back to 1871, which
# makes charts unreadable. 5 years is plenty of context. Bump if you want more.
SP500_BACKFILL_YEARS = 5

# --- Valuation bands (tune these to whatever you consider cheap/expensive).
#     These are NOT advice — they're your own reference rails. The defaults
#     below are placeholders; current S&P P/B is ~6, well above old 2-4.5.
BANDS = {
    "SP500": {
        "pb":      {"low": 3.0, "high": 5.0},
        "pe_ttm":  {"low": 18,  "high": 26},
        "fwd_pe":  {"low": 16,  "high": 22},
    },
    "OSLO": {
        "pb":      {"low": 1.5, "high": 2.4},
        "pe_ttm":  {"low": 10,  "high": 17},
        "fwd_pe":  {"low": 9,   "high": 16},
    },
}

# --- Financial calendar -----------------------------------------------------
# Hand-maintained list of scheduled market-moving events. Central banks publish
# these ~a year ahead, so update once a year. Format: (date, type, label).
# The dashboard flags upcoming ones in a panel and draws faint vertical markers
# on the time-series charts so past moves line up visibly with past events.
#
# IMPORTANT: it shows events; it does NOT claim a given event *caused* a given
# market move. You draw that inference yourself.
#
# Sources (verified): FOMC = federalreserve.gov; Norges Bank = norges-bank.no;
# ECB = ecb.europa.eu. CPI dates are mid-month approximations — confirm exact
# BLS release days at bls.gov if you need precision. Earnings "seasons" are
# the rough windows when most large caps report.
CALENDAR = [
    # --- US Federal Reserve (FOMC), 2026 — confirmed ---
    ("2026-01-28", "Fed", "FOMC decision"),
    ("2026-03-18", "Fed", "FOMC decision + projections"),
    ("2026-04-29", "Fed", "FOMC decision"),
    ("2026-06-17", "Fed", "FOMC decision + projections"),
    ("2026-07-29", "Fed", "FOMC decision"),
    ("2026-09-16", "Fed", "FOMC decision + projections"),
    ("2026-10-28", "Fed", "FOMC decision"),
    ("2026-12-09", "Fed", "FOMC decision + projections"),

    # --- Norges Bank, 2026 — confirmed ---
    ("2026-01-22", "Norges Bank", "Policy rate decision"),
    ("2026-03-26", "Norges Bank", "Policy rate + report"),
    ("2026-05-07", "Norges Bank", "Policy rate decision"),
    ("2026-06-18", "Norges Bank", "Policy rate + report"),
    ("2026-08-13", "Norges Bank", "Policy rate decision"),
    ("2026-09-24", "Norges Bank", "Policy rate + report"),
    ("2026-11-05", "Norges Bank", "Policy rate decision"),
    ("2026-12-17", "Norges Bank", "Policy rate + report"),

    # --- ECB Governing Council rate decisions, 2026 — confirmed ---
    ("2026-03-19", "ECB", "Rate decision"),
    ("2026-04-30", "ECB", "Rate decision"),
    ("2026-06-11", "ECB", "Rate decision"),
    ("2026-07-23", "ECB", "Rate decision"),
    ("2026-09-10", "ECB", "Rate decision"),
    ("2026-10-29", "ECB", "Rate decision"),
    ("2026-12-17", "ECB", "Rate decision"),

    # --- US CPI inflation releases, 2026 — APPROXIMATE (mid-month) ---
    ("2026-01-13", "US CPI", "Inflation report (approx)"),
    ("2026-02-11", "US CPI", "Inflation report (approx)"),
    ("2026-03-11", "US CPI", "Inflation report (approx)"),
    ("2026-04-10", "US CPI", "Inflation report (approx)"),
    ("2026-05-12", "US CPI", "Inflation report (approx)"),
    ("2026-06-10", "US CPI", "Inflation report (approx)"),
    ("2026-07-14", "US CPI", "Inflation report (approx)"),
    ("2026-08-12", "US CPI", "Inflation report (approx)"),
    ("2026-09-11", "US CPI", "Inflation report (approx)"),
    ("2026-10-13", "US CPI", "Inflation report (approx)"),
    ("2026-11-12", "US CPI", "Inflation report (approx)"),
    ("2026-12-10", "US CPI", "Inflation report (approx)"),

    # --- Earnings season start (rough windows when most large caps report) ---
    ("2026-01-13", "Earnings", "Q4 2025 earnings season begins"),
    ("2026-04-14", "Earnings", "Q1 2026 earnings season begins"),
    ("2026-07-14", "Earnings", "Q2 2026 earnings season begins"),
    ("2026-10-13", "Earnings", "Q3 2026 earnings season begins"),
]

# Colour per event type for the chart markers / panel chips.
CALENDAR_COLORS = {
    "Fed":         "#3DA5D9",
    "Norges Bank": "#E0A458",
    "ECB":         "#9B8FD9",
    "US CPI":      "#5FB88A",
    "Earnings":    "#C77DFF",
}
