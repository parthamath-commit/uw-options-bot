"""
schwab_data.py
==============

Schwab Market Data API integration for the Barchart Pro Bot.

Public function:
    get_intraday_bars(ticker, period="1d", interval="1m") -> pd.DataFrame

Returns the same DataFrame shape as yfinance.Ticker(...).history():
columns Open, High, Low, Close, Volume, indexed by timestamp.

Behavior depends on CONFIG["intraday_data_source"]:
    "schwab" -> Schwab only, no fallback. Errors propagate.
    "yahoo"  -> Yahoo only, ignore Schwab. Same as the old behavior.
    "auto"   -> Try Schwab first; on any failure, silently fall back to Yahoo.

Token lifecycle:
    - Access tokens expire every 30 minutes; refreshed automatically.
    - Refresh tokens last 7 days; if expired, the bot logs a warning and
      falls back to Yahoo. You then re-run `--setup-schwab` to renew.

Concurrency:
    - This module assumes single-threaded use (one bot process). The
      schwab-py client is not thread-safe by default.

CLI helper (used by main bot's --setup-schwab flag):
    setup_schwab_oauth() -> performs the one-time browser OAuth handshake.
"""

import os
import time
import json
import threading
from datetime import datetime, timedelta
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import pandas as pd

# Soft import: if schwab-py isn't installed, we degrade to Yahoo-only and
# log a warning at first use rather than crashing on import.
try:
    from schwab.auth import client_from_token_file, client_from_login_flow
    from schwab.client import Client
    SCHWAB_AVAILABLE = True
except ImportError:
    SCHWAB_AVAILABLE = False

import yfinance as yf


# ============================================================
# v17.4.1 — HANG GUARDS
# ============================================================
# Three fixes for the hang-during-scan problem:
#   1. Per-call timeout: every Schwab HTTP call is wrapped in a thread
#      with a hard timeout, so a hung response can't block the whole bot.
#   2. Circuit breaker: after N consecutive Schwab failures in one scan
#      cycle, stop calling Schwab and fall through to Yahoo for the rest
#      of the cycle. The breaker resets when the cycle completes.
#   3. Progress logging: every Schwab call is counted and logged so you
#      can see the bot is making progress instead of guessing.

# Hard per-request timeout, in seconds. Schwab usually responds in <1s;
# anything taking >10s is almost certainly hung.
_SCHWAB_CALL_TIMEOUT_SECONDS = float(os.getenv("SCHWAB_CALL_TIMEOUT_SECONDS", "10"))

# Circuit breaker: after this many consecutive failures, stop calling Schwab.
_CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("SCHWAB_CIRCUIT_BREAKER_THRESHOLD", "5"))

