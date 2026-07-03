"""
outcome_tracker.py -- Nightly outcome tracker for UW Options Bot v2.0

For every scored signal (any score), fetches Schwab data to determine
whether the signal's direction (bullish/bearish) was correct and whether
the target (+65% premium) was hit.

TWO METHODS per signal:
  1. Option premium lookup (get_option_chain) -- exact contract P&L
  2. Underlying price move fallback -- if option no longer has a quote

Checks at 1-day, 3-day, and 5-day horizons after the signal date.
Writes outcome, exit_premium, pnl_usd back to scored_signals.

RUN NIGHTLY (after market close):
    python outcome_tracker.py

Or check a specific date range:
    python outcome_tracker.py --from 2026-06-01 --to 2026-06-15

Or re-check already-labeled signals (re-evaluate):
    python outcome_tracker.py --redo
"""
import os
import sys
import time
import json
import sqlite3
import argparse
import logging
from pathlib import Path
from datetime import date, datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] OutcomeTracker - %(message)s"
)
log = logging.getLogger("outcome_tracker")

ROOT         = Path(__file__).parent
DB_PATH      = ROOT / "output" / "uw_bot.db"
TOKEN_PATH   = os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json")
APP_KEY      = os.getenv("SCHWAB_APP_KEY", "")
APP_SECRET   = os.getenv("SCHWAB_APP_SECRET", "")

# Horizons to check (days after signal date)
HORIZONS = [1, 3, 5]

# Target thresholds
TARGET_GAIN_PCT  = float(os.getenv("TARGET_GAIN_PCT", "0.65"))   # +65% = win
STOP_LOSS_PCT    = float(os.getenv("MAX_RISK_PCT", "0.01")) * 10  # -10% of premium = stop


def get_schwab_client():
    """Load Schwab client from saved token."""
    import importlib.util as ilu
    spec = ilu.find_spec("schwab")
    ldr  = ilu.spec_from_file_location("_schwab_real", spec.origin)
    mod  = ilu.module_from_spec(ldr)
    ldr.loader.exec_module(mod)
    client = mod.auth.client_from_token_file(
        token_path=TOKEN_PATH,
        api_key=APP_KEY,
        app_secret=APP_SECRET,
    )
    log.info("Schwab client ready")
    return client, mod


