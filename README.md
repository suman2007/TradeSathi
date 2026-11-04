# Trading analyzer: quick start

**Start it** (Windows: `venv\Scripts\python`, Mac/Linux: `venv/bin/python`)

```
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
venv\Scripts\python -m streamlit run dashboard.py
```

The dashboard starts the analysis engine itself: it downloads history, replays the live rules on past data for every
share (historical check), and re-analyses live data every minute. Ctrl+C stops it. Terminal-only mode:
`python -m tradesathi.main` (add `--once` for a single pass).

## Data (free)
| Source | Used for | Notes |
|---|---|---|
| Upstox public market-data API | everything (default) | No account or login. 1-minute history from Jan 2022, 10+ years daily |
| Yahoo Finance (yfinance) | automatic fallback | Used when Upstox fails or does not list a symbol |

Change the order in `config.py` (`DATA_SOURCES`). Live data is decision support; check prices in your broker app.

## What you give it (all optional, edit while it runs)
| File | For | Columns |
|---|---|---|
| `holdings.csv` | shares you already own | `symbol,qty,buy_price` |
| `buylist.csv` | shares you want to buy | `symbol,amount,qty,max_price` (amount OR qty; qty wins; max_price optional) |

Example buylist row for "invest 10000, pay at most 300": `PPLPHARMA,10000,,300` (note the empty qty column).

You can also type both lists into the dashboard, or pass them on the command line:
`python -m tradesathi.main --symbols SBIN ITC --capital 100000 --holding RELIANCE:10:2450 --buy TCS:30000:3900`

## What you get
- Holdings: live P&L, trailing stop, targets, HOLD / BOOK PROFIT / EXIT call.
- Shares you want to buy: BUY NOW / PLACE LIMIT ORDER / WAIT / DO NOT BUY, with zone, stop, targets, quantity and risk.
  A BUY NOW must hold for 2 cycles before it is confirmed (filters one-minute flickers).
- New ideas: ranked setups sized to your free capital.
- Historical check: the real live rules replayed on 5-minute candles since 2022 (EDGE / WEAK EDGE / NO EDGE /
  INSUFFICIENT DATA). Only EDGE (profitable AND t-stat >= 2, i.e. unlikely to be luck) may BUY.
- Paper trades: every live BUY NOW is followed automatically in `data/paper_trades.csv`, so you can see whether
  the live signal makes money before risking real money.

## Testing the rules yourself
```
python -m tradesathi.analysis.backtest --intraday                       # your holdings + default list, real live rules on 5-min data
python -m tradesathi.analysis.backtest --universe nifty100 --intraday   # broad test on every current Nifty 100 share
python -m tradesathi.analysis.backtest --symbols SBIN ITC --years 8     # daily-candle version, longer history
```
Charges are set to 0.3% per round trip (`CHARGES_PCT`), close to real delivery costs in India.

Delayed free data and rule-based signals: decision support, not investment advice.


## Testing locally (no network needed)
```
venv\Scripts\python -m pip install -r requirements-dev.txt     (Mac/Linux: venv/bin/python)
venv\Scripts\python -m pytest
```
The tests use synthetic price data and an in-memory fake of Google Sheets. They check: storage (local files and
Sheets, including long texts), one full engine cycle, and the real `dashboard.py` rendered headless (also the password
gate). To try the cloud behaviour by hand, run only the dashboard, exactly as Streamlit Cloud does:
```
venv\Scripts\python -m streamlit run dashboard.py
```
It starts its own engine thread. To test the password gate or Google Sheets storage locally, copy
`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in (that file is git-ignored).

## Deploying to Streamlit Community Cloud
1. **Make the GitHub repo private** (holdings.csv / buylist.csv are in it). Community Cloud can deploy private repos.
2. **Persistent storage (recommended).** The cloud disk is wiped on every restart. To keep holdings, buy list, paper trades
   and trailing stops: create a Google Cloud service account (Sheets API + Drive API enabled), download its JSON key,
   create an empty Google Sheet and share it (Editor) with the service account's `client_email`. The first start copies
   your committed holdings.csv / buylist.csv into the sheet (tab `tradesathi_kv`); after that the sheet is the source of truth.
3. **Create the app** at share.streamlit.io: repo, branch `main`, main file `dashboard.py`. In *Advanced settings* choose
   Python 3.12 and paste the contents of `.streamlit/secrets.toml.example` (filled in) into *Secrets*.
4. **Restrict access**: set `app_password` in the secrets (the dashboard asks for it), and/or use the app's
   *Sharing* settings to allow only your email.

Things to know: the engine runs as a thread inside the app, so it only runs while the app is awake (Community Cloud
puts idle apps to sleep after ~12 hours without visitors; opening the URL wakes it). Candle history and the historical
check are rebuilt after each restart (first start takes several minutes). Paper trades therefore only advance while
the app is awake. Free data from cloud IPs can be rate limited: errors show per share in the dashboard.

## Project layout
```
dashboard.py              Streamlit entry point (the "main file" on Streamlit Cloud)
tradesathi/
  config.py               all settings
  engine.py               background analysis thread used by the dashboard
  main.py                 the analysis cycle + terminal mode (python -m tradesathi.main)
  data_source.py          free market-data sources (Upstox, Yahoo)
  storage/                persist.py (local files or Google Sheets), store.py (candles, JSON), holdings.py, buylist.py
  analysis/               indicators, patterns, strategy, allocator, buyplan, positions,
                          backtest, history (historical check), paper (paper trading)
tests/                    offline tests (python -m pytest)
.streamlit/               secrets.toml.example
holdings.csv, buylist.csv your lists (seed the Google Sheet on first cloud start)
data/                     local cache of candles and results (git-ignored, recreated automatically)
```