# Circuit breaker state. Tripped state persists for this many seconds before
# allowing a retry. Conservative — we'd rather under-call Schwab than hang.
_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(os.getenv("SCHWAB_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "600"))
_CIRCUIT_BREAKER = {
    "tripped": False,
    "tripped_at": 0.0,
    "consecutive_failures_in_cycle": 0,
    "calls_this_cycle": 0,
    "successes_this_cycle": 0,
}
_CIRCUIT_LOCK = threading.Lock()

# Background executor for timing out individual Schwab calls. Reuses workers
# rather than spinning up a new thread per call.
_SCHWAB_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="schwab-call")


def _circuit_breaker_is_tripped():
    """Returns True if the breaker is tripped and still within cooldown."""
    with _CIRCUIT_LOCK:
        if not _CIRCUIT_BREAKER["tripped"]:
            return False
        elapsed = time.time() - _CIRCUIT_BREAKER["tripped_at"]
        if elapsed >= _CIRCUIT_BREAKER_COOLDOWN_SECONDS:
            # Reset and let the next call try again.
            _CIRCUIT_BREAKER["tripped"] = False
            _CIRCUIT_BREAKER["consecutive_failures_in_cycle"] = 0
            print(f"[schwab] Circuit breaker reset after {elapsed:.0f}s cooldown")
            return False
        return True


def _circuit_breaker_record_success():
    with _CIRCUIT_LOCK:
        _CIRCUIT_BREAKER["consecutive_failures_in_cycle"] = 0
        _CIRCUIT_BREAKER["successes_this_cycle"] += 1
        _CIRCUIT_BREAKER["calls_this_cycle"] += 1


def _circuit_breaker_record_failure(reason=""):
    with _CIRCUIT_LOCK:
        _CIRCUIT_BREAKER["consecutive_failures_in_cycle"] += 1
        _CIRCUIT_BREAKER["calls_this_cycle"] += 1
        if (_CIRCUIT_BREAKER["consecutive_failures_in_cycle"] >= _CIRCUIT_BREAKER_THRESHOLD
                and not _CIRCUIT_BREAKER["tripped"]):
            _CIRCUIT_BREAKER["tripped"] = True
            _CIRCUIT_BREAKER["tripped_at"] = time.time()
            print(f"[schwab] CIRCUIT BREAKER TRIPPED after "
                  f"{_CIRCUIT_BREAKER['consecutive_failures_in_cycle']} consecutive failures. "
                  f"Falling back to Yahoo for {_CIRCUIT_BREAKER_COOLDOWN_SECONDS}s.")
            print(f"[schwab] Last failure reason: {reason[:120]}")


def reset_circuit_breaker_cycle_counters():
    """Called at the start of each scan cycle by the bot to reset counters."""
    with _CIRCUIT_LOCK:
        _CIRCUIT_BREAKER["calls_this_cycle"] = 0
        _CIRCUIT_BREAKER["successes_this_cycle"] = 0
        # Don't reset consecutive_failures — that crosses cycle boundaries
        # intentionally so a half-broken state doesn't reset mid-failure.


def _run_with_timeout(callable_fn, timeout_seconds, *args, **kwargs):
    """
    Runs callable_fn(*args, **kwargs) with a hard timeout. If the call doesn't
    return within `timeout_seconds`, raises TimeoutError. The underlying call
    keeps running in the background thread (we can't safely kill it), but
    its result will be discarded.
    """
    future = _SCHWAB_EXECUTOR.submit(callable_fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        # Note: we can't actually cancel the underlying HTTP call; it will
        # eventually finish in the background. But the caller gets unblocked.
        raise TimeoutError(f"Schwab call exceeded {timeout_seconds}s timeout")


# ============================================================
# Module state
# ============================================================

_SCHWAB_CLIENT = None
_CLIENT_LOCK = threading.Lock()

# Rate limiter: rolling window of timestamps. Schwab market-data quota is
# typically 120 requests/minute. We aim for 100 to leave headroom for the
# trading endpoints if we ever add them.
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 100
_REQUEST_TIMESTAMPS = deque(maxlen=_RATE_LIMIT_MAX_REQUESTS)
_RATE_LOCK = threading.Lock()

# Soft cache: ticker -> (fetched_at_ts, period, interval, dataframe)
# Avoid repeating the same Schwab call for the same (ticker, period, interval)
# within a short window. Useful when the bot's scoring pass touches the same
# ticker multiple times in one cycle.
_BAR_CACHE = {}
_BAR_CACHE_TTL_SECONDS = 30


# ============================================================
# Configuration helpers
# ============================================================

def _get_setting(name, default=None):
    """Read setting from the bot's CONFIG if available, else from env."""
    try:
        # Late import: barchart_pro_bot defines CONFIG. Avoid circular import.
        import barchart_pro_bot as bot
        if hasattr(bot, "CONFIG") and name in bot.CONFIG:
            return bot.CONFIG[name]
    except Exception:
        pass
    return os.getenv(name.upper(), default)


def _intraday_data_source():
    raw = (_get_setting("intraday_data_source", "auto") or "auto").lower().strip()
    if raw not in ("schwab", "yahoo", "auto"):
        return "auto"
    return raw


def _schwab_app_key():
    return _get_setting("schwab_app_key", "") or os.getenv("SCHWAB_APP_KEY", "")


def _schwab_app_secret():
    return _get_setting("schwab_app_secret", "") or os.getenv("SCHWAB_APP_SECRET", "")


def _schwab_callback_url():
    return _get_setting("schwab_callback_url", "https://127.0.0.1:8182") or "https://127.0.0.1:8182"


def _schwab_token_path():
    return _get_setting("schwab_token_path", "") or os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")


# ============================================================
# Rate limiter
# ============================================================

def _rate_limit_acquire():
    """Block until a request slot is available within our rolling window."""
    while True:
        with _RATE_LOCK:
            now = time.time()
            cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
            while _REQUEST_TIMESTAMPS and _REQUEST_TIMESTAMPS[0] < cutoff:
                _REQUEST_TIMESTAMPS.popleft()
            if len(_REQUEST_TIMESTAMPS) < _RATE_LIMIT_MAX_REQUESTS:
                _REQUEST_TIMESTAMPS.append(now)
                return
            sleep_for = (_REQUEST_TIMESTAMPS[0] + _RATE_LIMIT_WINDOW_SECONDS) - now
        if sleep_for > 0:
            time.sleep(min(sleep_for, 1.0))


# ============================================================
# Schwab client lifecycle
# ============================================================

def _get_schwab_client():
    """
    Returns a cached schwab-py client, loading it from the token file if it
    hasn't been loaded yet. Returns None if the library isn't installed,
    credentials are missing, or the token file doesn't exist.
    """
    global _SCHWAB_CLIENT

    if not SCHWAB_AVAILABLE:
        return None

    with _CLIENT_LOCK:
        if _SCHWAB_CLIENT is not None:
            return _SCHWAB_CLIENT

        app_key = _schwab_app_key()
        app_secret = _schwab_app_secret()
        token_path = _schwab_token_path()

        if not app_key or not app_secret:
            return None

        if not os.path.exists(token_path):
            print(f"[schwab] Token file not found at {token_path}. "
                  f"Run: python barchart_pro_bot.py --setup-schwab")
            return None

        try:
            _SCHWAB_CLIENT = client_from_token_file(
                token_path=token_path,
                api_key=app_key,
                app_secret=app_secret,
            )
            print(f"[schwab] Client loaded from {token_path}")
            return _SCHWAB_CLIENT
        except Exception as e:
            print(f"[schwab] Failed to load client: {e}")
            return None


# ============================================================
# Period/interval translation
# ============================================================

# Maps yfinance period strings (e.g. "1d", "5d") to Schwab's price_history
# parameters. Schwab uses period_type + period + frequency_type + frequency.
_PERIOD_MAP = {
    "1d":  {"period_type": "day",   "period": 1,  "freq_default": "minute"},
    "2d":  {"period_type": "day",   "period": 2,  "freq_default": "minute"},
    "3d":  {"period_type": "day",   "period": 3,  "freq_default": "minute"},
    "5d":  {"period_type": "day",   "period": 5,  "freq_default": "minute"},
    "10d": {"period_type": "day",   "period": 10, "freq_default": "minute"},
    "1mo": {"period_type": "month", "period": 1,  "freq_default": "daily"},
    "3mo": {"period_type": "month", "period": 3,  "freq_default": "daily"},
    "6mo": {"period_type": "month", "period": 6,  "freq_default": "daily"},
    "1y":  {"period_type": "year",  "period": 1,  "freq_default": "daily"},
    "2y":  {"period_type": "year",  "period": 2,  "freq_default": "daily"},
}

# Yahoo interval -> Schwab (frequency_type, frequency)
_INTERVAL_MAP = {
    "1m":  ("minute",  1),
    "5m":  ("minute",  5),
    "15m": ("minute", 15),
    "30m": ("minute", 30),
    "1h":  ("minute", 30),  # Schwab's max minute frequency is 30; degrade gracefully
    "1d":  ("daily",   1),
    "1wk": ("weekly",  1),
    "1mo": ("monthly", 1),
}


def _build_schwab_params(period, interval):
    """Translate yfinance-style (period, interval) into Schwab params."""
    period = (period or "1d").lower()
    interval = (interval or "1m").lower()

    period_info = _PERIOD_MAP.get(period, _PERIOD_MAP["1d"])
    freq_type, freq = _INTERVAL_MAP.get(interval, _INTERVAL_MAP["1m"])

    return {
        "period_type": period_info["period_type"],
        "period": period_info["period"],
        "frequency_type": freq_type,
        "frequency": freq,
    }


# ============================================================
# Schwab fetch
# ============================================================

def _fetch_schwab(ticker, period, interval):
    """
    Fetch intraday bars from Schwab. Returns a DataFrame with the same shape
    as yfinance, or raises on error.
    """
    client = _get_schwab_client()
    if client is None:
        raise RuntimeError("Schwab client not available")

    params = _build_schwab_params(period, interval)

    # schwab-py uses enums for period_type and frequency_type. Map our strings.
    pt_map = {
        "day":   Client.PriceHistory.PeriodType.DAY,
        "month": Client.PriceHistory.PeriodType.MONTH,
        "year":  Client.PriceHistory.PeriodType.YEAR,
    }
    ft_map = {
        "minute":  Client.PriceHistory.FrequencyType.MINUTE,
        "daily":   Client.PriceHistory.FrequencyType.DAILY,
        "weekly":  Client.PriceHistory.FrequencyType.WEEKLY,
        "monthly": Client.PriceHistory.FrequencyType.MONTHLY,
    }
    p_map = {
        ("day", 1):    Client.PriceHistory.Period.ONE_DAY,
        ("day", 2):    Client.PriceHistory.Period.TWO_DAYS,
        ("day", 3):    Client.PriceHistory.Period.THREE_DAYS,
        ("day", 5):    Client.PriceHistory.Period.FIVE_DAYS,
        ("day", 10):   Client.PriceHistory.Period.TEN_DAYS,
        ("month", 1):  Client.PriceHistory.Period.ONE_MONTH,
        ("month", 3):  Client.PriceHistory.Period.THREE_MONTHS,
        ("month", 6):  Client.PriceHistory.Period.SIX_MONTHS,
        ("year", 1):   Client.PriceHistory.Period.ONE_YEAR,
        ("year", 2):   Client.PriceHistory.Period.TWO_YEARS,
    }
    f_map = {
        ("minute", 1):   Client.PriceHistory.Frequency.EVERY_MINUTE,
        ("minute", 5):   Client.PriceHistory.Frequency.EVERY_FIVE_MINUTES,
        ("minute", 10):  Client.PriceHistory.Frequency.EVERY_TEN_MINUTES,
        ("minute", 15):  Client.PriceHistory.Frequency.EVERY_FIFTEEN_MINUTES,
        ("minute", 30):  Client.PriceHistory.Frequency.EVERY_THIRTY_MINUTES,
        ("daily", 1):    Client.PriceHistory.Frequency.DAILY,
        ("weekly", 1):   Client.PriceHistory.Frequency.WEEKLY,
        ("monthly", 1):  Client.PriceHistory.Frequency.MONTHLY,
    }

    period_type_enum = pt_map.get(params["period_type"], Client.PriceHistory.PeriodType.DAY)
    freq_type_enum = ft_map.get(params["frequency_type"], Client.PriceHistory.FrequencyType.MINUTE)
    period_enum = p_map.get((params["period_type"], params["period"]),
                            Client.PriceHistory.Period.ONE_DAY)
    freq_enum = f_map.get((params["frequency_type"], params["frequency"]),
                          Client.PriceHistory.Frequency.EVERY_MINUTE)

    _rate_limit_acquire()

    # v17.4.1: check circuit breaker before making the call.
    if _circuit_breaker_is_tripped():
        raise RuntimeError("Schwab circuit breaker tripped — using Yahoo fallback")

    try:
        # v17.4.1: hard timeout so a hung Schwab response can't freeze the bot.
        response = _run_with_timeout(
            client.get_price_history,
            _SCHWAB_CALL_TIMEOUT_SECONDS,
            ticker,
            period_type=period_type_enum,
            period=period_enum,
            frequency_type=freq_type_enum,
            frequency=freq_enum,
            need_extended_hours_data=False,
        )
    except TimeoutError as e:
        _circuit_breaker_record_failure(str(e))
        raise RuntimeError(f"Schwab timeout for {ticker}: {e}")
    except Exception as e:
        _circuit_breaker_record_failure(str(e))
        raise

    if response.status_code != 200:
        _circuit_breaker_record_failure(f"status {response.status_code}")
        raise RuntimeError(f"Schwab returned status {response.status_code}: {response.text[:200]}")

    data = response.json()
    candles = data.get("candles", [])
    if not candles:
        raise RuntimeError(f"Schwab returned no candles for {ticker}")

    df = pd.DataFrame(candles)
    # Schwab columns: open, high, low, close, volume, datetime (epoch ms)
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
    df = df.set_index("datetime")
    df = df.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    })
    # Drop any extra columns Schwab might add later, keep canonical 5.
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    _circuit_breaker_record_success()
    return df