def get_underlying_price(client, symbol, target_date: date) -> float | None:
    """
    Get the closing price of the underlying stock on or near target_date
    using Schwab daily price history.
    """
    try:
        start = target_date - timedelta(days=3)
        end   = target_date + timedelta(days=1)
        resp  = client.get_price_history_every_day(
            symbol,
            start_datetime=datetime.combine(start, datetime.min.time()),
            end_datetime=datetime.combine(end, datetime.min.time()),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        candles = data.get("candles", [])
        if not candles:
            return None
        # Find the candle closest to but not after target_date
        best = None
        for c in candles:
            ts = datetime.fromtimestamp(c["datetime"] / 1000).date()
            if ts <= target_date:
                best = c
        return float(best["close"]) if best else None
    except Exception as e:
        log.debug("Underlying price error {}: {}".format(symbol, e))
        return None


def get_option_premium(client, schwab_mod, symbol, expiry_str, strike, right,
                       target_date: date) -> float | None:
    """
    Get the mid-price of a specific option contract on or near target_date.
    Returns None if expired or no quote found.
    """
    try:
        exp_date = date.fromisoformat(expiry_str)
        if target_date > exp_date:
            return None  # expired

        from schwab.client import Client as _SC
        contract_type = (
            _SC.Options.ContractType.CALL if right == "C"
            else _SC.Options.ContractType.PUT
        )
        resp = client.get_option_chain(
            symbol=symbol,
            contract_type=contract_type,
            strike=float(strike),
            from_date=exp_date,
            to_date=exp_date,
            include_underlying_quote=False,
        )
        if resp.status_code != 200:
            return None

        chain  = resp.json()
        side   = "callExpDateMap" if right == "C" else "putExpDateMap"
        expmap = chain.get(side, {})

        for date_key, strikes in expmap.items():
            for strike_key, contracts in strikes.items():
                for c in contracts:
                    bid = c.get("bid", 0) or 0
                    ask = c.get("ask", 0) or 0
                    if ask > 0:
                        return (bid + ask) / 2.0
        return None
    except Exception as e:
        log.debug("Option premium error {} {}{}: {}".format(symbol, strike, right, e))
        return None


def classify_outcome(entry_premium, exit_premium, entry_price,
                     exit_price, intent, has_option_quote) -> tuple:
    """
    Returns (outcome, pnl_usd, pnl_pct, method) where:
      outcome = 'win' | 'loss' | 'open' | 'expired'
      pnl_pct = percentage change in premium or underlying move
    """
    if has_option_quote and entry_premium and entry_premium > 0 and exit_premium:
        # Real option P&L (per contract = 100 shares)
        pnl_pct = (exit_premium - entry_premium) / entry_premium
        pnl_usd = (exit_premium - entry_premium) * 100
        method  = "option_premium"
    elif entry_price and exit_price:
        # Underlying directional move as proxy
        raw_move = (exit_price - entry_price) / entry_price
        pnl_pct  = raw_move if intent == "bullish" else -raw_move
        pnl_usd  = None   # can't calculate dollar P&L without position size
        method   = "underlying_move"
    else:
        return "open", None, None, "no_data"

    if pnl_pct >= TARGET_GAIN_PCT:
        outcome = "win"
    elif pnl_pct <= -STOP_LOSS_PCT:
        outcome = "loss"
    else:
        outcome = "open"

    return outcome, pnl_usd, round(pnl_pct * 100, 2), method


def run(from_date=None, to_date=None, redo=False):
    if not DB_PATH.exists():
        log.error("DB not found: {}".format(DB_PATH))
        sys.exit(1)

    client, schwab_mod = get_schwab_client()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Build query for signals to evaluate
    where_clauses = []
    params = []

    if not redo:
        where_clauses.append("(outcome IS NULL OR outcome = 'open')")

    if from_date:
        where_clauses.append("date(scored_at) >= ?")
        params.append(from_date)
    if to_date:
        where_clauses.append("date(scored_at) <= ?")
        params.append(to_date)

    # Only evaluate signals old enough to have a 1-day outcome
    where_clauses.append("date(scored_at) <= date('now', 'localtime', '-1 day')")

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    signals = conn.execute("""
        SELECT id, symbol, strike, right, expiry, intent,
               composite_score, premium, scored_at
        FROM scored_signals
        {}
        ORDER BY scored_at DESC
        LIMIT 5000
    """.format(where), params).fetchall()
    # NOTE: No score filter here -- we intentionally track ALL scored signals
    # (including score 40-59) so performance_check.py can compare win rates
    # across every score tier and tell us if MIN_SCORE_ALERT=60 is optimal.
    # Telegram alerts are separately controlled by MIN_SCORE_ALERT (now 60).

    log.info("Evaluating {} signals...".format(len(signals)))

    updated = 0
    wins = losses = opens = no_data = 0

    today = date.today()

    for sig in signals:
        sig_date = date.fromisoformat(sig["scored_at"][:10])
        entry_premium = sig["premium"]
        symbol  = sig["symbol"]
        strike  = sig["strike"]
        right   = sig["right"]
        expiry  = sig["expiry"]
        intent  = sig["intent"] or "bullish"
        sig_id  = sig["id"]

        best_outcome  = "open"
        best_pnl_usd  = None
        best_pnl_pct  = None
        best_exit_prem = None
        best_horizon  = None
        best_method   = "no_data"

        for horizon in HORIZONS:
            target_dt = sig_date + timedelta(days=horizon)
            if target_dt > today:
                break  # not enough time has passed yet

            # Try option premium first (requires strike > 0 and valid expiry)
            exit_prem = None
            entry_price = exit_price = None
            has_option_quote = False

            if strike and strike > 0 and expiry and right:
                exit_prem = get_option_premium(
                    client, schwab_mod, symbol, expiry, strike, right, target_dt)
                has_option_quote = exit_prem is not None
                time.sleep(0.3)   # rate limit courtesy

            # Fallback: underlying price move
            if not has_option_quote:
                exit_price   = get_underlying_price(client, symbol, target_dt)
                entry_price  = get_underlying_price(client, symbol, sig_date)
                time.sleep(0.3)

            outcome, pnl_usd, pnl_pct, method = classify_outcome(
                entry_premium, exit_prem, entry_price, exit_price,
                intent, has_option_quote)

            log.info("  {} {}  score={:.0f}  horizon={}d  "
                     "outcome={}  pnl={}%  method={}".format(
                         symbol, expiry or "", sig["composite_score"],
                         horizon, outcome, pnl_pct, method))

            # Store best (most terminal) outcome across horizons
            # win/loss trump open
            if outcome in ("win", "loss") or best_outcome == "open":
                best_outcome   = outcome
                best_pnl_usd   = pnl_usd
                best_pnl_pct   = pnl_pct
                best_exit_prem = exit_prem
                best_horizon   = horizon
                best_method    = method

            if outcome in ("win", "loss"):
                break   # no need to check further horizons

        # Write back to DB
        conn.execute("""
            UPDATE scored_signals
            SET outcome        = ?,
                pnl_usd        = ?,
                exit_premium   = ?,
                exit_date      = ?
            WHERE id = ?
        """, (
            best_outcome,
            best_pnl_usd,
            best_exit_prem,
            (sig_date + timedelta(days=best_horizon or 1)).isoformat()
                if best_horizon else None,
            sig_id,
        ))
        conn.commit()
        updated += 1

        if best_outcome == "win":   wins += 1
        elif best_outcome == "loss": losses += 1
        elif best_outcome == "open": opens += 1
        else: no_data += 1

    conn.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 56)
    print("  OUTCOME TRACKER RESULTS")
    print("=" * 56)
    print("  Signals evaluated : {}".format(updated))
    print("  Wins (≥+65%)      : {}".format(wins))
    print("  Losses (≤-10%)    : {}".format(losses))
    print("  Still open        : {}".format(opens))
    print("  No data           : {}".format(no_data))
    if wins + losses > 0:
        win_rate = wins / (wins + losses) * 100
        print("  Win rate          : {:.1f}%  ({} closed)".format(
            win_rate, wins + losses))
    print("=" * 56)
    print()
    print("  Run performance_check.py to see outcome breakdown by score tier.")
    print()


def main():
    parser = argparse.ArgumentParser(description="UW Options Bot outcome tracker")
    parser.add_argument("--from", dest="from_date", default=None,
                        help="Start date YYYY-MM-DD (default: all unresolved)")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="End date YYYY-MM-DD")
    parser.add_argument("--redo", action="store_true",
                        help="Re-evaluate already-labeled signals")
    args = parser.parse_args()

    run(from_date=args.from_date, to_date=args.to_date, redo=args.redo)


if __name__ == "__main__":
    main()
