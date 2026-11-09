"""Default settings. Symbols and capital can also be passed on the command line."""
TZ_NAME = "Asia/Kolkata"
DATA_DIR = "data"
DATA_SOURCES = ["upstox", "yahoo"]   # free sources, tried in this order (see data_source.py)

DEFAULT_SYMBOLS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
DEFAULT_CAPITAL = 100000.0

# --- risk / sizing ---
RISK_PCT = 0.01          # max loss per trade as a fraction of capital
MAX_ALLOC = 0.30         # max capital in a single stock
CHARGES_PCT = 0.003      # round trip for delivery trades: STT 0.1% each side + stamp duty, exchange, GST, DP (~0.3%)

# --- entry / exit ---
ATR_SOURCE = "daily"     # "daily" = swing style, "intraday" = tighter 5-min ATR
ENTRY_BAND = 0.25        # entry zone = anchor +/- ENTRY_BAND * ATR
ATR_STOP_MULT = 1.5      # stop = entry - 1.5 * ATR (unless swing stop is tighter)
ATR_MIN_STOP_MULT = 0.75 # never place the stop closer than this (noise)
MIN_RR = 1.5             # reject setups with less room to resistance than this
T1_R, T2_R = 1.5, 2.0    # targets in multiples of risk

# --- signal thresholds (score range is roughly -10..+10) ---
BUY_SCORE = 4
AVOID_SCORE = -3

INTRA_RULE = "5min"      # candle size for live intraday analysis. Keep it equal to the history check (5-min candles, the only
                         # long history available): a different size means the EDGE verdicts describe other rules than the live ones
REFRESH_SECONDS = 60

# Normal session is Mon-Fri 09:15-15:30 IST. Add special days here as "YYYY-MM-DD": ("HH:MM", "HH:MM"),
# e.g. Diwali Muhurat trading (usually a weekend/holiday). Use None to mark a full exchange holiday.
SPECIAL_SESSIONS = {
    "2026-11-08": ("14:20", "15:30"),   # Diwali Muhurat trading
}


def market_open(now):
    """True if the exchange is open at `now` (IST-aware datetime), honouring SPECIAL_SESSIONS."""
    import datetime as _dt
    sp = SPECIAL_SESSIONS.get(now.date().isoformat(), "normal")
    if sp is None:
        return False
    if sp == "normal":
        return now.weekday() < 5 and _dt.time(9, 15) <= now.time() <= _dt.time(15, 30)
    start, end = (_dt.time.fromisoformat(x) for x in sp)
    return start <= now.time() <= end

# --- historical validation (backtest on DAILY candles) ---
HISTORY_PERIOD = "10y"          # how much daily history to download (5y test + warm-up for EMA200)
BACKTEST_YEARS = 5              # years of signals to test (use --years 8 etc. in tradesathi/analysis/backtest.py)
MIN_HISTORY_BARS = 1100         # re-download full history if the stored file is shorter than this
BT_MAX_WAIT = 3                 # a pullback buy order stays valid this many days
BT_MAX_HOLD = 20                # close the trade at market after this many days
MIN_TRADES = 20                 # fewer past trades than this = "INSUFFICIENT DATA"
EDGE_MIN_EXPECTANCY = 0.10      # average net R per trade needed to call it an EDGE
EDGE_MIN_PF = 1.2               # profit factor needed to call it an EDGE
REQUIRE_HISTORICAL_EDGE = True  # block BUY signals on shares without a proven historical edge
ALLOWED_EDGES = ("EDGE",)       # verdicts that may BUY. Add "WEAK EDGE" to loosen (not recommended: t-stat < 2 = may be luck)
EDGE_MIN_TSTAT = 2.0              # statistical significance needed for EDGE (about 95% confidence it is not luck)
HISTORY_TEST = "intraday"       # "intraday" = replay the real live rules on 5-min history (Upstox, from 2022);
                                # "daily" = the older daily-candle approximation. Falls back to daily if no 5-min history.
MIN_INTRADAY_YEARS = 1.0        # need at least this much 5-min history to use the intraday test
CONFIRM_CYCLES = 2              # a BUY NOW must hold this many consecutive cycles before it is alerted / confirmed

# --- shares the user wants to buy (buylist.csv) ---
SPLIT_ENTRY = True       # split a buy in two: half at the entry price, half at the bottom of the buy zone
MAX_STOP_PCT = 8.0       # warn when the stop is further than this % below the entry
EXTENDED_ATR = 1.5       # "do not chase" warning when price is this many ATR above the buy zone

# --- public (visitor) mode: every visitor's lists live only in their own browser tab ---
MAX_SYMBOLS_PER_VISITOR = 10    # different shares one visitor may track (holdings + buy list together)
MAX_TOTAL_SYMBOLS = 40          # different shares the server analyses for all visitors together (disk, memory, rate limits)
VISITOR_TTL_CLOSED = 900        # seconds a share's analysis is reused while the market is closed

# Buy list shown to EVERY visitor in public mode (symbol, quantity). Visitors cannot change or remove it, and it does
# not count toward their own MAX_SYMBOLS_PER_VISITOR. Use NSE trading symbols (Reliance is RELIANCE, not RIL).
PUBLIC_DEFAULT_BUYLIST = [("TCS", 100), ("RELIANCE", 100), ("INFY", 100), ("WIPRO", 100), ("ADANIENT", 100)]