# ============================================================
# Yahoo fetch (fallback)
# ============================================================

def _fetch_yahoo(ticker, period, interval):
    """Fallback path. Same as the old code in get_enhanced_price_reaction."""
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo returned empty data for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ============================================================
# v17.4 — LIVE OPTION QUOTE FETCH (chase-context for alerts)
# ============================================================

# Cache option quotes briefly so re-fetching the same contract within seconds
# doesn't double-spend the rate limit.
_OPTION_QUOTE_CACHE = {}
_OPTION_QUOTE_CACHE_TTL_SECONDS = 15


def _build_occ_symbol(ticker, expiry, option_type, strike):
    """
    Build an OCC option symbol string in the format Schwab's API requires:

        TICKER YYMMDDCXXXXXXXX

    Where:
      - TICKER is the underlying symbol (no padding)
      - A SINGLE space separates ticker from date
      - YYMMDD is the expiration date (e.g. 251106 for Nov 6 2025)
      - C or P is call/put
      - 8-digit strike in thousandths, zero-padded

    Examples confirmed against working schwab-py user code:
      SPY 251106C00674000   (SPY $674 call, Nov 6 2025)
      RDDT 260116C00050000  (RDDT $50 call, Jan 16 2026)

    v17.4.4: corrected after two wrong guesses (no-space, then 6-char padded).
    The actual format Schwab accepts is a single space between ticker and
    date, with no other padding. Verified against working examples in the
    schwab-py ecosystem.

    Returns None if we can't build it (bad inputs).
    """
    if not ticker or not expiry or not option_type or strike is None:
        return None
    try:
        expiry_str = str(expiry).strip()

        # Strip ISO datetime suffix: everything from 'T' onward.
        if "T" in expiry_str:
            expiry_str = expiry_str.split("T")[0]
        if " " in expiry_str:
            expiry_str = expiry_str.split(" ")[0]

        exp_dt = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y"):
            try:
                exp_dt = datetime.strptime(expiry_str, fmt)
                break
            except ValueError:
                continue
        if exp_dt is None:
            return None

        yymmdd = exp_dt.strftime("%y%m%d")
        cp = "C" if "call" in str(option_type).lower() else "P"
        strike_int = int(round(float(strike) * 1000))
        strike_str = f"{strike_int:08d}"

        ticker_clean = ticker.upper().strip()
        return f"{ticker_clean} {yymmdd}{cp}{strike_str}"
    except Exception:
        return None


