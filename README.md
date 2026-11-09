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

## Two modes: public and owner
| | **public** (for a URL you share) | **owner** (your own private tool) |
|---|---|---|
| Who it is for | anyone who opens the link | you only |
| Lists (holdings, buy list) | live **only in the visitor's own browser tab**; nothing is stored on the server | stored: local files, or Google Sheets on the cloud |
| Can visitors see each other's shares? | **No.** Each visitor has a separate session | n/a (single user) |
| Keeping a list | visitor downloads / uploads a CSV (Manage lists page) | automatic |
| Default buy list | a built-in list every visitor sees; **cannot be edited or deleted** | none |
| Paper trades, saved trailing stops | not available | yes |
| Limits | 10 different shares per visitor, 40 shares on the whole server | none |

The mode is chosen automatically: **on Streamlit Community Cloud the app starts in public mode**, anywhere else (your
computer) in owner mode. So a forgotten setting can never expose a private portfolio. Override it with the app secret
`mode = "owner"` or `"public"` (or the environment variable `TRADESATHI_MODE`).

The default buy list for public mode is `PUBLIC_DEFAULT_BUYLIST` in `tradesathi/config.py` (currently TCS, RELIANCE,
INFY, WIPRO, ADANIENT, 100 shares each; use NSE trading symbols). It is shown first on every visitor's Manage lists
page in a read-only table, it does not count toward their 10-share limit, and a visitor cannot change, remove or
re-add it. Visitors add their own shares below it.

In public mode the server keeps only anonymous market data (candles and each share's analysis, shared by all visitors
to stay fast). It never writes a visitor's holdings, buy list, stops or paper trades anywhere, and the historical-check
page shows a visitor only the shares in their own lists.

## Deploying to Streamlit Community Cloud
### A. Public app (share this link)
1. Create the app at share.streamlit.io: repo, branch `main`, main file `dashboard.py`, *Advanced settings* -> Python 3.12.
2. That is all: no secrets are needed. Do not add `[storage]` or `app_password` here (optional: `app_password` makes
   everyone enter one shared password first).
3. Your own holdings.csv / buylist.csv in the repo are **not** used in public mode.

### B. Your private copy (your real portfolio)
1. Deploy the same repo a second time (a different app URL) from a **private** GitHub repo or fork.
2. In its *Secrets* set `mode = "owner"`, an `app_password`, and (so your lists survive restarts, because the cloud
   disk is wiped) a `[storage]` section: create a Google Cloud service account (Sheets API + Drive API enabled),
   download its JSON key, create an empty Google Sheet and share it (Editor) with the service account's `client_email`.
   Use `.streamlit/secrets.toml.example` as the template. The first start copies your committed holdings.csv /
   buylist.csv into the sheet (tab `tradesathi_kv`); after that the sheet is the source of truth.
3. Optionally also restrict viewers in the app's *Sharing* settings.

Things to know: apps sleep after ~12 hours without visitors (opening the URL wakes them) and their disk is wiped on
restart, so candle history and historical checks are rebuilt (the first analysis of a share takes about a minute).
In owner mode the engine is a thread inside the app, so paper trades only advance while the app is awake. Free data
from cloud IPs can be rate limited: errors show per share in the dashboard.

## Project layout
```
dashboard.py              Streamlit entry point (the "main file" on Streamlit Cloud)
tradesathi/
  config.py               all settings
  engine.py               owner mode: background analysis thread used by the dashboard
  mode.py                 public / owner mode switch
  visitor.py              public mode: per-visitor lists, shared price cache, symbol validation
  main.py                 the analysis cycle + terminal mode (python -m tradesathi.main)
  data_source.py          free market-data sources (Upstox, Yahoo)
  storage/                persist.py (local files or Google Sheets), store.py (candles, JSON), holdings.py, buylist.py
  analysis/               indicators, patterns, strategy, allocator, buyplan, positions,
                          backtest, history (historical check), paper (paper trading)
tests/                    offline tests (python -m pytest)
.streamlit/               secrets.toml.example
holdings.csv, buylist.csv your lists in owner mode (seed the Google Sheet on first cloud start)
data/                     local cache of candles and results (git-ignored, recreated automatically)
```
