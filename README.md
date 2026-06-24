# Market Context Dashboard

A free, daily-refresh dashboard for building market understanding across the
S&P 500 and Oslo Børs. Not a trading-signal tool.

## What it does
- **Prices** (S&P 500, OSEBX) — real daily history from yfinance.
- **Macro** (VIX, US/Norway 10Y yields, USD/NOK, Brent) — from FRED.
- **S&P valuation** (P/E, P/B) — real monthly history backfilled from multpl,
  extended daily.
- **Oslo valuation** — accrues from the day you start (no free history exists).

## Data honesty
There is no free source of historical Oslo index valuation ratios. The Oslo
tab therefore builds up over time rather than showing fake history. The S&P
tab uses multpl's monthly series, so its resolution is monthly, not daily.

## Setup (local)
1. Install Python 3.12, then: `pip install -r requirements.txt`
2. Get a free FRED key: https://fredaccount.stlouisfed.org/apikeys
3. Paste it into `config.py` (or set env var `FRED_API_KEY`).
4. `python fetch_data.py`   (builds the database)
5. `streamlit run dashboard.py`

## Setup (hands-off, cloud)
1. Push this folder to a GitHub repo.
2. Repo Settings > Secrets and variables > Actions > add secret `FRED_API_KEY`.
3. The workflow in `.github/workflows/daily-fetch.yml` runs every weekday at
   18:00 UTC, fetches data, and commits `market_data.db` back to the repo.
4. (Optional) Deploy `dashboard.py` free on Streamlit Community Cloud pointed
   at the same repo.

## Tuning
All tickers, FRED series, and valuation bands live in `config.py`.
The bands are your own reference rails — edit them freely.