def _parse_expiry_to_date(expiry):
    """
    Parse various expiry string formats into a datetime.date object.
    Returns None if parsing fails.
    """
    if expiry is None:
        return None
    try:
        if isinstance(expiry, date):
            # Already a date or datetime
            return expiry if not isinstance(expiry, datetime) else expiry.date()
    except Exception:
        pass
    expiry_str = str(expiry).strip()
    if not expiry_str or expiry_str == "0":
        return None

    # Strip ISO datetime suffix.
    if "T" in expiry_str:
        expiry_str = expiry_str.split("T")[0]
    if " " in expiry_str:
        expiry_str = expiry_str.split(" ")[0]

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(expiry_str, fmt).date()
        except ValueError:
            continue
    return None


def get_option_quote(ticker, expiry, option_type, strike):
    """
    Fetch live option bid/ask/mid/last/Greeks for a specific contract from Schwab.

    v17.4.5: rewritten to use get_option_chain() instead of get_quote().
    Earlier versions tried to construct an OCC symbol and pass it to
    get_quote(), which always returned 404 because get_quote is a stock-only
    endpoint. The correct approach is to fetch the option chain filtered to
    the specific strike+expiry+type, then read bid/ask/etc. directly from
    Schwab's response. As a bonus, we also get Greeks, IV, OI, and volume
    for free since the chain response includes them.

    Returns dict:
      - source: "schwab" | "unavailable"
      - bid, ask, mid, last, mark: float or None
      - bid_size, ask_size: int or None
      - spread, spread_pct: float or None
      - delta, gamma, theta, vega, rho: float or None  (v17.4.5 bonus)
      - implied_volatility: float or None  (v17.4.5 bonus)
      - open_interest, volume: int or None  (v17.4.5 bonus)
      - quote_time_iso: str or None
      - underlying_price: float or None
      - schwab_symbol: str or None  (Schwab's canonical symbol for the contract)
      - error: str or None
    """
    # Parse and validate inputs.
    expiry_date = _parse_expiry_to_date(expiry)
    if expiry_date is None:
        return {"source": "unavailable", "error": f"could not parse expiry: {expiry!r}"}

    if not ticker or strike is None:
        return {"source": "unavailable", "error": "missing ticker or strike"}

    try:
        strike_float = float(strike)
    except (TypeError, ValueError):
        return {"source": "unavailable", "error": f"could not parse strike: {strike!r}"}

    # Cache key: ticker + strike + expiry + option_type
    is_call = "call" in str(option_type).lower()
    cache_key = (ticker.upper().strip(), expiry_date.isoformat(), "C" if is_call else "P", strike_float)
    now_ts = time.time()
    cached = _OPTION_QUOTE_CACHE.get(cache_key)
    if cached and (now_ts - cached[0]) < _OPTION_QUOTE_CACHE_TTL_SECONDS:
        return dict(cached[1])

    # Circuit breaker check.
    if _circuit_breaker_is_tripped():
        return {"source": "unavailable", "error": "schwab circuit breaker tripped"}

    client = _get_schwab_client()
    if client is None:
        return {"source": "unavailable", "error": "schwab client not available"}

    try:
        _rate_limit_acquire()

        # Schwab-py requires typed arguments: ContractType enum + date objects.
        ct_enum = client.Options.ContractType.CALL if is_call else client.Options.ContractType.PUT
        response = _run_with_timeout(
            client.get_option_chain,
            _SCHWAB_CALL_TIMEOUT_SECONDS,
            ticker.upper().strip(),
            contract_type=ct_enum,
            strike=strike_float,
            from_date=expiry_date,
            to_date=expiry_date,
        )

        if response.status_code != 200:
            _circuit_breaker_record_failure(f"chain status {response.status_code}")
            return {"source": "unavailable",
                    "error": f"schwab status {response.status_code}: {response.text[:200]}"}

        data = response.json()
        if not isinstance(data, dict):
            _circuit_breaker_record_failure("chain non-dict response")
            return {"source": "unavailable", "error": f"unexpected response: {str(data)[:200]}"}

        if data.get("status") != "SUCCESS" or data.get("numberOfContracts", 0) == 0:
            # Empty chain — contract doesn't exist for these filters
            _circuit_breaker_record_success()  # API call was healthy, just no match
            return {"source": "unavailable",
                    "error": f"no contract for {ticker} {expiry_date} {ct_enum.value} {strike_float}"}

        # Navigate the chain. Structure:
        #   data["callExpDateMap"|"putExpDateMap"][date_key][strike_key][0] = contract
        # where date_key looks like "2026-05-15:3" (date + DTE)
        # and strike_key is the strike formatted as a string ("735.0")
        chain_key = "callExpDateMap" if is_call else "putExpDateMap"
        chain_map = data.get(chain_key, {})
        if not chain_map:
            _circuit_breaker_record_success()
            return {"source": "unavailable", "error": f"empty {chain_key} in response"}

        # Find the date key that matches our expiry (the date portion before ':').
        target_date_str = expiry_date.isoformat()
        matching_date_key = None
        for k in chain_map.keys():
            if k.startswith(target_date_str):
                matching_date_key = k
                break
        if matching_date_key is None:
            _circuit_breaker_record_success()
            return {"source": "unavailable",
                    "error": f"no expiry {target_date_str} in chain (got: {list(chain_map.keys())[:3]})"}

        # Find the strike key. Schwab uses "735.0" format.
        strikes_for_date = chain_map[matching_date_key]
        matching_strike_key = None
        # First try exact match on common formats
        for candidate in (f"{strike_float}", f"{strike_float:.1f}", f"{strike_float:.2f}",
                          f"{int(strike_float)}.0" if strike_float == int(strike_float) else None):
            if candidate and candidate in strikes_for_date:
                matching_strike_key = candidate
                break
        # Fall back to float comparison
        if matching_strike_key is None:
            for k in strikes_for_date.keys():
                try:
                    if abs(float(k) - strike_float) < 0.001:
                        matching_strike_key = k
                        break
                except (TypeError, ValueError):
                    continue
        if matching_strike_key is None:
            _circuit_breaker_record_success()
            return {"source": "unavailable",
                    "error": f"no strike {strike_float} in chain (got: {list(strikes_for_date.keys())[:3]})"}

        contracts = strikes_for_date[matching_strike_key]
        if not contracts:
            _circuit_breaker_record_success()
            return {"source": "unavailable", "error": "empty contracts array"}

        contract = contracts[0]

        # Extract pricing.
        bid = contract.get("bid")
        ask = contract.get("ask")
        last = contract.get("last")
        mark = contract.get("mark")  # Schwab's own mid estimate
        bid_size = contract.get("bidSize")
        ask_size = contract.get("askSize")

        # Compute mid from bid/ask. Fall back to Schwab's "mark" if either is missing.
        mid = None
        spread = None
        spread_pct = None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = round((bid + ask) / 2, 4)
            spread = round(ask - bid, 4)
            if mid > 0:
                spread_pct = round((spread / mid) * 100, 2)
        elif mark is not None and mark > 0:
            mid = round(mark, 4)

        # Quote time — Schwab gives "quoteTimeInLong" in ms epoch.
        quote_time_ms = contract.get("quoteTimeInLong") or contract.get("tradeTimeInLong")

        result = {
            "source": "schwab",
            "schwab_symbol": contract.get("symbol"),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": last,
            "mark": mark,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "spread": spread,
            "spread_pct": spread_pct,
            # v17.4.5 bonus fields from the chain response:
            "delta": contract.get("delta"),
            "gamma": contract.get("gamma"),
            "theta": contract.get("theta"),
            "vega": contract.get("vega"),
            "rho": contract.get("rho"),
            "implied_volatility": contract.get("volatility"),
            "open_interest": contract.get("openInterest"),
            "volume": contract.get("totalVolume"),
            "underlying_price": data.get("underlyingPrice"),
            "quote_time_iso": (
                datetime.fromtimestamp(quote_time_ms / 1000).isoformat()
                if quote_time_ms else None
            ),
            "error": None,
        }
        _OPTION_QUOTE_CACHE[cache_key] = (now_ts, dict(result))
        _circuit_breaker_record_success()
        return result

    except TimeoutError as e:
        _circuit_breaker_record_failure(f"chain timeout: {e}")
        return {"source": "unavailable", "error": f"timeout: {e}"}
    except Exception as e:
        _circuit_breaker_record_failure(f"chain error: {e}")
        return {"source": "unavailable", "error": str(e)[:200]}


