"""
config.py
=========
UW Options Bot — Pal Initiatives LLC
All settings loaded from .env in the project root.

Data   : Unusual Whales API  (Bearer token)
Broker : IBKR ib_async       (live quotes + account value)
Alerts : Telegram
Output : Excel signals.xlsx
"""

import os
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

ET = ZoneInfo("America/New_York")

# ── Logging ──────────────────────────────────────────────────────────────────
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

# Windows-safe stream handler
import io as _io
_stream = (
    _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer")
    else sys.stdout
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(_stream),
        logging.FileHandler(_log_dir / "uw_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("UWBot")


# ── Config ───────────────────────────────────────────────────────────────────
class Config:

    # ── Unusual Whales ───────────────────────────────────────────────────────
    UW_API_KEY: str   = os.getenv("UW_API_KEY", "")
    UW_BASE: str      = "https://api.unusualwhales.com"

    # ── IBKR ─────────────────────────────────────────────────────────────────
    IBKR_HOST: str    = os.getenv("IBKR_HOST", "127.0.0.1")
    IBKR_PORT: int    = int(os.getenv("IBKR_PORT", "7497"))   # 7497=TWS | 4001=Gateway
    IBKR_CLIENT_ID: int = int(os.getenv("IBKR_CLIENT_ID", "10"))
    IBKR_ACCOUNT: str = os.getenv("IBKR_ACCOUNT", "")         # Pal Initiatives LLC account ID

    # ── Schwab fallback broker ───────────────────────────────────────────────
    # Only used when IBKR TWS is unreachable. Free with Schwab account.
    # Setup: python broker/schwab.py --auth   (one-time browser login)
    USE_SCHWAB_FALLBACK: bool = os.getenv("USE_SCHWAB_FALLBACK", "false").lower() == "true"
    SCHWAB_APP_KEY: str       = os.getenv("SCHWAB_APP_KEY", "")
    SCHWAB_APP_SECRET: str    = os.getenv("SCHWAB_APP_SECRET", "")
    SCHWAB_TOKEN_PATH: str    = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")
    SCHWAB_CALLBACK_URL: str  = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")

    # ── Telegram ─────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Output ───────────────────────────────────────────────────────────────
    EXCEL_PATH: str = os.getenv(
        "EXCEL_PATH",
        str(Path(__file__).parent / "output" / "signals.xlsx")
    )

    # ── Feed configuration ───────────────────────────────────────────────────
    FEED_OPTIONS_FLOW:  bool = os.getenv("FEED_OPTIONS_FLOW",  "true").lower() == "true"
    FEED_DARK_POOL:     bool = os.getenv("FEED_DARK_POOL",     "true").lower() == "true"
    FEED_CONGRESS:      bool = os.getenv("FEED_CONGRESS",      "true").lower() == "true"
    FEED_INSIDER:       bool = os.getenv("FEED_INSIDER",       "true").lower() == "true"
    FEED_INSTITUTIONAL: bool = os.getenv("FEED_INSTITUTIONAL", "true").lower() == "true"
    FEED_LIT_FLOW:      bool = os.getenv("FEED_LIT_FLOW",      "true").lower() == "true"
    FEED_MIN_SCORE:     int  = int(os.getenv("FEED_MIN_SCORE", "40"))

    # ── Watchlist mode ───────────────────────────────────────────────────────
    # DYNAMIC: auto-builds from UW flow alerts daily (recommended)
    # STATIC:  uses WATCHLIST from .env
    WATCHLIST_MODE: str  = os.getenv("WATCHLIST_MODE", "dynamic").lower()
    WATCHLIST_SIZE: int  = int(os.getenv("WATCHLIST_SIZE", "25"))
    WATCHLIST_MIN_PREMIUM: float = float(os.getenv("WATCHLIST_MIN_PREMIUM", "50000"))
    MIN_ALERT_PREMIUM:     float = float(os.getenv("MIN_ALERT_PREMIUM", "500000"))  # $500k total premium threshold
    # Anchor symbols always included regardless of mode
    WATCHLIST_ANCHORS: list = os.getenv(
        "WATCHLIST_ANCHORS", "SPY,QQQ,IWM,NVDA,AAPL"
    ).upper().split(",")

    # ── Static watchlist (used when WATCHLIST_MODE=static) ───────────────────
    WATCHLIST: list = os.getenv(
        "WATCHLIST",
        "SPY,QQQ,NVDA,AAPL,MSFT,TSLA,AMZN,META,GOOGL,AMD,MU,AVGO"
    ).upper().split(",")

    # ── Scoring thresholds ───────────────────────────────────────────────────
    MIN_SCORE_ALERT: int = int(os.getenv("MIN_SCORE_ALERT", "60"))
    MIN_SCORE_LOG: int   = int(os.getenv("MIN_SCORE_LOG", "50"))

    # ── Risk / sizing ────────────────────────────────────────────────────────
    PORTFOLIO_SIZE: float   = float(os.getenv("PORTFOLIO_SIZE", "100000"))
    MAX_RISK_PCT: float     = float(os.getenv("MAX_RISK_PCT", "0.01"))      # 1% per trade
    TARGET_GAIN_PCT: float  = float(os.getenv("TARGET_GAIN_PCT", "0.65"))   # +65% target exit
    TARGET_DELTA_MIN: float = float(os.getenv("TARGET_DELTA_MIN", "0.20"))  # wide range covers OTM flow
    TARGET_DELTA_MAX: float = float(os.getenv("TARGET_DELTA_MAX", "0.90"))
    MIN_DTE: int            = int(os.getenv("MIN_DTE", "0"))                # include near-term flow
    MAX_DTE: int            = int(os.getenv("MAX_DTE", "60"))               # include LEAP-ish flow

    # ── GEX regime block ─────────────────────────────────────────────────────
    # Suppress bullish CALL signals when SPY GEX < threshold ($M)
    GEX_BLOCK_THRESHOLD: float = float(os.getenv("GEX_BLOCK_THRESHOLD", "-500"))

    # ── Scan loop ────────────────────────────────────────────────────────────
    SCAN_INTERVAL_SEC: int = int(os.getenv("SCAN_INTERVAL_SEC", "180"))  # 3 min


cfg = Config()