def compute_chase_context(alert_entry, quote):
    """
    Given the bot's original entry price and a live quote dict from
    get_option_quote(), return a chase-context dict that the message
    builder can render. All fields are None if quote source != schwab.
    """
    if not quote or quote.get("source") != "schwab":
        return {
            "available": False,
            "reason": quote.get("error", "no live quote") if quote else "no quote",
        }

    mid = quote.get("mid")
    ask = quote.get("ask")
    bid = quote.get("bid")

    if not alert_entry or alert_entry <= 0 or not mid or mid <= 0:
        return {
            "available": False,
            "reason": f"alert_entry={alert_entry}, mid={mid} — chase % undefined",
        }

    chase_pct = ((mid - alert_entry) / alert_entry) * 100

    # Recompute stop-loss math anchored at the CURRENT mid rather than the
    # stale alert entry. This shows the trader the real risk if they enter now.
    return {
        "available": True,
        "alert_entry": round(alert_entry, 2),
        "current_bid": bid,
        "current_ask": ask,
        "current_mid": mid,
        "spread": quote.get("spread"),
        "spread_pct": quote.get("spread_pct"),
        "chase_pct": round(chase_pct, 1),
        "quote_time_iso": quote.get("quote_time_iso"),
        "underlying_price": quote.get("underlying_price"),
    }


# ============================================================
# Public API
# ============================================================

def get_intraday_bars(ticker, period="1d", interval="1m"):
    """
    Returns a DataFrame of OHLCV bars for `ticker`, with the same shape as
    yfinance.Ticker.history(). Source is determined by CONFIG.

    Caches results for 30 seconds per (ticker, period, interval) tuple.
    Raises on hard failure when source="schwab"; returns Yahoo result when
    source="auto" and Schwab fails.
    """
    cache_key = (ticker.upper(), period, interval)
    now = time.time()
    cached = _BAR_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _BAR_CACHE_TTL_SECONDS:
        return cached[1].copy()  # defensive copy: callers may mutate

    source = _intraday_data_source()
    df = None
    last_error = None

    # v17.4.1: periodic progress logging so user sees scan progress
    # instead of guessing whether the bot is hung.
    with _CIRCUIT_LOCK:
        call_num = _CIRCUIT_BREAKER["calls_this_cycle"]
    if call_num > 0 and call_num % 10 == 0:
        print(f"[schwab] progress: {call_num} calls this cycle "
              f"({_CIRCUIT_BREAKER['successes_this_cycle']} succeeded)")

    if source in ("schwab", "auto"):
        try:
            df = _fetch_schwab(ticker, period, interval)
            _record_schwab_success()  # track for consecutive-failure detector
        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            is_auth_error = (
                "token_invalid" in error_msg
                or "unauthorized" in error_msg
                or "401" in error_msg
                or "invalid_grant" in error_msg
            )
            _record_schwab_failure(is_auth_error=is_auth_error, error=str(e))
            if source == "schwab":
                raise
            # auto mode: fall through to Yahoo silently with a debug log
            print(f"[schwab] {ticker} fetch failed, falling back to Yahoo: {e}")

    if df is None and source in ("yahoo", "auto"):
        df = _fetch_yahoo(ticker, period, interval)

    if df is None:
        raise RuntimeError(f"All data sources failed for {ticker}: {last_error}")

    _BAR_CACHE[cache_key] = (now, df.copy())
    return df


# ============================================================
# v17.3 — TOKEN HEALTH MONITORING
# ============================================================
# Tracks:
#   - Token file expiry timestamps (proactive warning before expiry)
#   - Consecutive Schwab fetch failures (catches password changes,
#     server outages, anything else that breaks tokens silently)
# Sends Telegram alerts via a callback registered by the main bot.

# Failure tracking state
_FAILURE_STATE = {
    "consecutive_failures": 0,
    "consecutive_auth_failures": 0,
    "last_success_time": None,
    "last_failure_time": None,
    "last_failure_error": "",
    "last_alert_time": 0,  # rate-limit alerts to one per cooldown window
}

# How often to send the "Schwab is down" alert when failures persist
_FAILURE_ALERT_COOLDOWN_SECONDS = 30 * 60  # 30 minutes

# Token-expiry warning state — track which warning levels we've already sent
# so we don't spam (1 alert at 24h, 1 alert at 4h, 1 alert when expired)
_TOKEN_WARNINGS_SENT = {
    "24h_warning_sent_for_expiry": None,
    "4h_warning_sent_for_expiry": None,
    "expired_alert_sent_for_expiry": None,
}

# Callback that the bot registers — called with (subject, body) when an
# alert should be sent. Kept abstract so this module doesn't depend on the
# bot's Telegram code directly.
_ALERT_CALLBACK = None


def register_alert_callback(callback):
    """
    Register a function to be called when token-health alerts fire.
    The callback signature is: callback(subject: str, body: str).
    The main bot calls this once at startup with its Telegram-send wrapper.
    """
    global _ALERT_CALLBACK
    _ALERT_CALLBACK = callback


def _send_alert(subject, body):
    """Emit an alert through the registered callback, or fall back to print."""
    full_msg = f"⚠️ {subject}\n\n{body}"
    print(f"[schwab-monitor] {subject}: {body}")
    if _ALERT_CALLBACK is not None:
        try:
            _ALERT_CALLBACK(subject, full_msg)
        except Exception as e:
            print(f"[schwab-monitor] Alert callback failed: {e}")


def _record_schwab_success():
    """Called from get_intraday_bars after a successful Schwab fetch."""
    _FAILURE_STATE["consecutive_failures"] = 0
    _FAILURE_STATE["consecutive_auth_failures"] = 0
    _FAILURE_STATE["last_success_time"] = time.time()


def _record_schwab_failure(is_auth_error=False, error=""):
    """Called from get_intraday_bars after a failed Schwab fetch."""
    _FAILURE_STATE["consecutive_failures"] += 1
    if is_auth_error:
        _FAILURE_STATE["consecutive_auth_failures"] += 1
    _FAILURE_STATE["last_failure_time"] = time.time()
    _FAILURE_STATE["last_failure_error"] = error[:200]

    # Threshold-based alerts.
    # Auth errors are urgent — re-auth is the only fix. Alert after 10 consecutive.
    # Generic errors might be a Schwab outage — alert after 25 consecutive
    # (gives Schwab time to recover before bothering the user).
    auth_threshold = 10
    generic_threshold = 25

    now = time.time()
    cooldown_ok = (now - _FAILURE_STATE["last_alert_time"]) >= _FAILURE_ALERT_COOLDOWN_SECONDS

    if cooldown_ok and _FAILURE_STATE["consecutive_auth_failures"] >= auth_threshold:
        _send_alert(
            "Schwab authentication failure",
            (f"Schwab token appears invalid. {_FAILURE_STATE['consecutive_auth_failures']} "
             f"consecutive auth failures.\n\n"
             f"Bot has fallen back to Yahoo (delayed) data. To restore real-time:\n"
             f"  1. Open Command Prompt\n"
             f"  2. cd C:\\BarchartBot\n"
             f"  3. python setup_schwab_manual.py\n"
             f"  4. Restart the bot (Ctrl+C then python barchart_pro_bot.py)\n\n"
             f"Last error: {_FAILURE_STATE['last_failure_error']}")
        )
        _FAILURE_STATE["last_alert_time"] = now
    elif cooldown_ok and _FAILURE_STATE["consecutive_failures"] >= generic_threshold:
        _send_alert(
            "Schwab data fetch repeatedly failing",
            (f"{_FAILURE_STATE['consecutive_failures']} consecutive Schwab fetch failures.\n\n"
             f"This might be a Schwab API outage. Bot is on Yahoo fallback.\n"
             f"Check status: https://status.schwab.com\n\n"
             f"Last error: {_FAILURE_STATE['last_failure_error']}")
        )
        _FAILURE_STATE["last_alert_time"] = now


def get_token_expiry_info():
    """
    Read the token file and return expiry info. Returns dict:
      - exists: bool
      - access_expires_in_seconds: int or None
      - refresh_expires_in_seconds: int or None  (Schwab refresh tokens
        last 7 days from issue; if the token file has a creation time, we
        can estimate this)
      - access_expires_at_iso: str or None
      - refresh_expires_at_iso: str or None
      - error: str or None
    """
    token_path = _schwab_token_path()
    if not token_path or not os.path.exists(token_path):
        return {"exists": False, "error": "token file not found"}

    try:
        with open(token_path, "r") as f:
            token_data = json.load(f)
    except Exception as e:
        return {"exists": True, "error": f"failed to parse token file: {e}"}

    now_ts = time.time()
    info = {"exists": True, "error": None}

    # schwab-py stores expires_at as either a unix timestamp or as a
    # 'creation_timestamp' + 'token' dict where the token has expires_in.
    # We try several known shapes.
    token = token_data.get("token", token_data)
    creation_ts = token_data.get("creation_timestamp")
    expires_in = token.get("expires_in")
    expires_at = token.get("expires_at") or token_data.get("expires_at")

    # Access token expiry
    if expires_at:
        info["access_expires_at_iso"] = datetime.fromtimestamp(expires_at).isoformat()
        info["access_expires_in_seconds"] = int(expires_at - now_ts)
    elif creation_ts and expires_in:
        access_expires_at = creation_ts + expires_in
        info["access_expires_at_iso"] = datetime.fromtimestamp(access_expires_at).isoformat()
        info["access_expires_in_seconds"] = int(access_expires_at - now_ts)
    else:
        info["access_expires_at_iso"] = None
        info["access_expires_in_seconds"] = None

    # Refresh token expiry — Schwab gives 7 days from issue time.
    # The "issue time" is the creation_timestamp on the token file
    # (which is set when we did the OAuth handshake).
    refresh_lifetime_seconds = 7 * 24 * 60 * 60
    if creation_ts:
        refresh_expires_at = creation_ts + refresh_lifetime_seconds
        info["refresh_expires_at_iso"] = datetime.fromtimestamp(refresh_expires_at).isoformat()
        info["refresh_expires_in_seconds"] = int(refresh_expires_at - now_ts)
        info["creation_timestamp"] = creation_ts
    else:
        info["refresh_expires_at_iso"] = None
        info["refresh_expires_in_seconds"] = None

    return info


def check_token_expiry_and_alert():
    """
    Called once per scan cycle. Checks the token file expiry and fires
    Telegram alerts at three thresholds:
      - 24h before refresh-token expiry: heads-up
      - 4h before: more urgent
      - after expiry: critical (data has degraded to Yahoo)

    Each alert is sent at most once per token-expiry cycle (tracked by the
    refresh_expires_at_iso value, so a fresh OAuth resets the warnings).
    """
    # On the VM, schwab_token_keeper.py owns expiry alerts + Telegram renewal.
    if os.getenv("SCHWAB_TOKEN_ALERTS", "true").lower() != "true":
        return

    info = get_token_expiry_info()
    if not info.get("exists"):
        return  # no token file — already handled at startup

    if info.get("error"):
        return  # parse error already logged in get_token_expiry_info

    refresh_expires_in = info.get("refresh_expires_in_seconds")
    refresh_expires_at_iso = info.get("refresh_expires_at_iso")

    if refresh_expires_in is None or refresh_expires_at_iso is None:
        return  # no creation timestamp; can't compute

    # Already-expired alert (highest priority)
    if refresh_expires_in <= 0:
        if _TOKEN_WARNINGS_SENT["expired_alert_sent_for_expiry"] != refresh_expires_at_iso:
            _send_alert(
                "Schwab token EXPIRED",
                (f"Refresh token expired {abs(refresh_expires_in) // 3600:.0f} hours ago.\n"
                 f"Bot is using Yahoo fallback (15-minute delayed data).\n\n"
                 f"To restore real-time data:\n"
                 f"  1. cd C:\\BarchartBot\n"
                 f"  2. python setup_schwab_manual.py\n"
                 f"  3. Restart the bot")
            )
            _TOKEN_WARNINGS_SENT["expired_alert_sent_for_expiry"] = refresh_expires_at_iso
        return

    # 4-hour urgent warning
    if refresh_expires_in <= 4 * 3600:
        if _TOKEN_WARNINGS_SENT["4h_warning_sent_for_expiry"] != refresh_expires_at_iso:
            hours_left = refresh_expires_in / 3600
            _send_alert(
                "Schwab token expiring URGENT",
                (f"Refresh token expires in {hours_left:.1f} hours "
                 f"(at {refresh_expires_at_iso}).\n\n"
                 f"Please re-auth before it expires:\n"
                 f"  1. cd C:\\BarchartBot\n"
                 f"  2. python setup_schwab_manual.py\n"
                 f"  3. Restart the bot\n\n"
                 f"If you don't, the bot will fall back to delayed Yahoo data.")
            )
            _TOKEN_WARNINGS_SENT["4h_warning_sent_for_expiry"] = refresh_expires_at_iso
        return

    # 24-hour advance warning
    if refresh_expires_in <= 24 * 3600:
        if _TOKEN_WARNINGS_SENT["24h_warning_sent_for_expiry"] != refresh_expires_at_iso:
            hours_left = refresh_expires_in / 3600
            _send_alert(
                "Schwab token expiring in 24h",
                (f"Refresh token expires in {hours_left:.0f} hours "
                 f"(at {refresh_expires_at_iso}).\n\n"
                 f"Plan to re-auth in the next day:\n"
                 f"  1. cd C:\\BarchartBot\n"
                 f"  2. python setup_schwab_manual.py\n"
                 f"  3. Restart the bot")
            )
            _TOKEN_WARNINGS_SENT["24h_warning_sent_for_expiry"] = refresh_expires_at_iso
        return


def get_monitoring_status():
    """Returns a dict with the current monitoring state for debugging."""
    expiry_info = get_token_expiry_info()
    return {
        "consecutive_failures": _FAILURE_STATE["consecutive_failures"],
        "consecutive_auth_failures": _FAILURE_STATE["consecutive_auth_failures"],
        "last_failure_error": _FAILURE_STATE["last_failure_error"],
        "alert_callback_registered": _ALERT_CALLBACK is not None,
        "token_expiry": expiry_info,
    }


def refresh_schwab_smart():
    """
    Smart re-auth helper.
    - If access token is valid (expires_in > 5 minutes): does nothing.
    - If refresh token is valid: lets schwab-py handle silent refresh on
      next API call; does not open browser.
    - If refresh token is expired or near-expired: runs full OAuth flow.

    Called by --refresh-schwab CLI flag.
    """
    info = get_token_expiry_info()

    if not info.get("exists"):
        print("[schwab] No token file. Running full OAuth setup...")
        return setup_schwab_oauth()

    if info.get("error"):
        print(f"[schwab] Token file unreadable ({info['error']}). Running full OAuth setup...")
        return setup_schwab_oauth()

    access_expires_in = info.get("access_expires_in_seconds")
    refresh_expires_in = info.get("refresh_expires_in_seconds")

    if access_expires_in is not None and access_expires_in > 5 * 60:
        print(f"[schwab] Access token valid for {access_expires_in / 60:.1f} more minutes. "
              f"No action needed.")
        print(f"[schwab] Refresh token expires in "
              f"{(refresh_expires_in or 0) / 3600:.1f} hours.")
        return True

    if refresh_expires_in is not None and refresh_expires_in > 60 * 60:
        # Refresh token still valid for >1 hour — silent refresh will happen
        # on next API call. We don't need to do anything manually.
        print(f"[schwab] Refresh token still valid "
              f"({refresh_expires_in / 3600:.1f} hours remaining).")
        print(f"[schwab] Access token will auto-refresh on next API call.")
        print(f"[schwab] No browser action needed.")
        return True

    print(f"[schwab] Refresh token expired or expiring soon "
          f"({(refresh_expires_in or 0) / 3600:.1f} hours).")
    print(f"[schwab] Running full OAuth flow...")
    return setup_schwab_oauth()


# ============================================================
# OAuth setup (run once via --setup-schwab)
# ============================================================

def setup_schwab_oauth():
    """
    Performs the one-time browser-based OAuth handshake. Saves the token
    file to SCHWAB_TOKEN_PATH for subsequent runs to load.

    This will:
      1. Open your default browser
      2. Send you to Schwab's login page
      3. After you log in and approve the app, Schwab redirects to your
         callback URL (https://127.0.0.1:8182)
      4. The handshake captures the redirect and saves tokens
    """
    if not SCHWAB_AVAILABLE:
        print("ERROR: schwab-py is not installed. Run: pip install schwab-py")
        return False

    app_key = _schwab_app_key()
    app_secret = _schwab_app_secret()
    callback_url = _schwab_callback_url()
    token_path = _schwab_token_path()

    if not app_key or not app_secret:
        print("ERROR: SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env")
        return False

    print(f"[schwab] Starting OAuth handshake...")
    print(f"[schwab] Token will be saved to: {token_path}")
    print(f"[schwab] Callback URL: {callback_url}")
    print(f"[schwab] A browser window will open. Log in with your SCHWAB BROKERAGE")
    print(f"[schwab] account credentials (NOT your developer portal login).")

    # Ensure the directory for the token file exists.
    token_dir = os.path.dirname(token_path)
    if token_dir and not os.path.exists(token_dir):
        os.makedirs(token_dir, exist_ok=True)

    try:
        client = client_from_login_flow(
            api_key=app_key,
            app_secret=app_secret,
            callback_url=callback_url,
            token_path=token_path,
        )
        print(f"[schwab] OAuth complete. Token saved to {token_path}")

        # v17.3: reset monitoring state so warnings start fresh for the new
        # token's 7-day cycle. Also reset the cached client and failure
        # counters so the bot picks up the new token immediately.
        global _SCHWAB_CLIENT
        _SCHWAB_CLIENT = None
        _TOKEN_WARNINGS_SENT["24h_warning_sent_for_expiry"] = None
        _TOKEN_WARNINGS_SENT["4h_warning_sent_for_expiry"] = None
        _TOKEN_WARNINGS_SENT["expired_alert_sent_for_expiry"] = None
        _FAILURE_STATE["consecutive_failures"] = 0
        _FAILURE_STATE["consecutive_auth_failures"] = 0
        _FAILURE_STATE["last_alert_time"] = 0

        # Quick sanity test: fetch one bar to confirm the client works.
        print(f"[schwab] Testing with SPY 1m bars...")
        df = _fetch_schwab("SPY", "1d", "1m")
        print(f"[schwab] Test successful. Got {len(df)} bars. Latest:")
        print(df.tail(3))
        return True
    except Exception as e:
        print(f"[schwab] OAuth setup failed: {e}")
        return False


# ============================================================
# Health check (used by status reporting)
# ============================================================

def schwab_health_check():
    """Returns dict with status info — useful for diagnostic logging."""
    return {
        "library_installed": SCHWAB_AVAILABLE,
        "credentials_present": bool(_schwab_app_key() and _schwab_app_secret()),
        "token_file_exists": os.path.exists(_schwab_token_path()) if _schwab_token_path() else False,
        "token_path": _schwab_token_path(),
        "data_source": _intraday_data_source(),
        "cache_entries": len(_BAR_CACHE),
        "rate_limit_used": len(_REQUEST_TIMESTAMPS),
    }
