import os
import time
import glob
import shutil
import json
import requests
import pandas as pd
import pytz
import yfinance as yf

from datetime import datetime, date, time as dt_time, timedelta
from collections import Counter
from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException

import sys

# VM port: everything lives next to this script (C:\BarchartBot on the laptop,
# /home/ubuntu/uw-options-bot/barchart_bot on the Oracle VM). Override with BOT_HOME.
BOT_HOME = os.getenv("BOT_HOME") or os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BOT_HOME, ".env"))
BOT_HOME = os.getenv("BOT_HOME") or BOT_HOME
IS_LINUX = sys.platform.startswith("linux")
# Headless Chrome on the VM (no screen); visible window on Windows unless overridden.
HEADLESS = os.getenv("HEADLESS", "true" if IS_LINUX else "false").lower() == "true"
# Block ad/tracker hosts + images to keep Chrome small on the 1 GB VM.
BLOCK_AD_HOSTS = os.getenv("BLOCK_AD_HOSTS", "true" if IS_LINUX else "false").lower() == "true"
DISABLE_IMAGES = os.getenv("DISABLE_IMAGES", "true" if IS_LINUX else "false").lower() == "true"

SCRIPT_VERSION = "v17.6.0_vm_headless"

CONFIG = {
    "barchart_unusual_url": "https://www.barchart.com/options/unusual-activity/stocks",
    "barchart_flow_url": "https://www.barchart.com/options/options-flow",
    "barchart_option_chain_url_template": "https://www.barchart.com/stocks/quotes/{ticker}/options",

    "download_folder": os.path.join(BOT_HOME, "downloads"),
    "archive_folder": os.path.join(BOT_HOME, "archive"),
    "image_folder": os.path.join(BOT_HOME, "images"),
    "debug_folder": os.path.join(BOT_HOME, "debug"),
    "chrome_profile_folder": os.path.join(BOT_HOME, "chrome_profile_bot"),
    "excel_file": os.path.join(BOT_HOME, "signals.xlsx"),

    # =========================
    # Performance Dashboard + Auto Learning
    # =========================
    "enable_performance_dashboard": os.getenv("ENABLE_PERFORMANCE_DASHBOARD", "true").lower() == "true",
    "enable_auto_learning": os.getenv("ENABLE_AUTO_LEARNING", "true").lower() == "true",
    "auto_learning_min_trades": int(os.getenv("AUTO_LEARNING_MIN_TRADES", "5")),
    "auto_learning_good_win_rate": float(os.getenv("AUTO_LEARNING_GOOD_WIN_RATE", "60")),
    "auto_learning_bad_win_rate": float(os.getenv("AUTO_LEARNING_BAD_WIN_RATE", "40")),
    "auto_learning_recent_days": int(os.getenv("AUTO_LEARNING_RECENT_DAYS", "30")),
    "send_performance_report_to_telegram": os.getenv("SEND_PERFORMANCE_REPORT_TO_TELEGRAM", "true").lower() == "true",

    # =========================
    # Win-Rate Based Auto Filtering
    # =========================
    # Uses signals.xlsx history to boost proven buckets and downrank weak buckets.
    # This does NOT delete signals; weak setups are normally downgraded to WATCHLIST.
    "enable_win_rate_auto_filtering": os.getenv("ENABLE_WIN_RATE_AUTO_FILTERING", "true").lower() == "true",
    "win_rate_filter_recent_days": int(os.getenv("WIN_RATE_FILTER_RECENT_DAYS", "30")),
    "win_rate_filter_min_trades": int(os.getenv("WIN_RATE_FILTER_MIN_TRADES", "5")),
    "win_rate_filter_good_pct": float(os.getenv("WIN_RATE_FILTER_GOOD_PCT", "60")),
    "win_rate_filter_bad_pct": float(os.getenv("WIN_RATE_FILTER_BAD_PCT", "40")),
    "win_rate_filter_boost_points": int(os.getenv("WIN_RATE_FILTER_BOOST_POINTS", "2")),
    "win_rate_filter_penalty_points": int(os.getenv("WIN_RATE_FILTER_PENALTY_POINTS", "4")),
    "win_rate_filter_force_watchlist": os.getenv("WIN_RATE_FILTER_FORCE_WATCHLIST", "true").lower() == "true",
    "win_rate_filter_block_bad_setup": os.getenv("WIN_RATE_FILTER_BLOCK_BAD_SETUP", "false").lower() == "true",
    "win_rate_filter_include_ticker_bucket": os.getenv("WIN_RATE_FILTER_INCLUDE_TICKER_BUCKET", "false").lower() == "true",
    "win_rate_filter_debug": os.getenv("WIN_RATE_FILTER_DEBUG", "false").lower() == "true",

    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    "telegram_option_chat_id": os.getenv("TELEGRAM_OPTION_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),
    "telegram_investment_chat_id": os.getenv("TELEGRAM_INVESTMENT_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),

    # =========================
    # News Momentum Runner Scanner
    # =========================
    "enable_news_runner_scanner": os.getenv("ENABLE_NEWS_RUNNER_SCANNER", "true").lower() == "true",
    "news_runner_scan_dynamic_tickers": os.getenv("NEWS_RUNNER_SCAN_DYNAMIC_TICKERS", "true").lower() == "true",
    "news_runner_min_price": float(os.getenv("NEWS_RUNNER_MIN_PRICE", "2")),
    "news_runner_max_price": float(os.getenv("NEWS_RUNNER_MAX_PRICE", "20")),
    # v17.5: loosened from 20% to 10% to actually fire on real setups.
    # 20% required gaps so massive that maybe 1-2 stocks per day qualify, often zero.
    "news_runner_min_gap_pct": float(os.getenv("NEWS_RUNNER_MIN_GAP_PCT", "10")),
    # v17.5: loosened from 5x to 3x. 5x relative volume is rare; 3x catches more.
    "news_runner_min_rel_volume": float(os.getenv("NEWS_RUNNER_MIN_REL_VOLUME", "3")),
    # v17.5: loosened from 1M to 500K absolute volume. Small caps often have <1M.
    "news_runner_min_volume": int(os.getenv("NEWS_RUNNER_MIN_VOLUME", "500000")),
    # v17.5: loosened from 20M to 50M float. 20M was extreme micro-float only.
    "news_runner_max_float": int(os.getenv("NEWS_RUNNER_MAX_FLOAT", "50000000")),
    # v17.5: lowered from 80 to 60. The 80 threshold required nearly perfect
    # scoring (massive gap + bullish news + micro float + clean spread all aligned).
    # 60 admits more realistic candidates while still filtering noise.
    "news_runner_min_score": int(os.getenv("NEWS_RUNNER_MIN_SCORE", "60")),
    "news_runner_max_alerts_per_scan": int(os.getenv("NEWS_RUNNER_MAX_ALERTS_PER_SCAN", "3")),
    # v17.5.1: reverted to true. User specifically wants news-driven squeezes
    # only — a gap without news is more likely manipulation than catalyst.
    # The "no news" framing in v17.5 was a misjudgment.
    "news_runner_require_fresh_news": os.getenv("NEWS_RUNNER_REQUIRE_FRESH_NEWS", "true").lower() == "true",
    # v17.5.1: max age (hours) for a news article to count as "fresh".
    # 24 = same trading day's news only. 6 = morning/intraday news only.
    # 0 = disabled, count any-age news as fresh (not recommended).
    "news_runner_max_news_age_hours": float(os.getenv("NEWS_RUNNER_MAX_NEWS_AGE_HOURS", "24")),
    "news_runner_require_vwap_hold": os.getenv("NEWS_RUNNER_REQUIRE_VWAP_HOLD", "false").lower() == "true",
    "news_runner_target_1_pct": float(os.getenv("NEWS_RUNNER_TARGET_1_PCT", "15")),
    "news_runner_target_2_pct": float(os.getenv("NEWS_RUNNER_TARGET_2_PCT", "30")),
    "news_runner_stop_loss_pct": float(os.getenv("NEWS_RUNNER_STOP_LOSS_PCT", "8")),
    "news_runner_spread_warning_pct": float(os.getenv("NEWS_RUNNER_SPREAD_WARNING_PCT", "5")),
    "news_runner_yfinance_news_fallback": os.getenv("NEWS_RUNNER_YFINANCE_NEWS_FALLBACK", "true").lower() == "true",

    "finnhub_api_key": os.getenv("FINNHUB_API_KEY", ""),
    "newsapi_key": os.getenv("NEWSAPI_KEY", ""),
    "alphavantage_api_key": os.getenv("ALPHAVANTAGE_API_KEY", ""),

    "market_timezone": "US/Eastern",

    "market_open_hour": 9,
    "market_open_minute": 30,
    "market_close_hour": 16,
    "market_close_minute": 0,

    "scan_start_hour": 9,
    "scan_start_minute": 30,
    "new_signal_cutoff_hour": 16,
    "new_signal_cutoff_minute": 00,
    "scan_end_hour": 16,
    "scan_end_minute": 0,

    "avoid_lunch_chop": False,
    "lunch_start_hour": 11,
    "lunch_start_minute": 45,
    "lunch_end_hour": 13,
    "lunch_end_minute": 15,
    
    "stock_no_chase_pct": 5,
    "option_no_chase_spread_pct": 25,
    "trap_volume_multiplier": 1.5,
    "daily_report_hour": 16,
    "daily_report_minute": 5,
    
    "late_session_risk_hour": 15,
    "late_session_risk_minute": 30,
    "last_15_minute_block_hour": 15,
    "last_15_minute_block_minute": 45,

    "check_interval_seconds": 600,
    "market_closed_sleep_seconds": 300,
    "outside_scan_window_sleep_seconds": 180,

    "initial_login_wait_seconds": 45,
    "page_load_wait_seconds": 12,
    "page_refresh_wait_seconds": 8,
    "download_wait_timeout_seconds": 80,
    "download_poll_seconds": 2,

    "min_volume": 1000,
    "min_open_interest": 0,
    "min_volume_oi_ratio": 1.5,
    "min_option_price": 0.50,
    "min_dte": int(os.getenv("MIN_DTE", "1")),
    "max_dte": int(os.getenv("MAX_DTE", "90")),
    "min_premium": float(os.getenv("MIN_PREMIUM", "100000")),
    "strong_premium": float(os.getenv("STRONG_PREMIUM", "300000")),
    "high_conviction_premium": float(os.getenv("HIGH_CONVICTION_PREMIUM", "500000")),

    # =========================
    # Consistency / Accuracy Upgrade
    # =========================
    # Goal: fewer alerts, higher quality, stronger repeatability.
    "enable_strict_signal_filtering": os.getenv("ENABLE_STRICT_SIGNAL_FILTERING", "true").lower() == "true",
    "enable_option_signals": os.getenv("ENABLE_OPTION_SIGNALS", "true").lower() == "true",
    "enable_stock_signals": os.getenv("ENABLE_STOCK_SIGNALS", "true").lower() == "true",
    "strict_min_premium": float(os.getenv("STRICT_MIN_PREMIUM", "200000")),
    "strict_option_trade_score": int(os.getenv("STRICT_OPTION_TRADE_SCORE", "12")),
    "strict_option_high_conviction_score": int(os.getenv("STRICT_OPTION_HIGH_CONVICTION_SCORE", "16")),
    "strict_price_reaction_min_score": int(os.getenv("STRICT_PRICE_REACTION_MIN_SCORE", "5")),
    "strict_price_reaction_max_chase_pct": float(os.getenv("STRICT_PRICE_REACTION_MAX_CHASE_PCT", "2.5")),
    "require_market_regime_alignment": os.getenv("REQUIRE_MARKET_REGIME_ALIGNMENT", "true").lower() == "true",
    "allow_neutral_market_regime": os.getenv("ALLOW_NEUTRAL_MARKET_REGIME", "false").lower() == "true",
    "require_multi_layer_confirmation": os.getenv("REQUIRE_MULTI_LAYER_CONFIRMATION", "true").lower() == "true",
    "option_min_confirmations": int(os.getenv("OPTION_MIN_CONFIRMATIONS", "3")),
    "require_structure_confirmation": os.getenv("REQUIRE_STRUCTURE_CONFIRMATION", "true").lower() == "true",
    "enable_option_time_filter": os.getenv("ENABLE_OPTION_TIME_FILTER", "true").lower() == "true",
    "option_morning_start_hour": int(os.getenv("OPTION_MORNING_START_HOUR", "9")),
    "option_morning_start_minute": int(os.getenv("OPTION_MORNING_START_MINUTE", "45")),
    "option_morning_end_hour": int(os.getenv("OPTION_MORNING_END_HOUR", "11")),
    "option_morning_end_minute": int(os.getenv("OPTION_MORNING_END_MINUTE", "0")),
    "option_afternoon_start_hour": int(os.getenv("OPTION_AFTERNOON_START_HOUR", "14")),
    "option_afternoon_start_minute": int(os.getenv("OPTION_AFTERNOON_START_MINUTE", "0")),
    "option_afternoon_end_hour": int(os.getenv("OPTION_AFTERNOON_END_HOUR", "15")),
    "option_afternoon_end_minute": int(os.getenv("OPTION_AFTERNOON_END_MINUTE", "30")),

    # =========================
    # High-Probability Mode
    # =========================
    # Goal: fewer alerts, but stronger setup quality and repeatability.
    "enable_high_probability_mode": os.getenv("ENABLE_HIGH_PROBABILITY_MODE", "true").lower() == "true",
    "high_prob_min_premium": float(os.getenv("HIGH_PROB_MIN_PREMIUM", "300000")),
    "high_prob_min_flow_premium": float(os.getenv("HIGH_PROB_MIN_FLOW_PREMIUM", "200000")),
    "high_prob_min_score": int(os.getenv("HIGH_PROB_MIN_SCORE", "18")),
    "high_prob_min_confirmations": int(os.getenv("HIGH_PROB_MIN_CONFIRMATIONS", "5")),
    "high_prob_min_price_reaction_score": int(os.getenv("HIGH_PROB_MIN_PRICE_REACTION_SCORE", "6")),
    "high_prob_max_chase_pct": float(os.getenv("HIGH_PROB_MAX_CHASE_PCT", "2.0")),
    "high_prob_min_dte": int(os.getenv("HIGH_PROB_MIN_DTE", "7")),
    "high_prob_max_dte": int(os.getenv("HIGH_PROB_MAX_DTE", "45")),
    "high_prob_require_flow": os.getenv("HIGH_PROB_REQUIRE_FLOW", "true").lower() == "true",
    "high_prob_require_aggressive_flow": os.getenv("HIGH_PROB_REQUIRE_AGGRESSIVE_FLOW", "true").lower() == "true",
    "high_prob_require_same_contract_or_cluster": os.getenv("HIGH_PROB_REQUIRE_SAME_CONTRACT_OR_CLUSTER", "true").lower() == "true",
    "high_prob_require_market_alignment": os.getenv("HIGH_PROB_REQUIRE_MARKET_ALIGNMENT", "true").lower() == "true",
    "high_prob_require_price_reaction": os.getenv("HIGH_PROB_REQUIRE_PRICE_REACTION", "true").lower() == "true",
    "high_prob_require_no_chase": os.getenv("HIGH_PROB_REQUIRE_NO_CHASE", "true").lower() == "true",
    "high_prob_auto_min_premium": float(os.getenv("HIGH_PROB_AUTO_MIN_PREMIUM", "1000000")),
    "high_prob_block_earnings_within_days": int(os.getenv("HIGH_PROB_BLOCK_EARNINGS_WITHIN_DAYS", "2")),
    "high_prob_max_option_alerts_per_scan": int(os.getenv("HIGH_PROB_MAX_OPTION_ALERTS_PER_SCAN", "2")),

    # =========================
    # Signal Categorization
    # =========================
    # TRADE = clean execution signal. WATCHLIST = good setup saved for review, not sent unless enabled.
    "enable_signal_categorization": os.getenv("ENABLE_SIGNAL_CATEGORIZATION", "true").lower() == "true",
    "trade_min_score": int(os.getenv("TRADE_MIN_SCORE", "18")),
    "trade_min_confirmations": int(os.getenv("TRADE_MIN_CONFIRMATIONS", "5")),
    "watchlist_min_score": int(os.getenv("WATCHLIST_MIN_SCORE", "12")),
    "watchlist_min_confirmations": int(os.getenv("WATCHLIST_MIN_CONFIRMATIONS", "3")),
    "watchlist_min_premium": float(os.getenv("WATCHLIST_MIN_PREMIUM", "100000")),
    "send_watchlist_to_telegram": os.getenv("SEND_WATCHLIST_TO_TELEGRAM", "false").lower() == "true",
    "max_bid_ask_spread_pct": 30,
    "max_watchlist_alerts_per_scan": int(os.getenv("MAX_WATCHLIST_ALERTS_PER_SCAN", "5")),

    # =========================
    # Professional Telegram Signal Template
    # =========================
    "enable_professional_signal_template": os.getenv("ENABLE_PROFESSIONAL_SIGNAL_TEMPLATE", "true").lower() == "true",
    "include_signal_setup_bullets": os.getenv("INCLUDE_SIGNAL_SETUP_BULLETS", "true").lower() == "true",
    "include_signal_risk_block": os.getenv("INCLUDE_SIGNAL_RISK_BLOCK", "true").lower() == "true",
    "include_signal_disclaimer": os.getenv("INCLUDE_SIGNAL_DISCLAIMER", "true").lower() == "true",
    "signal_brand_name": os.getenv("SIGNAL_BRAND_NAME", "Pal Trading Signals"),
    "signal_disclaimer_short": os.getenv("SIGNAL_DISCLAIMER_SHORT", "Educational purposes only. Not financial advice. Options trading involves high risk."),
    "signal_disclaimer_full": os.getenv(
        "SIGNAL_DISCLAIMER_FULL",
        "This message is for educational and informational purposes only and is "
        "not personalized investment advice. Past performance does not predict "
        "future results. Options involve substantial risk of loss including "
        "total loss of premium paid. You are solely responsible for your own "
        "trading decisions. Consult a licensed financial advisor before acting."
    ),

    # =========================
    # External Flow Confirmation: Unusual Whales
    # =========================
    # This uses an exported Unusual Whales CSV placed in download_folder.
    # It avoids fragile web scraping and gives you a third-party confirmation layer.
    "enable_unusual_whales_confirmation": os.getenv("ENABLE_UNUSUAL_WHALES_CONFIRMATION", "true").lower() == "true",
    "unusual_whales_file_keyword": os.getenv("UNUSUAL_WHALES_FILE_KEYWORD", "unusual_whales").lower(),
    "uw_min_premium": float(os.getenv("UW_MIN_PREMIUM", "100000")),
    "uw_strong_premium": float(os.getenv("UW_STRONG_PREMIUM", "300000")),
    "uw_same_contract_bonus": int(os.getenv("UW_SAME_CONTRACT_BONUS", "5")),
    "uw_same_direction_bonus": int(os.getenv("UW_SAME_DIRECTION_BONUS", "3")),
    "uw_ticker_only_bonus": int(os.getenv("UW_TICKER_ONLY_BONUS", "1")),
    "uw_trade_promotion_bonus": int(os.getenv("UW_TRADE_PROMOTION_BONUS", "3")),
    "uw_confirm_watchlist_promotion": os.getenv("UW_CONFIRM_WATCHLIST_PROMOTION", "true").lower() == "true",
    "uw_expiry_match_required_for_same_contract": os.getenv("UW_EXPIRY_MATCH_REQUIRED_FOR_SAME_CONTRACT", "false").lower() == "true",
    "uw_max_file_age_minutes": int(os.getenv("UW_MAX_FILE_AGE_MINUTES", "720")),
    "high_prob_require_unusual_whales": os.getenv("HIGH_PROB_REQUIRE_UNUSUAL_WHALES", "false").lower() == "true",
    
    "after_hours_end_hour": int(os.getenv("AFTER_HOURS_END_HOUR", "20")),
    "after_hours_end_minute": int(os.getenv("AFTER_HOURS_END_MINUTE", "0")),

    "watchlist_score": 5,
    "trade_score": 8,
    "high_conviction_score": 14,

    "send_only_high_conviction_options": False,
    "max_option_alerts_per_scan": 3,
    "max_stock_alerts_per_scan": 3,
    
    "premarket_start_hour": 8,
    "premarket_start_minute": 0,
    "premarket_end_hour": 9,
    "premarket_end_minute": 30,
    
    "enable_premarket_squeeze_alert": True,
    "premarket_squeeze_min_price": 2,
    "premarket_squeeze_min_volume": 300000,
    "premarket_squeeze_min_gap_pct": 5,
    "premarket_squeeze_top_n": 5,
    
    "option_min_dte": int(os.getenv("OPTION_MIN_DTE", "1")),
    "option_max_dte": int(os.getenv("OPTION_MAX_DTE", "90")),
    "option_sweet_spot_min_dte": 14,
    "option_sweet_spot_max_dte": 30,

    "option_premium_huge": float(os.getenv("OPTION_PREMIUM_HUGE", "1000000")),
    "option_premium_large": float(os.getenv("OPTION_PREMIUM_LARGE", "500000")),
    "option_premium_medium": float(os.getenv("OPTION_PREMIUM_MEDIUM", "100000")),
    "option_premium_small": float(os.getenv("OPTION_PREMIUM_SMALL", "50000")),

"option_volume_very_high": 10000,
"option_volume_high": 3000,
"option_volume_decent": 1000,

"option_vol_oi_aggressive": 3,
"option_vol_oi_strong": 1.5,
"option_vol_oi_ok": 1,

    "option_high_conviction_score": int(os.getenv("OPTION_HIGH_CONVICTION_SCORE", "12")),
    "option_trade_score": int(os.getenv("OPTION_TRADE_SCORE", "8")),
    "option_signal_max_score": int(os.getenv("OPTION_SIGNAL_MAX_SCORE", "45")),

    # Telegram recommendation display only.
    # These are risk guidance values, not guarantees. Keep conservative.
    "option_position_size_low_pct": float(os.getenv("OPTION_POSITION_SIZE_LOW_PCT", "0.25")),
    "option_position_size_medium_pct": float(os.getenv("OPTION_POSITION_SIZE_MEDIUM_PCT", "0.50")),
    "option_position_size_high_pct": float(os.getenv("OPTION_POSITION_SIZE_HIGH_PCT", "1.00")),
    "option_win_prob_floor_pct": int(os.getenv("OPTION_WIN_PROB_FLOOR_PCT", "52")),
    "option_win_prob_ceiling_pct": int(os.getenv("OPTION_WIN_PROB_CEILING_PCT", "72")),

    "option_target_multiplier": float(os.getenv("OPTION_TARGET_MULTIPLIER", "1.30")),
    "option_stop_loss_multiplier": float(os.getenv("OPTION_STOP_LOSS_MULTIPLIER", "0.75")),
    # v17.5: target range for Telegram display. The bot computes a low and a
    # high target; the alert shows them as a range (e.g., "+25% to +40%").
    # Internally the single `target` (option_target_multiplier) is still used
    # for outcome labeling/scoring to keep backward compat.
    "option_target_low_multiplier": float(os.getenv("OPTION_TARGET_LOW_MULTIPLIER", "1.25")),
    "option_target_high_multiplier": float(os.getenv("OPTION_TARGET_HIGH_MULTIPLIER", "1.40")),
    # v17.5: concise alert format (true) vs verbose (false). Concise shows
    # entry, target range, stop only — clean for paid-signal subscribers.
    # Verbose shows full reasoning bullets and setup detail.
    "signal_format_concise": os.getenv("SIGNAL_FORMAT_CONCISE", "true").lower() == "true",
    # v17.5: EOD per-channel reports (option summary to option channel,
    # stock summary to investment channel; weekly stock report on Fridays).
    "enable_eod_channel_reports": os.getenv("ENABLE_EOD_CHANNEL_REPORTS", "true").lower() == "true",

    # Institutional option-flow logic
    "require_flow_confirmation_for_trade": os.getenv("REQUIRE_FLOW_CONFIRMATION_FOR_TRADE", "false").lower() == "true",
    "valid_flow_codes": [x.strip().upper() for x in os.getenv("VALID_FLOW_CODES", "SLAN,TLAT,MLAT,AUTO,TLCT").split(",") if x.strip()],
    "aggressive_flow_codes": [x.strip().upper() for x in os.getenv("AGGRESSIVE_FLOW_CODES", "SLAN,TLAT,MLAT,TLCT").split(",") if x.strip()],
    "min_flow_premium": float(os.getenv("MIN_FLOW_PREMIUM", "50000")),
    "large_flow_premium": float(os.getenv("LARGE_FLOW_PREMIUM", "200000")),
    "institutional_flow_premium": float(os.getenv("INSTITUTIONAL_FLOW_PREMIUM", "1000000")),
    "flow_same_contract_bonus": int(os.getenv("FLOW_SAME_CONTRACT_BONUS", "4")),
    "flow_same_direction_bonus": int(os.getenv("FLOW_SAME_DIRECTION_BONUS", "3")),
    "flow_ticker_only_bonus": int(os.getenv("FLOW_TICKER_ONLY_BONUS", "1")),
    "flow_loop_bonus_cap": int(os.getenv("FLOW_LOOP_BONUS_CAP", "8")),

    # =========================
    # v16: IV PERCENTILE GATING
    # =========================
    "enable_iv_percentile": os.getenv("ENABLE_IV_PERCENTILE", "true").lower() == "true",
    "iv_pct_high_threshold": float(os.getenv("IV_PCT_HIGH_THRESHOLD", "70")),
    "iv_pct_very_high_threshold": float(os.getenv("IV_PCT_VERY_HIGH_THRESHOLD", "85")),
    "iv_pct_low_threshold": float(os.getenv("IV_PCT_LOW_THRESHOLD", "30")),
    "iv_pct_high_penalty": int(os.getenv("IV_PCT_HIGH_PENALTY", "-2")),
    "iv_pct_very_high_penalty": int(os.getenv("IV_PCT_VERY_HIGH_PENALTY", "-4")),
    "iv_pct_low_bonus": int(os.getenv("IV_PCT_LOW_BONUS", "2")),
    "iv_pct_high_bonus_seller": int(os.getenv("IV_PCT_HIGH_BONUS_SELLER", "2")),
    "iv_pct_low_penalty_seller": int(os.getenv("IV_PCT_LOW_PENALTY_SELLER", "-2")),
    # v16.1: don't penalize high IV when a known catalyst (earnings) is near.
    "iv_pct_catalyst_skip_days": int(os.getenv("IV_PCT_CATALYST_SKIP_DAYS", "14")),
    # v16.1: minimum RV samples before percentile is trustworthy. Below this,
    # return neutral instead of a noisy reading from a thin sample.
    "iv_pct_min_samples": int(os.getenv("IV_PCT_MIN_SAMPLES", "90")),

    # v17.x: EMPIRICAL IV CALIBRATION
    # The original theoretical calibration assumed high IV %ile = crush risk
    # for premium buyers. The 21-day signals log (n=515 credible) shows the
    # opposite: IV %ile 75-100 wins 67.9% vs 39.8% for IV %ile 0-25. The
    # measurement is RV-based, so "high IV %ile" really means "high realized
    # vol regime" — which is precisely when directional flow theses pay off.
    # When this flag is on, score_iv_environment() applies the empirically-
    # observed sign instead of the theoretical one. Default OFF so you can
    # validate on your own logs before flipping it.
    "iv_pct_empirical_calibration": os.getenv("IV_PCT_EMPIRICAL_CALIBRATION", "false").lower() == "true",
    "iv_pct_empirical_high_bonus": int(os.getenv("IV_PCT_EMPIRICAL_HIGH_BONUS", "2")),
    "iv_pct_empirical_very_high_bonus": int(os.getenv("IV_PCT_EMPIRICAL_VERY_HIGH_BONUS", "4")),
    "iv_pct_empirical_low_penalty": int(os.getenv("IV_PCT_EMPIRICAL_LOW_PENALTY", "-3")),

    # v17.x: OPTION-PRICE MONITOR
    # When on, monitor_open_option_signals() uses real option bid/ask from
    # Schwab (yfinance fallback) instead of the underlying stock price.
    # Shadow mode keeps the legacy behavior writing to the log and runs the
    # new logic in parallel to a separate audit log (shadow_outcomes.csv).
    # Recommended rollout: shadow for 3-5 trading days, review diffs, then
    # set monitor_use_option_price=True to cut over.
    "monitor_use_option_price": os.getenv("MONITOR_USE_OPTION_PRICE", "false").lower() == "true",
    "monitor_shadow_mode": os.getenv("MONITOR_SHADOW_MODE", "true").lower() == "true",
    "monitor_shadow_csv": os.getenv("MONITOR_SHADOW_CSV", "shadow_outcomes.csv"),
    "monitor_max_spread_pct": float(os.getenv("MONITOR_MAX_SPREAD_PCT", "0.25")),

    # =========================
    # v16: VIX REGIME FILTER
    # =========================
    "enable_vix_regime_filter": os.getenv("ENABLE_VIX_REGIME_FILTER", "true").lower() == "true",
    "vix_calm_max": float(os.getenv("VIX_CALM_MAX", "15")),
    "vix_normal_max": float(os.getenv("VIX_NORMAL_MAX", "20")),
    "vix_elevated_max": float(os.getenv("VIX_ELEVATED_MAX", "28")),
    "vix_calm_score_adj": int(os.getenv("VIX_CALM_SCORE_ADJ", "-1")),
    "vix_elevated_score_adj": int(os.getenv("VIX_ELEVATED_SCORE_ADJ", "2")),
    "vix_panic_score_adj": int(os.getenv("VIX_PANIC_SCORE_ADJ", "4")),
    "vix_spike_change_pct": float(os.getenv("VIX_SPIKE_CHANGE_PCT", "10")),
    "vix_spike_score_adj": int(os.getenv("VIX_SPIKE_SCORE_ADJ", "2")),
    # v16.1: when both VIX is in panic AND IV%ile is very high (>= very_high_threshold),
    # cap the VIX adjustment to this value. Both filters measure the same
    # underlying risk; without the cap, signals get double-penalized.
    "vix_iv_combined_cap": int(os.getenv("VIX_IV_COMBINED_CAP", "3")),

    # =========================
    # v16: LATENCY TRACKING
    # =========================
    "enable_latency_tracking": os.getenv("ENABLE_LATENCY_TRACKING", "true").lower() == "true",
    "latency_warning_seconds": int(os.getenv("LATENCY_WARNING_SECONDS", "60")),

    # =========================
    # v16: EOD OUTCOME LABELER
    # =========================
    "enable_eod_labeler": os.getenv("ENABLE_EOD_LABELER", "true").lower() == "true",

    # =========================
    # v17: SCHWAB INTRADAY DATA INTEGRATION
    # =========================
    # Set INTRADAY_DATA_SOURCE in .env to control data source:
    #   schwab -> Schwab only, no fallback (fail loudly if Schwab is down)
    #   yahoo  -> Yahoo only, ignore Schwab (old behavior, useful for rollback)
    #   auto   -> Try Schwab first, silently fall back to Yahoo on failure
    "intraday_data_source": os.getenv("INTRADAY_DATA_SOURCE", "auto").lower().strip(),

    # Schwab credentials and paths. Don't hard-code these — use .env.
    "schwab_app_key": os.getenv("SCHWAB_APP_KEY", ""),
    "schwab_app_secret": os.getenv("SCHWAB_APP_SECRET", ""),
    "schwab_callback_url": os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"),
    "schwab_token_path": os.getenv("SCHWAB_TOKEN_PATH", "schwab_token.json"),

    # Skip the price-reaction structure check if the most recent bar is older
    # than this many seconds. Stale data scoring is the second-largest source
    # of late/wrong signals after the data source itself.
    "intraday_stale_threshold_seconds": int(os.getenv("INTRADAY_STALE_THRESHOLD_SECONDS", "300")),

    # =========================
    # v17.1: OPPOSITE-LEG / STRUCTURE DETECTOR
    # =========================
    # Detects straddles, strangles, and balanced two-sided trades that
    # masquerade as directional flow. When a candidate signal looks like
    # one leg of a structural trade, demote it to watchlist (or skip it
    # entirely if the structure is unambiguous).
    "enable_opposite_leg_detection": os.getenv("ENABLE_OPPOSITE_LEG_DETECTION", "true").lower() == "true",
    # Below this candidate premium, skip the structure check (small prints
    # are unlikely to be one leg of an institutional structure).
    "opposite_leg_min_premium": float(os.getenv("OPPOSITE_LEG_MIN_PREMIUM", "250000")),
    # Strangle-band: opposite-side strikes within ±X% of candidate strike
    # are treated as plausibly part of the same strangle.
    "opposite_leg_strangle_band_pct": float(os.getenv("OPPOSITE_LEG_STRANGLE_BAND_PCT", "8.0")),
    # Premium-balance threshold: opposite-side total must be at least X%
    # of same-side total to count as "balanced" (and thus structural).
    "opposite_leg_balance_threshold_pct": float(os.getenv("OPPOSITE_LEG_BALANCE_THRESHOLD_PCT", "60")),
    # Score penalty when a STRADDLE or STRANGLE is detected (always demoted).
    "opposite_leg_strict_penalty": int(os.getenv("OPPOSITE_LEG_STRICT_PENALTY", "-6")),
    # Score penalty for generic balanced two-sided activity (softer).
    "opposite_leg_soft_penalty": int(os.getenv("OPPOSITE_LEG_SOFT_PENALTY", "-3")),

    # =========================
    # v17.4: LIVE CHASE CONTEXT
    # =========================
    # When an alert is being sent, fetch the option's live bid/ask from
    # Schwab and show the trader (a) the current mid price, (b) how much
    # the option has moved since the bot's alert entry was computed
    # ("chase %"), and (c) a visual warning if the chase is severe.
    # This addresses the very common case where a Telegram alert arrives
    # showing entry $2.50 but the option is already trading at $3.00.
    "enable_chase_context": os.getenv("ENABLE_CHASE_CONTEXT", "true").lower() == "true",
    # Visual warning when chase >= this percent
    "chase_warn_pct": float(os.getenv("CHASE_WARN_PCT", "15.0")),
    # Strong "skip" recommendation when chase >= this percent
    "chase_block_pct": float(os.getenv("CHASE_BLOCK_PCT", "25.0")),
    "mixed_direction_no_directional_trade": os.getenv("MIXED_DIRECTION_NO_DIRECTIONAL_TRADE", "true").lower() == "true",
    "mixed_direction_ratio": float(os.getenv("MIXED_DIRECTION_RATIO", "0.65")),
    "institutional_directional_min_score": int(os.getenv("INSTITUTIONAL_DIRECTIONAL_MIN_SCORE", "12")),
    "auto_flow_min_premium": float(os.getenv("AUTO_FLOW_MIN_PREMIUM", "500000")),
    "ticker_bias_min_total_premium": float(os.getenv("TICKER_BIAS_MIN_TOTAL_PREMIUM", "100000")),
    "ticker_bias_min_total_volume": float(os.getenv("TICKER_BIAS_MIN_TOTAL_VOLUME", "1000")),
    "prefer_flow_signals_over_unusual": os.getenv("PREFER_FLOW_SIGNALS_OVER_UNUSUAL", "true").lower() == "true",

    # =========================
    # Real-Time / Price-Reaction Institutional Upgrade
    # =========================
    "enable_realtime_price_reaction": os.getenv("ENABLE_REALTIME_PRICE_REACTION", "true").lower() == "true",
    "require_price_reaction_for_high_conviction": os.getenv("REQUIRE_PRICE_REACTION_FOR_HIGH_CONVICTION", "false").lower() == "true",
    "price_reaction_interval": os.getenv("PRICE_REACTION_INTERVAL", "1m"),
    "price_reaction_period": os.getenv("PRICE_REACTION_PERIOD", "1d"),
    "price_reaction_recent_bars": int(os.getenv("PRICE_REACTION_RECENT_BARS", "15")),
    "price_reaction_min_score": int(os.getenv("PRICE_REACTION_MIN_SCORE", "4")),
    "price_reaction_max_chase_pct": float(os.getenv("PRICE_REACTION_MAX_CHASE_PCT", "3.5")),
    "price_reaction_vwap_bonus": int(os.getenv("PRICE_REACTION_VWAP_BONUS", "2")),
    "price_reaction_breakout_bonus": int(os.getenv("PRICE_REACTION_BREAKOUT_BONUS", "2")),
    "price_reaction_volume_bonus": int(os.getenv("PRICE_REACTION_VOLUME_BONUS", "2")),
    "price_reaction_trend_bonus": int(os.getenv("PRICE_REACTION_TREND_BONUS", "1")),
    "price_reaction_penalty": int(os.getenv("PRICE_REACTION_PENALTY", "3")),
    "enable_order_splitting_detection": os.getenv("ENABLE_ORDER_SPLITTING_DETECTION", "true").lower() == "true",
    "split_order_min_count": int(os.getenv("SPLIT_ORDER_MIN_COUNT", "2")),
    "split_order_bonus": int(os.getenv("SPLIT_ORDER_BONUS", "3")),
    "same_ticker_cluster_bonus": int(os.getenv("SAME_TICKER_CLUSTER_BONUS", "2")),
    "realtime_download_refresh_seconds": int(os.getenv("REALTIME_DOWNLOAD_REFRESH_SECONDS", "600")),

    # =========================
    # TradingView-Style Confirmation
    # =========================
    "enable_tradingview_style_confirmation": os.getenv("ENABLE_TRADINGVIEW_STYLE_CONFIRMATION", "true").lower() == "true",
    "tradingview_min_score": int(os.getenv("TRADINGVIEW_MIN_SCORE", "7")),
    "tradingview_high_prob_min_score": int(os.getenv("TRADINGVIEW_HIGH_PROB_MIN_SCORE", "8")),
    "require_tradingview_for_trade": os.getenv("REQUIRE_TRADINGVIEW_FOR_TRADE", "false").lower() == "true",
    "high_prob_require_tradingview_confirmation": os.getenv("HIGH_PROB_REQUIRE_TRADINGVIEW_CONFIRMATION", "true").lower() == "true",
    "tv_ema_fast": int(os.getenv("TV_EMA_FAST", "9")),
    "tv_ema_slow": int(os.getenv("TV_EMA_SLOW", "21")),
    "tv_volume_expansion_multiplier": float(os.getenv("TV_VOLUME_EXPANSION_MULTIPLIER", "1.35")),
    "tv_breakout_lookback_bars": int(os.getenv("TV_BREAKOUT_LOOKBACK_BARS", "30")),
    "tv_structure_tolerance_pct": float(os.getenv("TV_STRUCTURE_TOLERANCE_PCT", "0.25")),
    "tv_min_body_pct": float(os.getenv("TV_MIN_BODY_PCT", "35")),
    "tv_chop_max_range_pct": float(os.getenv("TV_CHOP_MAX_RANGE_PCT", "0.35")),

    # =========================
    # Gamma / Straddle Setup Detection
    # =========================
    "enable_gamma_detection": os.getenv("ENABLE_GAMMA_DETECTION", "true").lower() == "true",
    "gamma_min_dte": int(os.getenv("GAMMA_MIN_DTE", "0")),
    "gamma_max_dte": int(os.getenv("GAMMA_MAX_DTE", "45")),
    "gamma_strike_tolerance_pct": float(os.getenv("GAMMA_STRIKE_TOLERANCE_PCT", "3.0")),
    "gamma_min_call_volume": int(os.getenv("GAMMA_MIN_CALL_VOLUME", "1000")),
    "gamma_min_put_volume": int(os.getenv("GAMMA_MIN_PUT_VOLUME", "1000")),
    "gamma_min_total_volume": int(os.getenv("GAMMA_MIN_TOTAL_VOLUME", "5000")),
    "gamma_min_call_premium": float(os.getenv("GAMMA_MIN_CALL_PREMIUM", "30000")),
    "gamma_min_put_premium": float(os.getenv("GAMMA_MIN_PUT_PREMIUM", "30000")),
    "gamma_min_total_premium": float(os.getenv("GAMMA_MIN_TOTAL_PREMIUM", "100000")),
    "gamma_min_call_vol_oi": float(os.getenv("GAMMA_MIN_CALL_VOL_OI", "1.0")),
    "gamma_min_put_vol_oi": float(os.getenv("GAMMA_MIN_PUT_VOL_OI", "1.0")),
    "gamma_min_score": int(os.getenv("GAMMA_MIN_SCORE", "10")),
    "gamma_max_alerts_per_scan": int(os.getenv("GAMMA_MAX_ALERTS_PER_SCAN", "3")),
    "gamma_target_pct_low": float(os.getenv("GAMMA_TARGET_PCT_LOW", "20")),
    "gamma_target_pct_high": float(os.getenv("GAMMA_TARGET_PCT_HIGH", "40")),
    "gamma_stop_loss_pct": float(os.getenv("GAMMA_STOP_LOSS_PCT", "15")),


    "enable_option_chain_confirmation": True,
    "allow_high_premium_near_wall": True,
    
    "barchart_stock_movers_url": "https://www.barchart.com/stocks/performance/percent-change/advances",

    "enable_dynamic_stock_scanner": True,
    "dynamic_stock_min_price": 2.00,
    "dynamic_stock_min_volume": 500000,
    "dynamic_stock_min_change_pct": 5,
    "max_dynamic_tickers": 75,

    "enable_stock_scanner": True,
    "stock_min_price": 2.00,
    "stock_signal_max_score": int(os.getenv("STOCK_SIGNAL_MAX_SCORE", "33")),
    "weekly_cleanup_enabled": True,
    
    "cleanup_day": 5,  # 5 = Saturday (0=Mon, 6=Sun)
    "cleanup_hour": 18,
    "cleanup_minute": 0,
    "archive_retention_days": 7,  # keep last 7 days (optional)
    
    "enable_trade_monitoring": True,
    "monitor_only_trade_and_high_conviction": True,
    "filter_review_min_trades": 20,
    "filter_good_win_rate": 60,
    "filter_bad_win_rate": 40,


    "stock_mover_columns": {
        "symbol": ["Symbol"],
        "last": ["Last"],
        "change_pct": ["%Chg", "% Change", "Change %"],
        "volume": ["Volume"],
    },

    "unusual_columns": {
        "symbol": ["Symbol", "Ticker", "Underlying"],
        "type": ["Type", "Option Type", "Put/Call"],
        "strike": ["Strike", "Strike Price"],
        "expiration": ["Expiration Date", "Expiration", "Exp Date", "Expires", "Expiry"],
        "volume": ["Volume"],
        "open_interest": ["Open Interest", "Open Int", "OI"],
        "vol_oi": ["Vol/OI", "Vol OI", "Volume/OI"],
        "last": ["Last", "Last Price", "Latest", "Trade"],
        "dte": ["DTE", "Days To Expiration"],
        "premium": ["Premium", "Trade Value", "Value"],
        "sentiment": ["Sentiment"],
        "bid": ["Bid"],
        "ask": ["Ask"],
        "delta": ["Delta"],
        "iv": ["IV", "Imp Vol", "Implied Volatility"],
        "price": ["Price", "Price~", "Underlying Price", "Stock Price"],
    },

    "flow_columns": {
        "symbol": ["Symbol", "Underlying", "Ticker"],
        "price": ["Price", "Price~", "Underlying Price", "Stock Price"],
        "type": ["Type", "Option Type", "Put/Call"],
        "strike": ["Strike", "Strike Price"],
        "expiration": ["Expiration Date", "Expiration", "Exp Date", "Expires", "Expiry"],
        "dte": ["DTE", "Days To Expiration"],
        "bid_x_size": ["Bid x Size", "Bid X Size", "BidXSize"],
        "ask_x_size": ["Ask x Size", "Ask X Size", "AskXSize"],
        "trade": ["Trade", "Last", "Last Price"],
        "size": ["Size", "Trade Size"],
        "premium": ["Premium", "Trade Value", "Value"],
        "volume": ["Volume"],
        "open_interest": ["Open Interest", "Open Int", "OI"],
        "iv": ["IV", "Imp Vol", "Implied Volatility"],
        "delta": ["Delta"],
        "code": ["Code"],
        "side": ["Side", "Trade Side"],
        "sentiment": ["Sentiment"],
    },

    "option_chain_columns": {
        "strike": ["Strike", "Strike Price"],
        "call_oi": ["Call Open Interest", "Calls Open Interest", "Call OI", "Open Interest Call", "Call Open Int"],
        "put_oi": ["Put Open Interest", "Puts Open Interest", "Put OI", "Open Interest Put", "Put Open Int"],
    },
}

GLOBAL_ALERTED_OPTIONS = set()
GLOBAL_ALERTED_STOCKS = set()
GLOBAL_ALERTED_NEWS_RUNNERS = set()
GLOBAL_ALERTED_GAMMA = set()

def is_after_hours():
    now = get_market_now()
    return make_time("market_close_hour", "market_close_minute") < now.time() <= make_time("after_hours_end_hour", "after_hours_end_minute")

def get_yahoo_news_catalyst(ticker):
    """
    Checks Yahoo Finance news for bullish/bearish catalyst keywords.
    Used as confirmation, not as the only reason to trade.
    """

    bullish_keywords = [
        "upgrade", "price target raised", "earnings beat", "raises guidance",
        "partnership", "contract", "fda", "approval", "positive data",
        "acquisition", "merger", "buyout", "ai", "launch", "record revenue"
    ]

    bearish_keywords = [
        "downgrade", "cuts guidance", "misses estimates", "offering",
        "dilution", "lawsuit", "investigation", "bankruptcy", "delisting"
    ]

    try:
        news_items = yf.Ticker(ticker).news or []

        if not news_items:
            return {
                "has_catalyst": False,
                "bullish": False,
                "bearish": False,
                "headline": "",
                "reason": "No Yahoo news found"
            }

        combined_text = ""
        best_headline = ""

        for item in news_items[:5]:
            title = str(item.get("title", ""))
            summary = str(item.get("summary", ""))
            combined_text += " " + title + " " + summary

            if not best_headline and title:
                best_headline = title

        combined_text = combined_text.lower()

        bullish_hits = [kw for kw in bullish_keywords if kw in combined_text]
        bearish_hits = [kw for kw in bearish_keywords if kw in combined_text]

        return {
            "has_catalyst": bool(bullish_hits or bearish_hits),
            "bullish": bool(bullish_hits),
            "bearish": bool(bearish_hits),
            "headline": best_headline,
            "bullish_hits": bullish_hits[:5],
            "bearish_hits": bearish_hits[:5],
            "reason": (
                f"Bullish catalyst: {', '.join(bullish_hits[:5])}"
                if bullish_hits else
                f"Bearish catalyst: {', '.join(bearish_hits[:5])}"
                if bearish_hits else
                "News found but no strong catalyst keyword"
            )
        }

    except Exception as e:
        return {
            "has_catalyst": False,
            "bullish": False,
            "bearish": False,
            "headline": "",
            "reason": f"Yahoo news error: {e}"
        }
        
def get_earnings_risk(ticker):
    """
    Detects near-term earnings risk.
    If earnings are within 2 calendar days, avoid blind option entries.
    """

    try:
        t = yf.Ticker(ticker)
        cal = t.calendar

        earnings_date = None

        if cal is None:
            return {
                "earnings_near": False,
                "earnings_date": None,
                "reason": "Earnings date unavailable"
            }

        if isinstance(cal, dict):
            raw_date = cal.get("Earnings Date")
            if isinstance(raw_date, list) and raw_date:
                earnings_date = raw_date[0]
            else:
                earnings_date = raw_date

        else:
            try:
                raw_date = cal.loc["Earnings Date"][0]
                earnings_date = raw_date
            except Exception:
                earnings_date = None

        if earnings_date is None:
            return {
                "earnings_near": False,
                "earnings_date": None,
                "reason": "Earnings date unavailable"
            }

        earnings_date = pd.to_datetime(earnings_date).date()
        today = get_market_now().date()
        days_to_earnings = (earnings_date - today).days

        earnings_near = 0 <= days_to_earnings <= 2

        return {
            "earnings_near": earnings_near,
            "earnings_date": earnings_date.strftime("%Y-%m-%d"),
            "days_to_earnings": days_to_earnings,
            "reason": (
                f"Earnings within {days_to_earnings} day(s)"
                if earnings_near else
                f"Earnings not near: {days_to_earnings} day(s)"
            )
        }

    except Exception as e:
        return {
            "earnings_near": False,
            "earnings_date": None,
            "reason": f"Earnings check error: {e}"
        }
        
def get_sector_strength_confirmation(ticker, option_type):
    """
    Simple sector/index confirmation.
    Uses QQQ/SPY as broad proxy.
    For tech-heavy names, QQQ matters more.
    """

    try:
        spy = yf.Ticker("SPY").history(period="1d", interval="5m")
        qqq = yf.Ticker("QQQ").history(period="1d", interval="5m")

        if spy.empty or qqq.empty:
            return {
                "confirmed": False,
                "reason": "SPY/QQQ data unavailable"
            }

        def trend(df):
            last = float(df["Close"].iloc[-1])
            first = float(df["Close"].iloc[0])
            return ((last - first) / first) * 100 if first > 0 else 0

        spy_trend = trend(spy)
        qqq_trend = trend(qqq)

        is_call = "call" in str(option_type).lower()
        is_put = "put" in str(option_type).lower()

        if is_call:
            confirmed = spy_trend > 0 and qqq_trend > 0
        elif is_put:
            confirmed = spy_trend < 0 and qqq_trend < 0
        else:
            confirmed = False

        return {
            "confirmed": confirmed,
            "spy_trend": round(spy_trend, 2),
            "qqq_trend": round(qqq_trend, 2),
            "reason": f"SPY trend {round(spy_trend, 2)}%, QQQ trend {round(qqq_trend, 2)}%"
        }

    except Exception as e:
        return {
            "confirmed": False,
            "reason": f"Sector confirmation error: {e}"
        }
        

def get_market_regime():
    try:
        spy = yf.Ticker("SPY").history(period="1d", interval="5m")
        qqq = yf.Ticker("QQQ").history(period="1d", interval="5m")

        if spy.empty or qqq.empty:
            return {"bullish": False, "bearish": False}

        def is_bull(df):
            price = df["Close"].iloc[-1]
            vwap = (df["Close"] * df["Volume"]).sum() / df["Volume"].sum()
            return price > vwap

        def is_bear(df):
            price = df["Close"].iloc[-1]
            vwap = (df["Close"] * df["Volume"]).sum() / df["Volume"].sum()
            return price < vwap

        return {
            "bullish": is_bull(spy) and is_bull(qqq),
            "bearish": is_bear(spy) and is_bear(qqq),
        }

    except:
        return {"bullish": False, "bearish": False}
        
def get_yahoo_price_confirmation(ticker, option_type):
    """
    Uses Yahoo Finance to confirm price action.

    Returns:
    {
        confirmed: bool,
        score_bonus: int,
        price: float,
        vwap: float,
        day_high: float,
        day_low: float,
        reason: str
    }
    """

    try:
        df = yf.Ticker(ticker).history(period="1d", interval="1m")

        # -------------------------------
        # SAFETY CHECKS
        # -------------------------------
        if df is None or df.empty or len(df) < 20:
            return {
                "confirmed": False,
                "score_bonus": 0,
                "price": None,
                "vwap": None,
                "day_high": None,
                "day_low": None,
                "reason": "Insufficient intraday data"
            }

        # Handle MultiIndex if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=["Close"])

        if df.empty:
            return {
                "confirmed": False,
                "score_bonus": 0,
                "price": None,
                "vwap": None,
                "day_high": None,
                "day_low": None,
                "reason": "No valid price data"
            }

        # -------------------------------
        # CORE CALCULATIONS
        # -------------------------------
        price = float(df["Close"].iloc[-1])
        day_high = float(df["High"].max())
        day_low = float(df["Low"].min())

        # Recent breakout levels (last 15 mins)
        recent = df.tail(15)
        recent_high = float(recent["High"].max())
        recent_low = float(recent["Low"].min())

        # VWAP
        volume = df["Volume"].fillna(0)
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3

        if volume.sum() > 0:
            vwap = float((typical_price * volume).sum() / volume.sum())
        else:
            vwap = price

        # Trend check (short-term)
        trend_up = price > df["Close"].iloc[-10]
        trend_down = price < df["Close"].iloc[-10]

        # -------------------------------
        # LOGIC
        # -------------------------------
        is_call = "call" in str(option_type).lower()
        is_put = "put" in str(option_type).lower()

        score = 0
        reasons = []
        vwap_aligned = False
        breakout_aligned = False
        trend_aligned = False

        if is_call:
            if price >= vwap:
                score += 2
                reasons.append("Above VWAP")

            if price >= recent_high * 0.995:
                score += 2
                reasons.append("Breaking recent high")

            if price >= day_high * 0.995:
                score += 2
                reasons.append("Near day high")

            if trend_up:
                score += 1
                reasons.append("Short-term uptrend")

        if is_put:
            if price <= vwap:
                score += 2
                reasons.append("Below VWAP")

            if price <= recent_low * 1.005:
                score += 2
                reasons.append("Breaking recent low")

            if price <= day_low * 1.005:
                score += 2
                reasons.append("Near day low")

            if trend_down:
                score += 1
                reasons.append("Short-term downtrend")

        # -------------------------------
        # FINAL DECISION
        # -------------------------------
        confirmed = score >= 3

        return {
            "confirmed": confirmed,
            "score_bonus": score,
            "price": round(price, 2),
            "vwap": round(vwap, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "reason": ", ".join(reasons) if reasons else "Weak price structure"
        }

    except Exception as e:
        return {
            "confirmed": False,
            "score_bonus": 0,
            "price": None,
            "vwap": None,
            "day_high": None,
            "day_low": None,
            "reason": f"Yahoo error: {e}"
        }
        

def get_stock_confirmation(ticker):
    try:
        df = yf.Ticker(ticker).history(period="1d", interval="1m")

        if df.empty or len(df) < 20:
            return None

        price = df["Close"].iloc[-1]
        high_15 = df["High"].tail(15).max()
        low_15 = df["Low"].tail(15).min()

        vwap = (df["Close"] * df["Volume"]).sum() / df["Volume"].sum()

        vol_recent = df["Volume"].tail(5).mean()
        vol_avg = df["Volume"].mean()

        rel_vol = vol_recent / vol_avg if vol_avg > 0 else 0

        return {
            "price": price,
            "vwap": vwap,
            "breakout_up": price >= high_15 * 0.995,
            "breakout_down": price <= low_15 * 1.005,
            "above_vwap": price > vwap,
            "below_vwap": price < vwap,
            "rel_volume": rel_vol
        }

    except:
        return None
        
def is_spread_ok(bid, ask):
    bid = clean_number(bid)
    ask = clean_number(ask)

    if bid <= 0 or ask <= 0:
        return False

    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100

    return spread_pct <= 20


def is_duplicate_signal(key, signal_type="option"):
    today = get_market_now().strftime("%Y-%m-%d")
    full_key = f"{signal_type.upper()}_{key}_{today}"

    if signal_type == "option":
        if full_key in GLOBAL_ALERTED_OPTIONS:
            return True
        return False

    if signal_type == "stock":
        if full_key in GLOBAL_ALERTED_STOCKS:
            return True
        return False

    if signal_type == "news":
        if full_key in GLOBAL_ALERTED_NEWS_RUNNERS:
            return True
        return False

    if signal_type == "gamma":
        if full_key in GLOBAL_ALERTED_GAMMA:
            return True
        return False

    return False
    
def score_option(row, flow_df, dte):
    score = 0
    reasons = []

    ticker = str(get_unusual_value(row, "symbol")).upper().strip()
    premium = clean_number(get_unusual_value(row, "premium"))
    volume = clean_number(get_unusual_value(row, "volume"))
    oi = clean_number(get_unusual_value(row, "open_interest"))
    option_type = str(get_unusual_value(row, "type")).lower()

    # DTE scoring
    if CONFIG["option_sweet_spot_min_dte"] <= dte <= CONFIG["option_sweet_spot_max_dte"]:
        score += 3
        reasons.append("Institution sweet-spot DTE 14–30")
    elif 3 <= dte < 14:
        score += 1
        reasons.append("Short-term swing/intraday option")
    elif 30 < dte <= 45:
        score += 2
        reasons.append("Swing option DTE")

    # Premium strength
    if premium >= CONFIG["option_premium_huge"]:
        score += 5
        reasons.append("Very large institutional premium")
    elif premium >= CONFIG["option_premium_large"]:
        score += 4
        reasons.append("Huge premium")
    elif premium >= CONFIG["option_premium_medium"]:
        score += 3
        reasons.append("High premium")
    elif premium >= CONFIG["option_premium_small"]:
        score += 2
        reasons.append("Good premium")

    # Volume
    if volume >= CONFIG["option_volume_very_high"]:
        score += 3
        reasons.append("Very high option volume")
    elif volume >= CONFIG["option_volume_high"]:
        score += 2
        reasons.append("High option volume")
    elif volume >= CONFIG["option_volume_decent"]:
        score += 1
        reasons.append("Decent option volume")

    # Volume/OI
    if oi > 0:
        ratio = volume / oi
        if ratio >= CONFIG["option_vol_oi_aggressive"]:
            score += 3
            reasons.append("Aggressive Vol/OI")
        elif ratio >= CONFIG["option_vol_oi_strong"]:
            score += 2
            reasons.append("Strong Vol/OI")
        elif ratio >= CONFIG["option_vol_oi_ok"]:
            score += 1
            reasons.append("Vol/OI acceptable")
    elif volume >= CONFIG["option_volume_decent"]:
        score += 2
        reasons.append("Fresh activity with low/zero OI")

    if "call" in option_type or "put" in option_type:
        score += 1
        reasons.append("Valid option type")

    # Flow confirmation — fixed:
    #   - Old code broke on first ticker match even if direction was wrong.
    #   - Old code double-counted (ticker +2, then call/call +2, etc) on noise rows.
    #   - New code aggregates across rows, requires same direction, and caps the
    #     total flow contribution so a busy ticker can't inflate the score.
    if flow_df is not None and not flow_df.empty:
        flow_bonus = 0
        same_dir_count = 0
        ask_seen = False
        sweep_seen = False

        for _, flow_row in flow_df.iterrows():
            flow_ticker = str(get_flow_value(flow_row, "symbol")).upper().strip()
            if flow_ticker != ticker:
                continue

            flow_type = str(get_flow_value(flow_row, "type")).lower()
            flow_text = " ".join([str(x).lower() for x in flow_row.values])

            same_dir = (
                ("call" in option_type and "call" in flow_type)
                or ("put" in option_type and "put" in flow_type)
            )
            if not same_dir:
                continue

            same_dir_count += 1
            if "ask" in flow_text:
                ask_seen = True
            if "sweep" in flow_text:
                sweep_seen = True

        if same_dir_count > 0:
            flow_bonus += 2
            reasons.append("Flow ticker + direction confirmed")
            if same_dir_count >= 2:
                flow_bonus += 1
                reasons.append(f"Repeated same-direction flow ({same_dir_count} prints)")
            if ask_seen:
                flow_bonus += 1
                reasons.append("Ask-side flow")
            if sweep_seen:
                flow_bonus += 2
                reasons.append("Sweep flow")

            # Hard cap so a single confirmation source cannot dominate the score.
            flow_bonus = min(flow_bonus, 6)
            score += flow_bonus

    return score, reasons
    
def is_premarket():
    now = get_market_now()
    return (
        is_market_day()
        and make_time("premarket_start_hour", "premarket_start_minute")
        <= now.time()
        < make_time("premarket_end_hour", "premarket_end_minute")
    )
    
def get_stock_mover_value(row, field_name):
    return get_first_available_value(row, CONFIG["stock_mover_columns"].get(field_name, []))
    
def download_stock_movers_csv(driver):
    return download_page_csv(driver, CONFIG["barchart_stock_movers_url"], "stock_movers")
    
def get_dynamic_stock_tickers(stock_movers_csv, unusual_csv, flow_df):
    ticker_scores = {}

    def add_score(ticker, points, reason):
        ticker = str(ticker).upper().strip()
        if not ticker or ticker == "NAN":
            return

        if ticker not in ticker_scores:
            ticker_scores[ticker] = {
                "score": 0,
                "reasons": []
            }

        ticker_scores[ticker]["score"] += points
        ticker_scores[ticker]["reasons"].append(reason)

    # =========================
    # 1. Stock movers ranking
    # =========================
    if stock_movers_csv:
        movers_df = read_barchart_csv(stock_movers_csv)

        for _, row in movers_df.iterrows():
            ticker = str(get_stock_mover_value(row, "symbol")).upper()
            price = clean_number(get_stock_mover_value(row, "last"))
            change_pct = clean_number(get_stock_mover_value(row, "change_pct"))
            volume = clean_number(get_stock_mover_value(row, "volume"))

            if ticker and price >= CONFIG["dynamic_stock_min_price"]:
                if volume >= CONFIG["dynamic_stock_min_volume"]:
                    add_score(ticker, 2, "High volume mover")

                if abs(change_pct) >= CONFIG["dynamic_stock_min_change_pct"]:
                    add_score(ticker, 3, "Strong % move")

                if abs(change_pct) >= 10:
                    add_score(ticker, 2, "Very strong % move")

                if volume >= 2_000_000:
                    add_score(ticker, 2, "Institutional-level volume")

    # =========================
    # 2. Unusual options ranking
    # =========================
    if unusual_csv:
        unusual_df = read_barchart_csv(unusual_csv)

        for _, row in unusual_df.iterrows():
            ticker = str(get_unusual_value(row, "symbol")).upper()
            premium = clean_number(get_unusual_value(row, "premium"))
            volume = clean_number(get_unusual_value(row, "volume"))
            oi = clean_number(get_unusual_value(row, "open_interest"))
            vol_oi = volume / oi if oi > 0 else volume

            if not ticker:
                continue

            add_score(ticker, 2, "Unusual options activity")

            if premium >= CONFIG["min_premium"]:
                add_score(ticker, 2, "Options premium >= minimum")

            if premium >= CONFIG["strong_premium"]:
                add_score(ticker, 3, "Strong options premium")

            if premium >= CONFIG["high_conviction_premium"]:
                add_score(ticker, 4, "High-conviction premium")

            if vol_oi >= CONFIG["min_volume_oi_ratio"]:
                add_score(ticker, 3, "Strong Vol/OI")

            if vol_oi >= 3:
                add_score(ticker, 2, "Very strong Vol/OI")

    # =========================
    # 3. Option flow ranking
    # =========================
    if flow_df is not None and not flow_df.empty:
        for _, row in flow_df.iterrows():
            ticker = str(get_flow_value(row, "symbol")).upper()
            premium = clean_number(get_flow_value(row, "premium"))
            flow_text = " ".join([str(x).lower() for x in row.values])

            if not ticker:
                continue

            add_score(ticker, 3, "Option flow activity")

            if premium >= 200000:
                add_score(ticker, 3, "Large flow premium")

            if premium >= 500000:
                add_score(ticker, 4, "Very large flow premium")

            if "ask" in flow_text:
                add_score(ticker, 2, "Ask-side flow")

            if "sweep" in flow_text:
                add_score(ticker, 3, "Sweep flow")

            if "aggressive" in flow_text:
                add_score(ticker, 2, "Aggressive flow")

    ranked = sorted(
        ticker_scores.items(),
        key=lambda x: x[1]["score"],
        reverse=True
    )

    selected = [ticker for ticker, data in ranked[:CONFIG["max_dynamic_tickers"]]]

    print("Ranked dynamic tickers:")
    for ticker, data in ranked[:20]:
        print(ticker, "Score:", data["score"], "|", ", ".join(data["reasons"][:5]))

    return selected
    
def was_stock_already_sent_today(signal_key):
    if not os.path.exists(CONFIG["excel_file"]):
        return False

    try:
        wb = load_workbook(CONFIG["excel_file"])

        if "StockSignals" not in wb.sheetnames:
            return False

        ws = wb["StockSignals"]
        today = get_market_now().strftime("%Y-%m-%d")

        for row in ws.iter_rows(min_row=2, values_only=True):
            dt = str(row[0])
            ticker = str(row[1])
            signal_type = str(row[2])

            if not dt.startswith(today):
                continue

            historical_key = f"STOCK_{ticker}_{signal_type}_{today}"

            if historical_key == signal_key:
                return True

        return False

    except Exception as e:
        print("Stock duplicate Excel check error:", e)
        return False
        
def scan_and_send_dynamic_stock_signals(tickers):
    if not CONFIG.get("enable_stock_signals", True):
        print("Stock signals disabled by ENABLE_STOCK_SIGNALS=false")
        return
    if not CONFIG["enable_dynamic_stock_scanner"]:
        return

    signals = []

    for ticker in tickers:
        signal = scan_stock(ticker)

        if not signal:
            continue

        key = make_stock_signal_key(signal)

        if is_duplicate_signal(key, "stock") or was_stock_already_sent_today(key):
            print(f"Duplicate dynamic stock skipped: {key}")
            continue

        signals.append(signal)

    signals = sorted(signals, key=lambda x: x["score"], reverse=True)
    top = signals[:CONFIG["max_stock_alerts_per_scan"]]

    for signal in top:
        if send_investment_telegram_message(build_stock_message(signal)):
            GLOBAL_ALERTED_STOCKS.add(
                f"STOCK_{make_stock_signal_key(signal)}_{get_market_now().strftime('%Y-%m-%d')}"
)

        save_stock_signal_to_excel(signal)
        time.sleep(1)
        
def make_option_signal_key(row):
    ticker = str(get_unusual_value(row, "symbol")).upper()
    option_type = str(get_unusual_value(row, "type")).upper()
    strike = str(get_unusual_value(row, "strike"))
    expiry = str(get_unusual_value(row, "expiration"))

    return f"OPTION_{ticker}_{expiry}_{strike}_{option_type}_{datetime.now().strftime('%Y-%m-%d')}"

def is_price_extended(price, reference, max_pct):
    if reference <= 0:
        return False
    return abs((price - reference) / reference) * 100 > max_pct
    
def make_stock_signal_key(signal):
    return f"STOCK_{signal['ticker']}_{signal['signal']}_{datetime.now().strftime('%Y-%m-%d')}"
    
def clean_number(value):
    try:
        if pd.isna(value):
            return 0
        value = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
        return float(value) if value else 0
    except:
        return 0    
def add_reject_stat(reject_stats, reason):
    reason = str(reason).strip()

    if not reason:
        reason = "Unknown reject reason"

    reject_stats[reason] = reject_stats.get(reason, 0) + 1

def scalar(value):
    try:
        if hasattr(value, "iloc"):
            return float(value.iloc[0])
        return float(value)
    except:
        return 0.0


def get_first_available_value(row, possible_column_names):
    for column_name in possible_column_names:
        if column_name in row:
            return row.get(column_name)
    return 0


def get_unusual_value(row, field_name):
    return get_first_available_value(row, CONFIG["unusual_columns"].get(field_name, []))


def get_flow_value(row, field_name):
    return get_first_available_value(row, CONFIG["flow_columns"].get(field_name, []))


def get_option_chain_value(row, field_name):
    return get_first_available_value(row, CONFIG["option_chain_columns"].get(field_name, []))



def get_option_direction_from_type(option_type):
    option_type = str(option_type).lower()
    if "call" in option_type or option_type == "c":
        return "bullish"
    if "put" in option_type or option_type == "p":
        return "bearish"
    return "unknown"


def parse_bid_ask_size_price(value):
    """
    Parses Barchart values like '1.54 x 1064' and returns the price part.
    """
    try:
        value = str(value).replace(",", "").strip()
        if not value or value.lower() == "nan":
            return 0.0
        return clean_number(value.split("x")[0].strip())
    except:
        return 0.0


def get_flow_trade_price(flow_row):
    trade = clean_number(get_flow_value(flow_row, "trade"))
    if trade > 0:
        return trade

    bid = parse_bid_ask_size_price(get_flow_value(flow_row, "bid_x_size"))
    ask = parse_bid_ask_size_price(get_flow_value(flow_row, "ask_x_size"))

    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    if ask > 0:
        return ask
    return bid


def get_flow_code(flow_row):
    return str(get_flow_value(flow_row, "code")).upper().strip()


def get_row_expiry(row, source="unusual"):
    if source == "flow":
        return str(get_flow_value(row, "expiration")).strip()
    return str(get_unusual_value(row, "expiration")).strip()


def get_row_dte(row, source="unusual"):
    raw_dte = clean_number(get_flow_value(row, "dte") if source == "flow" else get_unusual_value(row, "dte"))
    if raw_dte > 0:
        return int(raw_dte)

    expiry = get_row_expiry(row, source)
    parsed = parse_expiry_to_dte(expiry)
    return int(parsed) if parsed is not None else 0


def build_flow_contract_key(flow_row):
    ticker = str(get_flow_value(flow_row, "symbol")).upper().strip()
    option_type = str(get_flow_value(flow_row, "type")).upper().strip()
    strike = str(get_flow_value(flow_row, "strike")).strip()
    expiry = get_row_expiry(flow_row, "flow")
    return f"{ticker}_{expiry}_{strike}_{option_type}"


def build_unusual_contract_key(row):
    ticker = str(get_unusual_value(row, "symbol")).upper().strip()
    option_type = str(get_unusual_value(row, "type")).upper().strip()
    strike = str(get_unusual_value(row, "strike")).strip()
    expiry = get_row_expiry(row, "unusual")
    return f"{ticker}_{expiry}_{strike}_{option_type}"

def spread_pct(bid, ask):
    bid = clean_number(bid)
    ask = clean_number(ask)
    mid = (bid + ask) / 2
    if mid <= 0:
        return 999
    return ((ask - bid) / mid) * 100

def is_after_time(hour_key, minute_key):
    now = get_market_now()
    cutoff = make_time(hour_key, minute_key)
    return now.time() >= cutoff
    
def get_market_now():
    return datetime.now(pytz.timezone(CONFIG["market_timezone"]))


def make_time(hour_key, minute_key):
    return dt_time(CONFIG[hour_key], CONFIG[minute_key])


def normalize_columns(df):
    df.columns = [str(c).strip() for c in df.columns]
    return df


def read_barchart_csv(file_path):
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        header_index = 0

        for i, line in enumerate(lines):
            lower = line.lower()
            if "symbol" in lower and ("volume" in lower or "open interest" in lower or "premium" in lower or "%chg" in lower or "last" in lower):
                header_index = i
                break

        df = pd.read_csv(file_path, skiprows=header_index)
        return normalize_columns(df)

    except Exception as e:
        print("Barchart CSV read error:", e)
        return pd.DataFrame()


def save_debug(driver, name):
    try:
        os.makedirs(CONFIG["debug_folder"], exist_ok=True)
        path = os.path.join(CONFIG["debug_folder"], f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        driver.save_screenshot(path)
        print("Debug screenshot saved:", path)
        try:
            with open(path[:-4] + ".html", "w", encoding="utf-8") as _f:
                _f.write(driver.page_source)
        except Exception:
            pass
    except Exception as e:
        print("Debug screenshot failed:", e)


# ── NYSE holiday calendar (same rules as uw-options-bot/main.py) ─────────────
def _easter_sunday(year):
    a = year % 19; b = year // 100; c = year % 100
    d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    d0 = date(year, month, 1)
    return d0 + timedelta(days=(weekday - d0.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d0 = nxt - timedelta(days=1)
    return d0 - timedelta(days=(d0.weekday() - weekday) % 7)


def _observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


_NYSE_HOLIDAY_CACHE = {}


def nyse_holidays(year):
    if year not in _NYSE_HOLIDAY_CACHE:
        _NYSE_HOLIDAY_CACHE[year] = {
            _observed(date(year, 1, 1)),                 # New Year's Day
            _nth_weekday(year, 1, 0, 3),                 # MLK Day
            _nth_weekday(year, 2, 0, 3),                 # Presidents Day
            _easter_sunday(year) - timedelta(days=2),    # Good Friday
            _last_weekday(year, 5, 0),                   # Memorial Day
            _observed(date(year, 6, 19)),                # Juneteenth
            _observed(date(year, 7, 4)),                 # Independence Day
            _nth_weekday(year, 9, 0, 1),                 # Labor Day
            _nth_weekday(year, 11, 3, 4),                # Thanksgiving
            _observed(date(year, 12, 25)),               # Christmas
        }
    return _NYSE_HOLIDAY_CACHE[year]


def is_market_day():
    d = get_market_now().date()
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def is_regular_market_hours():
    now = get_market_now()
    return is_market_day() and make_time("market_open_hour", "market_open_minute") <= now.time() <= make_time("market_close_hour", "market_close_minute")


def is_lunch_chop_window():
    if not CONFIG["avoid_lunch_chop"]:
        return False
    now = get_market_now()
    return make_time("lunch_start_hour", "lunch_start_minute") <= now.time() <= make_time("lunch_end_hour", "lunch_end_minute")


def is_scan_window_open():
    now = get_market_now()
    return (
        is_regular_market_hours()
        and make_time("scan_start_hour", "scan_start_minute") <= now.time() <= make_time("scan_end_hour", "scan_end_minute")
        and not is_lunch_chop_window()
    )


def is_new_signal_allowed():
    now = get_market_now()
    return is_scan_window_open() and now.time() <= make_time("new_signal_cutoff_hour", "new_signal_cutoff_minute")


def get_market_status_message():
    now = get_market_now()
    if not is_market_day():
        return f"Market day check failed. Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    if not is_regular_market_hours():
        return f"Outside regular market hours. Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    if is_lunch_chop_window():
        return f"Lunch chop window active. Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    if not is_scan_window_open():
        return f"Outside configured scan window. Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    if not is_new_signal_allowed():
        return f"New signal cutoff reached. Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    return f"Market open and scan window active. Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"


def get_telegram_chat_id(channel_type="investment"):
    """
    Strict Telegram channel routing.

    Final routing rule:
    - option / options / gamma / straddle -> TELEGRAM_OPTION_CHAT_ID
    - stock / stocks / investment / news / premarket / report / default -> TELEGRAM_INVESTMENT_CHAT_ID

    TELEGRAM_CHAT_ID is used only as a fallback for backward compatibility.
    """
    channel_type = str(channel_type or "investment").lower().strip()

    option_types = ["option", "options", "gamma", "straddle", "volatility"]
    investment_types = [
        "investment", "stock", "stocks", "news", "premarket",
        "report", "dashboard", "summary", "monitor", "default"
    ]

    if channel_type in option_types:
        return CONFIG.get("telegram_option_chat_id") or CONFIG.get("telegram_chat_id")

    if channel_type in investment_types:
        return CONFIG.get("telegram_investment_chat_id") or CONFIG.get("telegram_chat_id")

    # Unknown/non-trade operational messages go to investment channel, not option channel.
    return CONFIG.get("telegram_investment_chat_id") or CONFIG.get("telegram_chat_id")


def send_telegram_message(message, channel_type="investment", chat_id=None):
    token = CONFIG["telegram_bot_token"]
    channel_type = str(channel_type or "investment").lower().strip()
    final_chat_id = chat_id or get_telegram_chat_id(channel_type)

    if not token or not final_chat_id:
        print(f"Telegram token/chat id missing for channel_type={channel_type}. Check .env.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": final_chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, data=payload, timeout=15)
        if response.status_code == 200:
            print(f"Telegram message sent to {channel_type} channel: {final_chat_id}")
            return True
        print("Telegram error:", response.text)
        return False
    except Exception as e:
        print("Telegram failed:", e)
        return False


def send_option_telegram_message(message):
    """All option/gamma/straddle alerts must use this wrapper."""
    return send_telegram_message(message, channel_type="option")


def send_investment_telegram_message(message):
    """All stock, investment, news, premarket, report and dashboard alerts use this wrapper."""
    return send_telegram_message(message, channel_type="investment")


def init_excel():
    file_exists = os.path.exists(CONFIG["excel_file"])

    if file_exists:
        wb = load_workbook(CONFIG["excel_file"])
    else:
        wb = Workbook()

    if "OptionSignals" not in wb.sheetnames:
        if "Sheet" in wb.sheetnames:
            ws = wb["Sheet"]
            ws.title = "OptionSignals"
        else:
            ws = wb.create_sheet("OptionSignals")

        ws.append([
            "DateTime", "Ticker", "Decision", "Score", "Option Type", "Strike", "Expiry",
            "Entry", "Stop Loss", "Target 1", "Target 2", "Premium", "Volume", "OI",
            "Vol/OI", "DTE", "Chain Status", "Call Wall", "Put Wall", "Reasons",
            "Status", "Success Date", "Exit Price", "Result",
            # v16 additions:
            "Setup Type", "IV %ile", "VIX Regime", "Latency (s)"
        ])
    else:
        # v16: in-place migration — append missing columns to existing sheets.
        ws = wb["OptionSignals"]
        existing_headers = [str(cell.value) if cell.value is not None else "" for cell in ws[1]]
        v16_columns = ["Setup Type", "IV %ile", "VIX Regime", "Latency (s)"]
        for col_name in v16_columns:
            if col_name not in existing_headers:
                next_col = ws.max_column + 1
                ws.cell(row=1, column=next_col).value = col_name

    if "StockSignals" not in wb.sheetnames:
        ws2 = wb.create_sheet("StockSignals")
        ws2.append([
            "DateTime", "Ticker", "Signal", "Score", "Setup", "Price", "Entry",
            "Stop Loss", "Target 1", "Target 2", "News Gist", "Reasons",
            "Status", "Success Date", "Exit Price", "Result"
        ])

    if "NewsRunnerSignals" not in wb.sheetnames:
        ws3 = wb.create_sheet("NewsRunnerSignals")
        ws3.append([
            "DateTime", "Ticker", "Signal", "Score", "Setup", "Price", "Previous Close",
            "Gap %", "Volume", "Avg Volume", "Rel Volume", "Float", "VWAP",
            "VWAP Hold", "Day High", "Day Low", "Entry", "Stop Loss",
            "Target 1", "Target 2", "News Source", "Headline", "Catalyst Tags",
            "Negative Tags", "Reasons", "Status", "Success Date", "Exit Price", "Result"
        ])

    if "PerformanceDashboard" not in wb.sheetnames:
        ws4 = wb.create_sheet("PerformanceDashboard")
        ws4.append([
            "Generated At", "Signal Group", "Category", "Total", "Wins", "Losses",
            "Open", "Win Rate %", "Avg Score", "Avg Premium", "Avg DTE", "Notes"
        ])

    if "AutoLearning" not in wb.sheetnames:
        ws5 = wb.create_sheet("AutoLearning")
        ws5.append([
            "Generated At", "Area", "Finding", "Current Result", "Suggested Action",
            "Reason", "Priority"
        ])

    wb.save(CONFIG["excel_file"])

def save_option_signal_to_excel(row, decision, score, reasons, chain_result):
    init_excel()

    wb = load_workbook(CONFIG["excel_file"])
    ws = wb["OptionSignals"]

    ticker = str(get_unusual_value(row, "symbol")).upper()
    option_type = get_unusual_value(row, "type")
    strike = get_unusual_value(row, "strike")
    expiry = get_unusual_value(row, "expiration")
    entry = clean_number(get_unusual_value(row, "last"))
    premium = clean_number(get_unusual_value(row, "premium"))
    volume = clean_number(get_unusual_value(row, "volume"))
    oi = clean_number(get_unusual_value(row, "open_interest"))
    dte = clean_number(get_unusual_value(row, "dte"))
    vol_oi = volume / oi if oi > 0 else 0

    ws.append([
        get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        ticker,
        decision,
        score,
        option_type,
        strike,
        expiry,
        entry,
        round(entry * 0.75, 2) if entry else "",
        round(entry * 1.35, 2) if entry else "",
        round(entry * 1.75, 2) if entry else "",
        premium,
        volume,
        oi,
        round(vol_oi, 2),
        dte,
        chain_result["status"],
        chain_result["call_wall"],
        chain_result["put_wall"],
        ", ".join(reasons),
        "OPEN",
        "",
        "",
        ""
    ])

    wb.save(CONFIG["excel_file"])


def _fetch_option_quote_for_monitor(ticker, expiry, option_type, strike):
    """
    v17.x: fetch real option bid/ask for monitoring. Schwab primary,
    yfinance fallback. Returns dict {bid, ask, mid, underlying, source}
    or None if neither source returns a usable quote.
    """
    # Path 1: Schwab via schwab_data.get_option_quote
    try:
        from schwab_data import get_option_quote
        q = get_option_quote(ticker, expiry, option_type, strike)
        if q and q.get("bid") and q.get("ask"):
            bid = float(q["bid"])
            ask = float(q["ask"])
            if bid > 0 and ask > 0:
                return {
                    "bid": bid, "ask": ask,
                    "mid": round((bid + ask) / 2, 4),
                    "underlying": float(q.get("underlying_price") or q.get("underlyingPrice") or 0),
                    "source": "schwab",
                }
    except ImportError:
        pass
    except Exception as e:
        print(f"[monitor] Schwab quote failed for {ticker} {strike}{option_type[0]}: {e}")

    # Path 2: yfinance option_chain fallback
    try:
        exp_str = str(expiry)[:10]
        t = yf.Ticker(ticker)
        if exp_str not in t.options:
            return None
        chain = t.option_chain(exp_str)
        table = chain.calls if "CALL" in option_type.upper() else chain.puts
        row = table[table["strike"].round(2) == round(float(strike), 2)]
        if row.empty:
            return None
        bid = float(row["bid"].iloc[0])
        ask = float(row["ask"].iloc[0])
        if bid <= 0 or ask <= 0:
            return None
        underlying = 0.0
        try:
            underlying = float(t.fast_info.get("last_price", 0) or 0)
        except Exception:
            pass
        return {
            "bid": bid, "ask": ask,
            "mid": round((bid + ask) / 2, 4),
            "underlying": underlying,
            "source": "yfinance",
        }
    except Exception as e:
        print(f"[monitor] yfinance quote failed for {ticker} {strike}{option_type[0]}: {e}")
        return None


def _evaluate_option_outcome(entry, stop_loss, target_1, target_2, quote, max_spread_pct):
    """
    v17.x: decide STOP_HIT / TARGET_1_HIT / TARGET_HIT / STILL_OPEN based
    on the REAL option mid against the option-priced thresholds in the log.
    Returns (status, exit_mid, reason).
    """
    if quote is None:
        return "STILL_OPEN", None, "quote_unavailable"

    mid = quote["mid"]
    spread_pct = (quote["ask"] - quote["bid"]) / mid if mid > 0 else 999.0
    if spread_pct > max_spread_pct:
        return "STILL_OPEN", mid, f"spread_{spread_pct:.1%}_too_wide"

    if target_2 and mid >= float(target_2):
        return "TARGET_HIT", mid, f"mid_{mid}_ge_t2_{target_2}"
    if target_1 and mid >= float(target_1):
        return "TARGET_1_HIT", mid, f"mid_{mid}_ge_t1_{target_1}"
    if stop_loss and mid <= float(stop_loss):
        return "STOP_HIT", mid, f"mid_{mid}_le_stop_{stop_loss}"
    return "STILL_OPEN", mid, f"mid_{mid}_between_stop_and_t1"


def _legacy_stock_proxy_outcome(option_type, current_underlying, strike):
    """
    v17.x: extracted legacy stock-proxy logic so we can run it side-by-side
    with the new option-price logic during shadow mode and audit the diffs.
    DO NOT use this for live decisions once monitor_use_option_price=True.
    """
    if "CALL" in option_type and current_underlying >= strike:
        return "TARGET_1_HIT"
    if "PUT" in option_type and current_underlying <= strike:
        return "TARGET_1_HIT"
    if "CALL" in option_type and current_underlying < strike * 0.985:
        return "STOP_HIT"
    if "PUT" in option_type and current_underlying > strike * 1.015:
        return "STOP_HIT"
    return "STILL_OPEN"


def _append_shadow_record(path, record):
    """v17.x: append one row to the shadow-mode audit CSV."""
    try:
        import csv as _csv
        file_exists = os.path.exists(path)
        with open(path, "a", newline="") as fh:
            writer = _csv.DictWriter(fh, fieldnames=list(record.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(record)
    except Exception as e:
        print(f"[monitor/shadow] Failed to write {path}: {e}")


def monitor_open_option_signals():
    """
    Monitors OPEN option signals from Excel and updates status.

    v17.x: now uses REAL option mid price (Schwab → yfinance fallback) to
    compare against the Stop Loss / Target 1 / Target 2 thresholds already
    stored in the sheet, instead of the underlying stock price.

    Behavior is gated by CONFIG:
      monitor_use_option_price=True   → option-price logic decides outcomes
      monitor_use_option_price=False  → legacy stock-proxy logic (rollback)
      monitor_shadow_mode=True        → ALSO log the OTHER logic's decision
                                          to shadow_outcomes.csv for audit,
                                          regardless of which one is live.

    Recommended rollout:
      1. Day 0-5: monitor_use_option_price=False, monitor_shadow_mode=True
         (legacy runs live, new logic logged for review)
      2. Review shadow_outcomes.csv diffs
      3. Day 5+: flip monitor_use_option_price=True
    """

    if not CONFIG.get("enable_trade_monitoring", True):
        return

    excel_file = CONFIG["excel_file"]

    if not os.path.exists(excel_file):
        print("Trade monitor skipped: Excel file not found.")
        return

    use_option_price = CONFIG.get("monitor_use_option_price", False)
    shadow_mode = CONFIG.get("monitor_shadow_mode", True)
    shadow_csv = CONFIG.get("monitor_shadow_csv", "shadow_outcomes.csv")
    max_spread = CONFIG.get("monitor_max_spread_pct", 0.25)

    try:
        wb = load_workbook(excel_file)

        if "OptionSignals" not in wb.sheetnames:
            print("Trade monitor skipped: OptionSignals sheet not found.")
            return

        ws = wb["OptionSignals"]

        headers = [cell.value for cell in ws[1]]
        header_map = {str(name): idx + 1 for idx, name in enumerate(headers) if name}

        required = [
            "Ticker", "Decision", "Option Type", "Strike", "Expiry",
            "Entry", "Stop Loss", "Target 1", "Target 2", "Status",
            "Success Date", "Exit Price", "Result"
        ]

        for col in required:
            if col not in header_map:
                print(f"Trade monitor skipped: missing column {col}")
                return

        monitored = 0
        updated = 0
        shadow_diffs = 0

        for row_num in range(2, ws.max_row + 1):
            status = str(ws.cell(row_num, header_map["Status"]).value or "").upper().strip()

            if status != "OPEN":
                continue

            ticker = str(ws.cell(row_num, header_map["Ticker"]).value or "").upper().strip()
            decision = str(ws.cell(row_num, header_map["Decision"]).value or "").upper().strip()
            option_type = str(ws.cell(row_num, header_map["Option Type"]).value or "").upper().strip()
            expiry = ws.cell(row_num, header_map["Expiry"]).value
            strike = clean_number(ws.cell(row_num, header_map["Strike"]).value)
            entry = clean_number(ws.cell(row_num, header_map["Entry"]).value)
            stop_loss = clean_number(ws.cell(row_num, header_map["Stop Loss"]).value)
            target_1 = clean_number(ws.cell(row_num, header_map["Target 1"]).value)
            target_2 = clean_number(ws.cell(row_num, header_map["Target 2"]).value)

            if not ticker or entry <= 0:
                continue

            if CONFIG.get("monitor_only_trade_and_high_conviction", True):
                if decision not in ["TRADE", "HIGH_CONVICTION"]:
                    continue

            monitored += 1

            try:
                # --- Fetch real option quote (always, so shadow mode works) ---
                quote = _fetch_option_quote_for_monitor(ticker, expiry, option_type, strike)

                # --- Fetch underlying for legacy proxy and context ---
                current_underlying = quote["underlying"] if quote else None
                if current_underlying is None or current_underlying <= 0:
                    yf_df = yf.Ticker(ticker).history(period="1d", interval="1m", auto_adjust=False)
                    if yf_df is not None and not yf_df.empty:
                        if isinstance(yf_df.columns, pd.MultiIndex):
                            yf_df.columns = yf_df.columns.get_level_values(0)
                        current_underlying = float(yf_df["Close"].dropna().iloc[-1])

                # --- Compute both outcomes ---
                option_status, option_mid, option_reason = _evaluate_option_outcome(
                    entry, stop_loss, target_1, target_2, quote, max_spread
                )
                legacy_status = _legacy_stock_proxy_outcome(
                    option_type, current_underlying or 0, strike
                )

                # --- Pick the LIVE result based on CONFIG ---
                if use_option_price:
                    live_status = option_status
                    live_exit = option_mid if option_mid is not None else (current_underlying or 0)
                else:
                    live_status = legacy_status
                    # Legacy preserved unchanged: write underlying as Exit Price
                    live_exit = current_underlying or 0

                # --- Shadow audit: log BOTH if they disagree (or always, if you prefer) ---
                if shadow_mode and option_status != "STILL_OPEN" or legacy_status != "STILL_OPEN":
                    agrees = (option_status == legacy_status)
                    if not agrees:
                        shadow_diffs += 1
                    _append_shadow_record(shadow_csv, {
                        "checked_at": get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
                        "ticker": ticker, "option_type": option_type,
                        "strike": strike, "expiry": str(expiry)[:10],
                        "entry": entry, "stop_loss": stop_loss,
                        "target_1": target_1, "target_2": target_2,
                        "underlying_price": current_underlying,
                        "option_bid": quote["bid"] if quote else None,
                        "option_ask": quote["ask"] if quote else None,
                        "option_mid": option_mid,
                        "quote_source": quote["source"] if quote else "none",
                        "legacy_status": legacy_status,
                        "new_status": option_status,
                        "new_reason": option_reason,
                        "agrees": agrees,
                        "live_status_written": live_status,
                    })

                # --- Write live result to the sheet ---
                if live_status in ["TARGET_1_HIT", "TARGET_HIT", "STOP_HIT"]:
                    ws.cell(row_num, header_map["Status"]).value = live_status
                    ws.cell(row_num, header_map["Success Date"]).value = get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z")
                    ws.cell(row_num, header_map["Exit Price"]).value = round(float(live_exit), 4)
                    ws.cell(row_num, header_map["Result"]).value = live_status
                    updated += 1

            except Exception as e:
                ws.cell(row_num, header_map["Status"]).value = "MONITOR_ERROR"
                ws.cell(row_num, header_map["Result"]).value = str(e)[:200]

        wb.save(excel_file)

        mode_str = "option-price" if use_option_price else "stock-proxy(legacy)"
        shadow_str = f", shadow diffs: {shadow_diffs}" if shadow_mode else ""
        print(f"Option monitor [{mode_str}] complete. Monitored: {monitored}, Updated: {updated}{shadow_str}")

    except Exception as e:
        print("Option monitor error:", e)
        

def save_stock_signal_to_excel(signal):
    init_excel()

    wb = load_workbook(CONFIG["excel_file"])
    ws = wb["StockSignals"]

    ws.append([
        get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        signal["ticker"],
        signal["signal"],
        signal["score"],
        signal["setup"],
        signal["price"],
        signal["entry"],
        signal["stop_loss"],
        signal["target_1"],
        signal["target_2"],
        signal["news_gist"],
        ", ".join(signal["reasons"]),
        "OPEN",
        "",
        "",
        ""
    ])

    wb.save(CONFIG["excel_file"])


def setup_driver():
    for folder in [
        CONFIG["download_folder"],
        CONFIG["archive_folder"],
        CONFIG["image_folder"],
        CONFIG["debug_folder"],
        CONFIG["chrome_profile_folder"],
    ]:
        os.makedirs(folder, exist_ok=True)

    chrome_options = Options()

    prefs = {
        "download.default_directory": CONFIG["download_folder"],
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }

    if DISABLE_IMAGES:
        prefs["profile.managed_default_content_settings.images"] = 2

    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument(f"--user-data-dir={CONFIG['chrome_profile_folder']}")
    chrome_options.add_argument("--profile-directory=Default")

    if HEADLESS:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-background-networking")
        chrome_options.add_argument("--disable-renderer-backgrounding")
        chrome_options.add_argument("--renderer-process-limit=2")
        chrome_options.add_argument("--mute-audio")
    else:
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_experimental_option("detach", True)

    if BLOCK_AD_HOSTS:
        ad_hosts = [
            "*.doubleclick.net", "*.googlesyndication.com", "*.googleadservices.com",
            "*.adnxs.com", "*.amazon-adsystem.com", "*.rubiconproject.com",
            "*.pubmatic.com", "*.openx.net", "*.criteo.com", "*.criteo.net",
            "*.taboola.com", "*.outbrain.com", "*.moatads.com", "*.casalemedia.com",
            "*.3lift.com", "*.sharethrough.com", "*.adsrvr.org", "*.quantserve.com",
            "*.scorecardresearch.com", "*.facebook.net", "*.hotjar.com",
        ]
        rules = ", ".join(f"MAP {h} ~NOTFOUND" for h in ad_hosts)
        chrome_options.add_argument(f"--host-resolver-rules={rules}")

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(90)

    # Headless UA says "HeadlessChrome" -- present the normal Chrome UA instead.
    if HEADLESS:
        try:
            ua = driver.execute_cdp_cmd("Browser.getVersion", {}).get("userAgent", "")
            if "HeadlessChrome" in ua:
                driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                       {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
        except Exception as e:
            print("UA override failed (ignored):", e)

    # Headless Chrome only saves downloads if told where to put them.
    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": CONFIG["download_folder"],
        })
    except Exception as e:
        print("setDownloadBehavior failed (ignored):", e)

    return driver


class LazyDriver:
    """
    VM memory saver. Chrome is started only when a page is actually needed and
    shut down (release()) while the bot sleeps between cycles, so the browser and
    the pandas/yfinance analysis never hold RAM at the same time.
    Any attribute access (driver.get, driver.find_elements, ...) starts Chrome on demand.
    The persistent profile keeps the Barchart login cookie between restarts.
    """

    def __init__(self):
        self._driver = None

    def _ensure(self):
        if self._driver is None:
            print("[chrome] starting browser...")
            self._driver = setup_driver()
            ensure_logged_in(self._driver)
        return self._driver

    def __getattr__(self, name):
        return getattr(self._ensure(), name)

    @property
    def is_running(self):
        return self._driver is not None

    def release(self):
        if self._driver is not None:
            try:
                self._driver.quit()
                print("[chrome] browser closed (idle).")
            except Exception as e:
                print(f"[chrome] close error (ignored): {e}")
            self._driver = None

    def quit(self):
        self.release()


def _idle(driver, seconds):
    """Close Chrome (if lazy) and sleep."""
    if isinstance(driver, LazyDriver) and os.getenv("RELEASE_CHROME_BETWEEN_CYCLES", "true" if IS_LINUX else "false").lower() == "true":
        driver.release()
    # Never oversleep the daily stop time (e.g. 16:30 ET): wake just after it
    # so the loop can shut down on schedule.
    try:
        now = get_market_now()
        stop_at = now.replace(hour=CONFIG["after_hours_end_hour"],
                              minute=CONFIG["after_hours_end_minute"],
                              second=5, microsecond=0)
        if now < stop_at:
            seconds = min(seconds, max(1, (stop_at - now).total_seconds()))
    except Exception:
        pass
    time.sleep(seconds)


def move_png_files():
    png_files = glob.glob(os.path.join(CONFIG["download_folder"], "*.png"))

    for file_path in png_files:
        try:
            filename = os.path.basename(file_path)
            new_path = os.path.join(CONFIG["image_folder"], filename)

            if os.path.exists(new_path):
                base, ext = os.path.splitext(filename)
                new_path = os.path.join(CONFIG["image_folder"], f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}")

            shutil.move(file_path, new_path)
        except Exception as e:
            print("Error moving PNG:", e)


def archive_csv(file_path):
    try:
        if not file_path or not os.path.exists(file_path):
            return

        today_folder = os.path.join(CONFIG["archive_folder"], datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(today_folder, exist_ok=True)

        filename = os.path.basename(file_path)
        destination = os.path.join(today_folder, filename)

        if os.path.exists(destination):
            base, ext = os.path.splitext(filename)
            destination = os.path.join(today_folder, f"{base}_{datetime.now().strftime('%H%M%S')}{ext}")

        shutil.move(file_path, destination)
        print("Archived:", destination)
    except Exception as e:
        print("Archive error:", e)


def get_latest_csv():
    files = glob.glob(os.path.join(CONFIG["download_folder"], "*.csv"))
    return max(files, key=os.path.getctime) if files else None


def wait_for_new_csv(old_file):
    start = time.time()

    while time.time() - start < CONFIG["download_wait_timeout_seconds"]:
        move_png_files()
        files = glob.glob(os.path.join(CONFIG["download_folder"], "*.csv"))

        if files:
            latest = max(files, key=os.path.getctime)
            if latest != old_file and not latest.endswith(".crdownload"):
                return latest

        time.sleep(CONFIG["download_poll_seconds"])

    return None


def js_set_value(driver, element, value):
    driver.execute_script(
        """
        arguments[0].focus();
        arguments[0].value = arguments[1];
        arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
        arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value
    )


def js_click(driver, element):
    driver.execute_script("arguments[0].click();", element)


BARCHART_LOGIN_URL = "https://www.barchart.com/login"


def _visible(elements):
    for e in elements:
        try:
            if e.is_displayed():
                return e
        except Exception:
            continue
    return None


def _type_into(driver, element, value):
    """Real keystrokes (Barchart's form ignores JS-only value changes)."""
    try:
        element.click()
    except Exception:
        pass
    try:
        element.clear()
    except Exception:
        pass
    try:
        element.send_keys(value)
    except Exception:
        js_set_value(driver, element, value)


def is_barchart_logged_in(driver):
    """Logged in => a logout/account link exists; logged out => a visible Login link."""
    try:
        if driver.find_elements(By.XPATH, "//a[contains(@href,'logout')]"):
            return True
        login_links = driver.find_elements(
            By.XPATH, "//a[contains(@href,'/login') or normalize-space(translate(text(),'LOGIN','login'))='login']")
        return _visible(login_links) is None and bool(driver.find_elements(By.XPATH, "//body"))
    except Exception:
        return False


def _fill_login_form(driver):
    """Fill + submit whichever Barchart login form is visible (page or popup)."""
    email = os.getenv("BARCHART_EMAIL")
    password = os.getenv("BARCHART_PASSWORD")
    if not email or not password:
        print("[login] BARCHART_EMAIL / BARCHART_PASSWORD missing in .env")
        return False

    password_box = _visible(driver.find_elements(By.XPATH, "//input[@type='password']"))
    if password_box is None:
        return False

    # Email box must be in the SAME form/panel as the password -- never the site search box.
    email_box = None
    for xp in [
        "./ancestor::form[1]//input[@type='email' or @type='text']",
        "./ancestor::div[.//input[@type='email' or @type='text']][1]//input[@type='email' or @type='text']",
    ]:
        email_box = _visible(password_box.find_elements(By.XPATH, xp))
        if email_box is not None:
            break
    if email_box is None:
        email_box = _visible(driver.find_elements(
            By.XPATH, "//input[@type='email' or contains(translate(@placeholder,'EMAIL','email'),'email') "
                      "or contains(translate(@name,'EMAIL','email'),'email')]"))
    if email_box is None:
        save_debug(driver, "login_email_field_not_found")
        return False

    _type_into(driver, email_box, email)
    _type_into(driver, password_box, password)
    time.sleep(1)

    btn = None
    for xp in [
        "./ancestor::form[1]//button[@type='submit']",
        "./ancestor::form[1]//button[contains(translate(normalize-space(.),'LOGIN ','login'),'login')]",
    ]:
        btn = _visible(password_box.find_elements(By.XPATH, xp))
        if btn is not None:
            break
    if btn is None:
        btn = _visible(driver.find_elements(
            By.XPATH, "//button[contains(translate(normalize-space(.),'LOGIN ','login'),'login')]"))
    if btn is None:
        from selenium.webdriver.common.keys import Keys
        password_box.send_keys(Keys.RETURN)
    else:
        try:
            btn.click()
        except Exception:
            js_click(driver, btn)
    time.sleep(10)
    return True


def ensure_logged_in(driver):
    """Log in on the dedicated login page if the session is not already logged in."""
    try:
        if "barchart.com" not in (driver.current_url or ""):
            driver.get(CONFIG["barchart_unusual_url"])
            time.sleep(CONFIG["page_load_wait_seconds"])
        if is_barchart_logged_in(driver):
            print("[login] Barchart session active.")
            return True
        print("[login] Not logged in -- logging in to Barchart...")
        driver.get(BARCHART_LOGIN_URL)
        time.sleep(CONFIG["page_load_wait_seconds"])
        _fill_login_form(driver)
        driver.get(CONFIG["barchart_unusual_url"])
        time.sleep(CONFIG["page_load_wait_seconds"])
        if is_barchart_logged_in(driver):
            print("[login] Barchart login OK.")
            return True
        print("[login] Barchart login FAILED -- see debug/login_failed_*.png")
        save_debug(driver, "login_failed")
        return False
    except Exception as e:
        print("[login] error:", e)
        save_debug(driver, "login_error")
        return False


def handle_login_popup(driver):
    """If a login form/popup is showing, fill it in. Returns True if nothing blocks us."""
    try:
        if _visible(driver.find_elements(By.XPATH, "//input[@type='password']")) is None:
            return True
        print("[login] Login popup detected -- logging in...")
        return _fill_login_form(driver)
    except Exception as e:
        print("Login popup handling error:", e)
        save_debug(driver, "login_popup_error")
        return False


def safe_click(driver, element):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.5)
        element.click()
        return True
    except:
        try:
            js_click(driver, element)
            return True
        except:
            return False


def click_download_button(driver):
    direct_xpaths = [
        "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'download')]",
        "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'csv')]",
        "//*[contains(@class,'download')]",
        "//*[contains(@aria-label,'Download')]",
        "//*[contains(@aria-label,'CSV')]",
        "//*[contains(@title,'Download')]",
        "//*[contains(@title,'CSV')]",
        "//i[contains(@class,'download')]/ancestor::*[self::button or self::a][1]",
        "//span[contains(@class,'download')]/ancestor::*[self::button or self::a][1]",
    ]

    for xpath in direct_xpaths:
        try:
            elements = driver.find_elements(By.XPATH, xpath)

            for element in elements:
                try:
                    if not element.is_displayed():
                        continue

                    label = (
                        str(element.text).lower()
                        + " "
                        + str(element.get_attribute("class")).lower()
                        + " "
                        + str(element.get_attribute("aria-label")).lower()
                        + " "
                        + str(element.get_attribute("title")).lower()
                    )

                    if "download" in label or "csv" in label:
                        if safe_click(driver, element):
                            return True
                except:
                    continue
        except:
            continue

    buttons = driver.find_elements(By.XPATH, "//button")

    for btn in buttons:
        try:
            if not btn.is_displayed():
                continue

            if not safe_click(driver, btn):
                continue

            time.sleep(1)

            menu_items = driver.find_elements(
                By.XPATH,
                "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'csv') "
                "or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'download')]"
            )

            for item in menu_items:
                try:
                    if item.is_displayed():
                        if safe_click(driver, item):
                            return True
                except:
                    continue
        except:
            continue

    return False


def download_page_csv(driver, url, label):
    try:
        return _download_page_csv_once(driver, url, label)
    except WebDriverException as e:
        print(f"[chrome] {label}: browser error ({str(e).splitlines()[0][:150]}) -- restarting Chrome and retrying once")
        if isinstance(driver, LazyDriver):
            driver.release()
            try:
                return _download_page_csv_once(driver, url, label)
            except WebDriverException as e2:
                print(f"[chrome] {label}: retry failed: {str(e2).splitlines()[0][:150]}")
                driver.release()
        return None


def _download_page_csv_once(driver, url, label):
    print(f"Opening {label} page...")
    driver.get(url)
    time.sleep(CONFIG["page_load_wait_seconds"])

    handle_login_popup(driver)

    print(f"Refreshing {label} page...")
    driver.refresh()
    time.sleep(CONFIG["page_refresh_wait_seconds"])

    handle_login_popup(driver)

    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(2)
    except:
        pass

    old_file = get_latest_csv()

    if not click_download_button(driver):
        print(f"Could not find {label} download button.")
        save_debug(driver, f"{label}_download_failed")
        return None

    # Download button opened the "Membership Feature" login popup -> log in and retry once.
    time.sleep(3)
    if _visible(driver.find_elements(By.XPATH, "//input[@type='password']")) is not None:
        print(f"[login] {label}: download asked for login -- logging in and retrying.")
        handle_login_popup(driver)
        driver.get(url)
        time.sleep(CONFIG["page_load_wait_seconds"])
        if not click_download_button(driver):
            save_debug(driver, f"{label}_download_failed_after_login")
            return None

    latest_file = wait_for_new_csv(old_file)

    if latest_file:
        print(f"{label} downloaded:", latest_file)
        return latest_file

    print(f"{label} download failed.")
    save_debug(driver, f"{label}_download_timeout")
    return None


def download_unusual_options_csv(driver):
    return download_page_csv(driver, CONFIG["barchart_unusual_url"], "unusual_options")


def download_option_flow_csv(driver):
    return download_page_csv(driver, CONFIG["barchart_flow_url"], "option_flow")


def download_option_chain_csv(driver, ticker):
    url = CONFIG["barchart_option_chain_url_template"].format(ticker=ticker)
    return download_page_csv(driver, url, f"option_chain_{ticker}")


def get_flow_matches(flow_df, ticker, option_type, unusual_sentiment, strike=None, expiry=None):
    """
    Institutional flow matcher.

    Old issue:
    - The previous version depended too much on Barchart Sentiment.
    - Many Barchart Flow rows have no sentiment but still show real direction from CALL/PUT and code.
    - This caused valid rows like large ARM CALL or TSM PUT flow to be missed.

    New logic:
    - Same ticker is required.
    - Same direction is preferred.
    - Same strike/expiry receives extra scoring later.
    - Valid/aggressive Barchart flow codes are recognized.
    """
    if flow_df is None or flow_df.empty:
        return pd.DataFrame()

    ticker = str(ticker).upper().strip()
    option_type = str(option_type).lower().strip()
    unusual_direction = unusual_sentiment if unusual_sentiment in ["bullish", "bearish"] else get_option_direction_from_type(option_type)

    matches = []

    for _, flow_row in flow_df.iterrows():
        flow_symbol = str(get_flow_value(flow_row, "symbol")).upper().strip()
        if flow_symbol != ticker:
            continue

        flow_type = str(get_flow_value(flow_row, "type")).lower().strip()
        flow_sentiment = str(get_flow_value(flow_row, "sentiment")).lower().strip()
        flow_direction = flow_sentiment if flow_sentiment in ["bullish", "bearish"] else get_option_direction_from_type(flow_type)

        same_direction = flow_direction == unusual_direction or (
            ("call" in option_type and "call" in flow_type) or
            ("put" in option_type and "put" in flow_type)
        )

        if not same_direction:
            continue

        premium = clean_number(get_flow_value(flow_row, "premium"))
        trade_price = get_flow_trade_price(flow_row)
        size = clean_number(get_flow_value(flow_row, "size"))

        if premium <= 0 and trade_price > 0 and size > 0:
            premium = trade_price * size * 100

        code = get_flow_code(flow_row)

        if premium < CONFIG["min_flow_premium"] and code not in CONFIG["aggressive_flow_codes"]:
            continue

        matches.append(flow_row)

    return pd.DataFrame(matches) if matches else pd.DataFrame()


def analyze_option_chain(chain_df, signal_row):
    """
    Option-chain open-interest wall confirmation.

    This should NOT block every good trade.
    It only adds CAUTION when a call is at/above a major call wall or a put is at/below a major put wall.
    Large premium trades near a wall are kept as caution, not rejected.
    """
    if not CONFIG["enable_option_chain_confirmation"]:
        return {"status": "DISABLED", "decision": "PASS", "call_wall": "N/A", "put_wall": "N/A", "reason": "Option chain disabled"}

    if chain_df is None or chain_df.empty:
        return {"status": "UNKNOWN", "decision": "PASS", "call_wall": "N/A", "put_wall": "N/A", "reason": "Option chain unavailable"}

    rows = []

    for _, row in chain_df.iterrows():
        strike = clean_number(get_option_chain_value(row, "strike"))
        call_oi = clean_number(get_option_chain_value(row, "call_oi"))
        put_oi = clean_number(get_option_chain_value(row, "put_oi"))

        if strike > 0:
            rows.append({"strike": strike, "call_oi": call_oi, "put_oi": put_oi})

    if not rows:
        return {"status": "UNKNOWN", "decision": "PASS", "call_wall": "N/A", "put_wall": "N/A", "reason": "No valid option-chain rows"}

    wall_df = pd.DataFrame(rows)

    call_wall = clean_number(wall_df.sort_values("call_oi", ascending=False).iloc[0]["strike"])
    put_wall = clean_number(wall_df.sort_values("put_oi", ascending=False).iloc[0]["strike"])

    option_type = str(get_unusual_value(signal_row, "type")).lower()
    signal_strike = clean_number(get_unusual_value(signal_row, "strike"))
    premium = clean_number(get_unusual_value(signal_row, "premium"))

    decision = "PASS"
    status = "CHAIN_CONFIRMED"
    reason = "Option chain wall check passed"

    if "call" in option_type and call_wall > 0 and signal_strike >= call_wall:
        decision = "CAUTION"
        status = "CALL_WALL_CAUTION"
        reason = "Call strike is at/above major call wall resistance"

    if "put" in option_type and put_wall > 0 and signal_strike <= put_wall:
        decision = "CAUTION"
        status = "PUT_WALL_CAUTION"
        reason = "Put strike is at/below major put wall support"

    if premium >= CONFIG["high_conviction_premium"] and decision == "CAUTION" and CONFIG["allow_high_premium_near_wall"]:
        status = "HIGH_PREMIUM_WALL_CAUTION"
        reason = "Large premium flow near option wall; trade allowed with caution"

    return {"status": status, "decision": decision, "call_wall": call_wall, "put_wall": put_wall, "reason": reason}


def evaluate_signal_with_scoring(row, flow_df, chain_df):
    """
    Updated institutional option decision engine.

    Fixes:
    - Uses Option Flow confirmation even when Barchart Sentiment is blank.
    - Scores same ticker + same direction + same contract.
    - Gives strong weight to premium, Vol/OI, DTE sweet spot, and aggressive flow codes.
    - Does not reject valid screenshot-style trades just because option-chain wall is nearby.
    """
    score = 0
    reasons = []

    ticker = str(get_unusual_value(row, "symbol")).upper().strip()
    option_type = str(get_unusual_value(row, "type")).lower().strip()
    sentiment = str(get_unusual_value(row, "sentiment")).lower().strip()

    volume = clean_number(get_unusual_value(row, "volume"))
    oi = clean_number(get_unusual_value(row, "open_interest"))
    option_price = clean_number(get_unusual_value(row, "last"))
    dte = get_row_dte(row, "unusual")
    premium = clean_number(get_unusual_value(row, "premium"))

    bid = clean_number(get_unusual_value(row, "bid"))
    ask = clean_number(get_unusual_value(row, "ask"))
    spread = spread_pct(bid, ask)

    vol_oi_raw = clean_number(get_unusual_value(row, "vol_oi"))
    vol_oi = vol_oi_raw if vol_oi_raw > 0 else (volume / oi if oi > 0 else volume)

    if option_price <= 0 and bid > 0 and ask > 0:
        option_price = round((bid + ask) / 2, 2)
    elif option_price <= 0 and ask > 0:
        option_price = ask

    if option_price < CONFIG["min_option_price"]:
        return "REJECT", score, ["Option price too low or missing"]

    if not (CONFIG["option_min_dte"] <= dte <= CONFIG["option_max_dte"]):
        return "REJECT", score, [f"DTE outside option range: {dte}"]

    if premium < CONFIG["min_premium"]:
        return "REJECT", score, ["Premium below threshold"]

    if spread > CONFIG["max_bid_ask_spread_pct"]:
        return "REJECT", score, ["Spread too wide"]

    # Infer direction from contract type if Barchart sentiment is missing.
    if sentiment not in ["bullish", "bearish"]:
        sentiment = get_option_direction_from_type(option_type)
        if sentiment in ["bullish", "bearish"]:
            reasons.append(f"Sentiment inferred from {'CALL' if sentiment == 'bullish' else 'PUT'}")
        else:
            return "REJECT", score, ["Cannot infer option direction"]

    if sentiment == "bullish" and "put" in option_type:
        return "REJECT", score, ["Bullish sentiment but PUT contract"]

    if sentiment == "bearish" and "call" in option_type:
        return "REJECT", score, ["Bearish sentiment but CALL contract"]

    # Base unusual activity
    score += 3
    reasons.append("Unusual option activity passed")

    # DTE scoring
    if CONFIG["option_sweet_spot_min_dte"] <= dte <= CONFIG["option_sweet_spot_max_dte"]:
        score += 4
        reasons.append("Institutional DTE sweet spot 14–30")
    elif 3 <= dte <= 45:
        score += 2
        reasons.append("Good swing DTE")
    elif dte <= 2:
        score -= 1
        reasons.append("Very short DTE: intraday/high gamma risk")

    # Premium scoring
    if premium >= CONFIG["option_premium_huge"]:
        score += 6
        reasons.append("Institutional premium $1M+")
    elif premium >= CONFIG["option_premium_large"]:
        score += 5
        reasons.append("Very large premium $500K+")
    elif premium >= CONFIG["option_premium_medium"]:
        score += 4
        reasons.append("Large premium $100K+")
    elif premium >= CONFIG["option_premium_small"]:
        score += 2
        reasons.append("Good premium")

    # Volume / OI
    if volume >= CONFIG["option_volume_very_high"]:
        score += 3
        reasons.append("Very high option volume")
    elif volume >= CONFIG["option_volume_high"]:
        score += 2
        reasons.append("High option volume")
    elif volume >= CONFIG["option_volume_decent"]:
        score += 1
        reasons.append("Decent option volume")

    if oi <= 0 and volume >= CONFIG["option_volume_decent"]:
        score += 3
        reasons.append("Fresh opening activity: volume with low/zero OI")
    elif oi < 100:
        score += 1
        reasons.append("Low OI / possible fresh position")

    if vol_oi >= CONFIG["option_vol_oi_aggressive"]:
        score += 4
        reasons.append("Aggressive Vol/OI")
    elif vol_oi >= CONFIG["option_vol_oi_strong"]:
        score += 3
        reasons.append("Strong Vol/OI")
    elif vol_oi >= CONFIG["option_vol_oi_ok"]:
        score += 1
        reasons.append("Vol/OI acceptable")

    # No-chase midpoint warning
    mid_price = (bid + ask) / 2 if bid > 0 and ask > 0 else option_price
    if option_price > 0 and mid_price > 0:
        option_chase_pct = abs((option_price - mid_price) / mid_price) * 100
        if option_chase_pct > CONFIG["option_no_chase_spread_pct"]:
            score -= 2
            reasons.append("No-chase warning: option price extended from midpoint")

    if is_after_time("late_session_risk_hour", "late_session_risk_minute"):
        score -= 2
        reasons.append("Late session risk after 3:30 PM")

    # Flow confirmation
    strike = str(get_unusual_value(row, "strike")).strip()
    expiry = get_row_expiry(row, "unusual")
    flow_matches = get_flow_matches(flow_df, ticker, option_type, sentiment, strike=strike, expiry=expiry)

    has_flow_confirmation = not flow_matches.empty

    if has_flow_confirmation:
        score += CONFIG["flow_same_direction_bonus"]
        reasons.append("Option flow confirms ticker/direction")

        total_flow_premium = 0
        same_contract_count = 0
        aggressive_found = False
        valid_code_found = False
        valid_code_count = 0
        aggressive_code_count = 0

        # Per-signal cap: a single signal cannot accumulate unbounded flow bonus
        # from 40+ flow prints on the same ticker on a busy day.
        flow_loop_bonus = 0
        FLOW_LOOP_BONUS_CAP = int(CONFIG.get("flow_loop_bonus_cap", 8))

        for _, flow_row in flow_matches.iterrows():
            flow_premium = clean_number(get_flow_value(flow_row, "premium"))
            flow_trade = get_flow_trade_price(flow_row)
            flow_size = clean_number(get_flow_value(flow_row, "size"))

            if flow_premium <= 0 and flow_trade > 0 and flow_size > 0:
                flow_premium = flow_trade * flow_size * 100

            total_flow_premium += flow_premium

            flow_code = get_flow_code(flow_row)
            flow_strike = str(get_flow_value(flow_row, "strike")).strip()
            flow_expiry = get_row_expiry(flow_row, "flow")

            if flow_code in CONFIG["valid_flow_codes"]:
                valid_code_found = True
                valid_code_count += 1
                if valid_code_count <= 2:  # diminishing returns after 2
                    flow_loop_bonus += 1

            if flow_code in CONFIG["aggressive_flow_codes"]:
                aggressive_found = True
                aggressive_code_count += 1
                if aggressive_code_count <= 2:
                    flow_loop_bonus += 3

            if flow_strike == strike and flow_expiry == expiry:
                same_contract_count += 1
                if same_contract_count <= 3:
                    flow_loop_bonus += CONFIG["flow_same_contract_bonus"]

        # Apply the loop bonus subject to cap, then add cluster/aggregate bonuses.
        capped_loop_bonus = min(flow_loop_bonus, FLOW_LOOP_BONUS_CAP)
        if capped_loop_bonus > 0:
            score += capped_loop_bonus
            reasons.append(
                f"Flow loop bonus +{capped_loop_bonus} "
                f"(valid:{valid_code_count}, aggr:{aggressive_code_count}, "
                f"same-contract:{same_contract_count}, cap {FLOW_LOOP_BONUS_CAP})"
            )

        if total_flow_premium >= CONFIG["institutional_flow_premium"]:
            score += 5
            reasons.append("Flow premium $1M+")
        elif total_flow_premium >= CONFIG["large_flow_premium"]:
            score += 3
            reasons.append("Flow premium >= large threshold")

        if same_contract_count >= 2:
            score += 2
            reasons.append("Repeated same-contract flow")

        if aggressive_found:
            score += 2
            reasons.append("Aggressive institutional flow confirmed")

        if not valid_code_found:
            reasons.append("Flow confirmed, but code not classified as aggressive")
    else:
        reasons.append("No matching option-flow confirmation")
        if CONFIG["require_flow_confirmation_for_trade"]:
            score -= 3
            reasons.append("Flow confirmation required by config")

    # Price action confirmation from Yahoo
    price_confirmation = get_yahoo_price_confirmation(ticker, option_type)
    if price_confirmation.get("confirmed"):
        score += price_confirmation.get("score_bonus", 0)
        reasons.append(f"Price confirms direction: {price_confirmation.get('reason')}")
    else:
        reasons.append(f"Price not confirmed yet: {price_confirmation.get('reason')}")

    # Earnings risk warning
    earnings = get_earnings_risk(ticker)
    if earnings.get("earnings_near"):
        score -= 2
        reasons.append(f"Earnings risk: {earnings.get('reason')}")

    chain_result = analyze_option_chain(chain_df, row)

    if chain_result["decision"] == "REJECT":
        return "REJECT", score, [chain_result["reason"]]

    if chain_result["decision"] == "CAUTION":
        score -= 1
        reasons.append(chain_result["reason"])
    else:
        score += 1
        reasons.append("Option chain passed")

    # Decision thresholds
    if score >= CONFIG["high_conviction_score"]:
        return "HIGH_CONVICTION", score, reasons

    if score >= CONFIG["trade_score"]:
        return "TRADE", score, reasons

    if score >= CONFIG["watchlist_score"]:
        return "WATCHLIST", score, reasons

    return "IGNORE", score, reasons


def format_score_over_total(score, max_score):
    """Return score as score/total score for Telegram display."""
    score = clean_number(score)
    max_score = clean_number(max_score)

    if max_score <= 0:
        max_score = 1

    if float(score).is_integer() and float(max_score).is_integer():
        return f"{int(score)}/{int(max_score)}"

    return f"{round(score, 2)}/{round(max_score, 2)}"


def get_option_confidence_emoji(score, max_score=None, signal_type=None):
    """Visual priority marker for concise Telegram option alerts."""
    score = clean_number(score)
    max_score = clean_number(max_score or CONFIG.get("option_signal_max_score", 45))
    pct = score / max_score if max_score > 0 else 0

    if signal_type and str(signal_type).upper() in ["GAMMA", "STRADDLE", "VOLATILITY", "GAMMA_STRADDLE"]:
        return "⚡"
    if pct >= 0.80:
        return "🔥🔥"
    if pct >= 0.60:
        return "🔥"
    return "⚠️"


def format_money_compact(value):
    """Compact money formatter for Telegram alerts."""
    value = clean_number(value)
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    if value > 0:
        return f"${value:,.0f}"
    return "N/A"


def format_price_value(value):
    value = clean_number(value)
    if value > 0:
        return f"${round(value, 2)}"
    return "-"


def build_option_setup_bullets(signal=None, row=None, chain_result=None):
    """Short public-facing setup bullets. Full details remain in Excel/log."""
    bullets = []

    if signal:
        if signal.get("premium", 0) >= CONFIG.get("high_conviction_premium", 500000):
            bullets.append("Institutional-size premium")
        elif signal.get("premium", 0) >= CONFIG.get("min_premium", 100000):
            bullets.append("Unusual premium activity")

        if signal.get("confirmation_count", 0) >= CONFIG.get("trade_min_confirmations", 5):
            bullets.append("Multi-layer confirmation")

        pr = signal.get("price_reaction") or {}
        if isinstance(pr, dict) and pr.get("confirmed"):
            bullets.append("Price reaction confirmed")

        tv = signal.get("tradingview_confirmation") or signal.get("tv_confirmation") or {}
        if isinstance(tv, dict) and (tv.get("confirmed") or clean_number(tv.get("score")) >= CONFIG.get("tradingview_min_score", 7)):
            bullets.append("VWAP / structure aligned")

        uw = signal.get("unusual_whales_confirmation") or {}
        if isinstance(uw, dict) and uw.get("confirmed"):
            bullets.append("External flow confirmation")

    else:
        premium = clean_number(get_unusual_value(row, "premium")) if row is not None else 0
        if premium >= CONFIG.get("high_conviction_premium", 500000):
            bullets.append("Institutional-size premium")
        elif premium >= CONFIG.get("min_premium", 100000):
            bullets.append("Unusual premium activity")
        bullets.append("Options activity detected")
        if chain_result and chain_result.get("status") not in ["UNKNOWN", "DISABLED"]:
            bullets.append("Option chain reviewed")

    if not bullets:
        bullets = ["High-quality options setup", "Confirm price action before entry"]

    deduped = []
    for b in bullets:
        if b not in deduped:
            deduped.append(b)
    return deduped[:3]


def build_professional_option_signal_message(
    ticker,
    option_type,
    strike,
    expiry,
    entry,
    target,
    stop,
    premium,
    score,
    max_score,
    win_probability,
    position_size,
    category="TRADE",
    emoji="🔥",
    setup_bullets=None,
    is_gamma=False,
    chase_context=None,  # v17.4: live quote + chase metrics
):
    """Professional paid-channel Telegram format. Full detail remains in Excel/log."""
    brand = CONFIG.get("signal_brand_name", "Pal Trading Signals")
    category = str(category or "TRADE").upper()

    # v17.4: chase classification. Threshold from CONFIG.
    chase_warn_pct = float(CONFIG.get("chase_warn_pct", 15.0))
    chase_block_pct = float(CONFIG.get("chase_block_pct", 25.0))
    chase_severity = None
    chase_pct = None
    if chase_context and chase_context.get("available"):
        chase_pct = chase_context.get("chase_pct")
        if chase_pct is not None:
            if chase_pct >= chase_block_pct:
                chase_severity = "skip"
            elif chase_pct >= chase_warn_pct:
                chase_severity = "warn"
            else:
                chase_severity = "ok"

    if is_gamma:
        header = "⚡ Volatility / Gamma Setup"
    elif category == "WATCHLIST":
        header = "👀 Watchlist Setup"
    else:
        header = "🚀 Option Trade Alert"

    # v17.4: prepend visual flag for severe chase, before everything else.
    if chase_severity == "skip":
        header = "⚠️🛑 LATE ENTRY — likely skip\n" + header
    elif chase_severity == "warn":
        header = "⚠️ Late entry — consider waiting\n" + header

    lines = [
        f"{header}",
        f"{brand}",
        "",
        f"Ticker: {ticker}",
        f"Type: {option_type}",
        f"Strike: {strike}",
        f"Expiry: {expiry}",
        "",
        f"Entry: {format_price_value(entry)}",
        f"Target: {format_price_value(target)}",
        f"Stop Loss: {format_price_value(stop)}",
        "",
        f"Premium: {format_money_compact(premium)}",
        f"Score: {format_score_over_total(score, max_score)} {emoji}",
        f"Win Prob: ~{win_probability}%",
        f"Position Size: {position_size}",
    ]

    # v17.4: live-quote and chase block. Only render if we have a quote.
    if chase_context and chase_context.get("available"):
        lines.append("")
        lines.append("📡 Live Quote (just now):")
        bid = chase_context.get("current_bid")
        ask = chase_context.get("current_ask")
        mid = chase_context.get("current_mid")
        if bid is not None and ask is not None:
            lines.append(f"  Bid/Ask: {format_price_value(bid)} / {format_price_value(ask)}")
        if mid is not None:
            lines.append(f"  Mid: {format_price_value(mid)}")
        spread_pct = chase_context.get("spread_pct")
        if spread_pct is not None:
            spread_marker = ""
            if spread_pct >= 10:
                spread_marker = "  ⚠️ wide spread"
            lines.append(f"  Spread: {spread_pct}%{spread_marker}")

        if chase_pct is not None:
            if chase_severity == "skip":
                marker = "🛑 STOP CHASE"
            elif chase_severity == "warn":
                marker = "⚠️ caution"
            else:
                marker = "✅ within tolerance"
            sign = "+" if chase_pct >= 0 else ""
            lines.append(f"  Chase: {sign}{chase_pct}% from alert entry  {marker}")
    elif chase_context and not chase_context.get("available"):
        # We tried to get a live quote and couldn't — say so briefly.
        lines.append("")
        lines.append(f"📡 Live quote unavailable: {chase_context.get('reason', 'n/a')[:60]}")

    if CONFIG.get("include_signal_setup_bullets", True) and setup_bullets:
        lines.extend(["", "📊 Setup:"])
        lines.extend([f"• {b}" for b in setup_bullets[:3]])

    if CONFIG.get("include_signal_risk_block", True):
        lines.extend([
            "",
            "⚠️ Risk Management:",
            "• Use strict stop loss",
            "• Do not over-leverage",
            "• Avoid chasing extended moves",
        ])

    if CONFIG.get("include_signal_disclaimer", True):
        # v16: stronger multi-line disclaimer. See your securities attorney
        # about whether this is sufficient for paid distribution in your
        # jurisdiction — generic disclaimers do not always cover personalized
        # recommendations or jurisdictions like the US/UK/EU.
        full_disclaimer = CONFIG.get("signal_disclaimer_full")
        if full_disclaimer:
            lines.extend(["", "⚠️ Disclaimer:", full_disclaimer])
        else:
            lines.extend(["", f"⚠️ Disclaimer: {CONFIG.get('signal_disclaimer_short')}"])

    return "\n".join(lines).strip()


def estimate_option_win_probability(score, max_score=None):
    """
    Estimated probability band for alert readability.
    This is a model confidence estimate, not a guaranteed win rate.
    """
    score = clean_number(score)
    max_score = clean_number(max_score or CONFIG.get("option_signal_max_score", 45))
    if max_score <= 0:
        max_score = 1

    pct = max(0, min(score / max_score, 1))
    floor = int(CONFIG.get("option_win_prob_floor_pct", 52))
    ceiling = int(CONFIG.get("option_win_prob_ceiling_pct", 72))
    est = round(floor + ((ceiling - floor) * pct))
    return max(floor, min(est, ceiling))


def recommend_option_position_size(score, max_score=None):
    """
    Conservative account-risk sizing suggestion for option premium at risk.
    Example: 0.5% means do not risk more than 0.5% of account equity on the trade.
    """
    score = clean_number(score)
    max_score = clean_number(max_score or CONFIG.get("option_signal_max_score", 45))
    pct = score / max_score if max_score > 0 else 0

    low = CONFIG.get("option_position_size_low_pct", 0.25)
    medium = CONFIG.get("option_position_size_medium_pct", 0.50)
    high = CONFIG.get("option_position_size_high_pct", 1.00)

    if pct >= 0.80:
        return f"{high}% max account risk"
    if pct >= 0.60:
        return f"{medium}% max account risk"
    return f"{low}% max account risk"


def build_option_telegram_message(row, final_decision, score, reasons, chain_result):
    """Professional Telegram option recommendation format. Full detail remains in Excel/log."""
    ticker = str(get_unusual_value(row, "symbol")).upper()
    option_type_raw = str(get_unusual_value(row, "type")).upper()
    option_type = "CALL" if "CALL" in option_type_raw or option_type_raw == "C" else "PUT"
    opt_side = "C" if option_type == "CALL" else "P"

    strike = get_unusual_value(row, "strike")
    expiry = str(get_unusual_value(row, "expiration"))
    last = clean_number(get_unusual_value(row, "last"))
    bid = clean_number(get_unusual_value(row, "bid"))
    ask = clean_number(get_unusual_value(row, "ask"))
    premium = clean_number(get_unusual_value(row, "premium"))

    if last <= 0 and bid > 0 and ask > 0:
        last = round((bid + ask) / 2, 2)

    entry = round(last, 2) if last else 0
    target = round(last * CONFIG["option_target_multiplier"], 2) if last else 0
    stop = round(last * CONFIG["option_stop_loss_multiplier"], 2) if last else 0
    max_score = CONFIG.get("option_signal_max_score", 45)
    emoji = get_option_confidence_emoji(score, max_score)
    win_probability = estimate_option_win_probability(score, max_score)
    position_size = recommend_option_position_size(score, max_score)
    category = "WATCHLIST" if final_decision == "WATCHLIST" else "TRADE"
    setup_bullets = build_option_setup_bullets(row=row, chain_result=chain_result)

    return build_professional_option_signal_message(
        ticker=ticker,
        option_type=option_type,
        strike=f"{strike}{opt_side}",
        expiry=expiry,
        entry=entry,
        target=target,
        stop=stop,
        premium=premium,
        score=score,
        max_score=max_score,
        win_probability=win_probability,
        position_size=position_size,
        category=category,
        emoji=emoji,
        setup_bullets=setup_bullets,
    )

def build_watchlist_telegram_message(watchlist_items):
    lines = []

    for item in watchlist_items:
        row = item["row"]

        ticker = str(get_unusual_value(row, "symbol")).upper()
        option_type = str(get_unusual_value(row, "type")).upper()
        strike = get_unusual_value(row, "strike")
        expiry = str(get_unusual_value(row, "expiration"))
        last = clean_number(get_unusual_value(row, "last"))

        opt_side = "C" if "CALL" in option_type else "P"
        short_expiry = expiry[:5] if len(expiry) >= 5 else expiry

        lines.append(
            f"Option Watchlist: {ticker} {strike}{opt_side} {short_expiry} | Entry Area: ${round(last, 2) if last else '-'}"
        )

    return "\n".join(lines)
    
def was_option_already_sent_today(signal_key):
    if not os.path.exists(CONFIG["excel_file"]):
        return False

    try:
        wb = load_workbook(CONFIG["excel_file"])
        if "OptionSignals" not in wb.sheetnames:
            return False

        ws = wb["OptionSignals"]

        today = get_market_now().strftime("%Y-%m-%d")

        for row in ws.iter_rows(min_row=2, values_only=True):
            dt = str(row[0])
            ticker = str(row[1])
            option_type = str(row[4])
            strike = str(row[5])
            expiry = str(row[6])

            historical_key = f"OPTION_{ticker}_{expiry}_{strike}_{option_type}_{today}"

            if historical_key == signal_key:
                return True

        return False

    except Exception as e:
        print("Duplicate Excel check error:", e)
        return False
        

# ============================================================
# GAMMA / STRADDLE SETUP DETECTION
# ============================================================

def parse_expiry_to_dte(expiry):
    """
    Parses Barchart expiry values and returns calendar DTE.
    Supports common Barchart formats like 05/08/26 and 05/08/2026.
    """
    try:
        expiry_text = str(expiry).strip()
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                expiry_date = datetime.strptime(expiry_text, fmt).date()
                return (expiry_date - get_market_now().date()).days
            except:
                continue
        return None
    except:
        return None


def make_gamma_signal_key(item):
    ticker = str(item.get("ticker", "")).upper().strip()
    expiry = str(item.get("expiry", "")).strip()
    strike_zone = str(item.get("strike_zone", "")).strip()
    return f"GAMMA_{ticker}_{expiry}_{strike_zone}"


def was_gamma_already_sent_today(signal_key):
    """
    Checks the OptionSignals sheet for prior GAMMA_SETUP alerts today.
    This prevents duplicate gamma/straddle alerts after script restart.
    """
    if not os.path.exists(CONFIG["excel_file"]):
        return False

    try:
        wb = load_workbook(CONFIG["excel_file"])
        if "OptionSignals" not in wb.sheetnames:
            return False

        ws = wb["OptionSignals"]
        today = get_market_now().strftime("%Y-%m-%d")

        for row in ws.iter_rows(min_row=2, values_only=True):
            dt = str(row[0])
            ticker = str(row[1])
            decision = str(row[2])
            strike = str(row[5])
            expiry = str(row[6])

            if not dt.startswith(today):
                continue

            historical_key = f"GAMMA_{ticker}_{expiry}_{strike}"

            if decision == "GAMMA_SETUP" and historical_key == signal_key:
                return True

        return False

    except Exception as e:
        print("Gamma duplicate Excel check error:", e)
        return False


def detect_gamma_setups(unusual_df, flow_df=None):
    """
    Detects institutional gamma / straddle style setups:
    - Same ticker
    - Same expiry
    - Heavy CALL and PUT activity near the same strike zone
    - High total premium / volume
    - Strong volume vs open interest where available

    Output is not a directional call/put recommendation.
    It is a volatility setup: big move expected, direction unknown.
    """
    setups = []

    if unusual_df is None or unusual_df.empty:
        return setups

    normalized_rows = []

    for _, row in unusual_df.iterrows():
        try:
            ticker = str(get_unusual_value(row, "symbol")).upper().strip()
            option_type = str(get_unusual_value(row, "type")).lower().strip()
            expiry = str(get_unusual_value(row, "expiration")).strip()
            strike = clean_number(get_unusual_value(row, "strike"))
            volume = clean_number(get_unusual_value(row, "volume"))
            oi = clean_number(get_unusual_value(row, "open_interest"))
            premium = clean_number(get_unusual_value(row, "premium"))
            last = clean_number(get_unusual_value(row, "last"))
            bid = clean_number(get_unusual_value(row, "bid"))
            ask = clean_number(get_unusual_value(row, "ask"))
            stock_price = clean_number(get_unusual_value(row, "price"))
            dte = parse_expiry_to_dte(expiry)

            if not ticker or ticker == "NAN":
                continue

            if dte is None:
                dte = int(clean_number(get_unusual_value(row, "dte")))

            if dte < CONFIG["gamma_min_dte"] or dte > CONFIG["gamma_max_dte"]:
                continue

            if strike <= 0:
                continue

            is_call = "call" in option_type
            is_put = "put" in option_type

            if not is_call and not is_put:
                continue

            vol_oi = volume / oi if oi > 0 else volume

            normalized_rows.append({
                "row": row,
                "ticker": ticker,
                "type": "CALL" if is_call else "PUT",
                "expiry": expiry,
                "strike": strike,
                "volume": volume,
                "oi": oi,
                "vol_oi": vol_oi,
                "premium": premium,
                "last": last,
                "bid": bid,
                "ask": ask,
                "stock_price": stock_price,
                "dte": dte
            })

        except Exception as e:
            print("Gamma normalization row error:", e)
            continue

    if not normalized_rows:
        return setups

    df = pd.DataFrame(normalized_rows)

    for (ticker, expiry), group in df.groupby(["ticker", "expiry"]):
        calls = group[group["type"] == "CALL"].copy()
        puts = group[group["type"] == "PUT"].copy()

        if calls.empty or puts.empty:
            continue

        best_candidates = []

        for _, call in calls.iterrows():
            for _, put in puts.iterrows():
                avg_strike = (float(call["strike"]) + float(put["strike"])) / 2
                if avg_strike <= 0:
                    continue

                strike_distance_pct = abs(float(call["strike"]) - float(put["strike"])) / avg_strike * 100

                if strike_distance_pct > CONFIG["gamma_strike_tolerance_pct"]:
                    continue

                call_volume = float(call["volume"])
                put_volume = float(put["volume"])
                call_premium = float(call["premium"])
                put_premium = float(put["premium"])
                total_volume = call_volume + put_volume
                total_premium = call_premium + put_premium
                call_vol_oi = float(call["vol_oi"])
                put_vol_oi = float(put["vol_oi"])

                if call_volume < CONFIG["gamma_min_call_volume"]:
                    continue
                if put_volume < CONFIG["gamma_min_put_volume"]:
                    continue
                if total_volume < CONFIG["gamma_min_total_volume"]:
                    continue
                if call_premium < CONFIG["gamma_min_call_premium"]:
                    continue
                if put_premium < CONFIG["gamma_min_put_premium"]:
                    continue
                if total_premium < CONFIG["gamma_min_total_premium"]:
                    continue
                if call_vol_oi < CONFIG["gamma_min_call_vol_oi"]:
                    continue
                if put_vol_oi < CONFIG["gamma_min_put_vol_oi"]:
                    continue

                score = 0
                reasons = []

                score += 4
                reasons.append("CALL and PUT activity detected near same strike/expiry")

                if strike_distance_pct <= 1:
                    score += 3
                    reasons.append("Very tight strike zone")

                if total_premium >= CONFIG["high_conviction_premium"]:
                    score += 4
                    reasons.append("High combined premium")
                elif total_premium >= CONFIG["strong_premium"]:
                    score += 3
                    reasons.append("Strong combined premium")
                else:
                    score += 2
                    reasons.append("Good combined premium")

                if total_volume >= CONFIG["option_volume_very_high"]:
                    score += 3
                    reasons.append("Very high combined option volume")
                elif total_volume >= CONFIG["option_volume_high"]:
                    score += 2
                    reasons.append("High combined option volume")

                if call_vol_oi >= CONFIG["option_vol_oi_aggressive"] and put_vol_oi >= CONFIG["option_vol_oi_aggressive"]:
                    score += 4
                    reasons.append("Aggressive Vol/OI on both sides")
                elif call_vol_oi >= CONFIG["option_vol_oi_strong"] and put_vol_oi >= CONFIG["option_vol_oi_strong"]:
                    score += 3
                    reasons.append("Strong Vol/OI on both sides")
                else:
                    score += 1
                    reasons.append("Both sides meet minimum Vol/OI")

                dte = int(min(call["dte"], put["dte"]))

                if CONFIG["option_sweet_spot_min_dte"] <= dte <= CONFIG["option_sweet_spot_max_dte"]:
                    score += 2
                    reasons.append("DTE in institutional swing sweet spot")
                elif dte <= 7:
                    score += 1
                    reasons.append("Short-dated gamma setup")

                flow_confirmed = False
                if flow_df is not None and not flow_df.empty:
                    flow_has_call = False
                    flow_has_put = False

                    for _, flow_row in flow_df.iterrows():
                        flow_ticker = str(get_flow_value(flow_row, "symbol")).upper().strip()
                        flow_type = str(get_flow_value(flow_row, "type")).lower()
                        flow_text = " ".join([str(x).lower() for x in flow_row.values])

                        if flow_ticker != ticker:
                            continue

                        if "call" in flow_type or "call" in flow_text:
                            flow_has_call = True

                        if "put" in flow_type or "put" in flow_text:
                            flow_has_put = True

                    if flow_has_call and flow_has_put:
                        flow_confirmed = True
                        score += 3
                        reasons.append("Options flow confirms both CALL and PUT activity")
                    elif flow_has_call or flow_has_put:
                        flow_confirmed = True
                        score += 1
                        reasons.append("Options flow confirms ticker activity")

                if score < CONFIG["gamma_min_score"]:
                    continue

                call_entry = float(call["ask"]) if float(call["ask"]) > 0 else float(call["last"])
                put_entry = float(put["ask"]) if float(put["ask"]) > 0 else float(put["last"])
                straddle_entry = call_entry + put_entry if call_entry > 0 and put_entry > 0 else 0

                if abs(float(call["strike"]) - float(put["strike"])) < 0.01:
                    strike_zone = str(round(avg_strike, 2))
                else:
                    strike_zone = f"{round(float(put['strike']), 2)}-{round(float(call['strike']), 2)}"

                best_candidates.append({
                    "ticker": ticker,
                    "expiry": expiry,
                    "strike_zone": strike_zone,
                    "avg_strike": round(avg_strike, 2),
                    "call_strike": float(call["strike"]),
                    "put_strike": float(put["strike"]),
                    "call_volume": int(call_volume),
                    "put_volume": int(put_volume),
                    "total_volume": int(total_volume),
                    "call_premium": round(call_premium, 2),
                    "put_premium": round(put_premium, 2),
                    "total_premium": round(total_premium, 2),
                    "call_vol_oi": round(call_vol_oi, 2),
                    "put_vol_oi": round(put_vol_oi, 2),
                    "dte": dte,
                    "score": score,
                    "reasons": reasons,
                    "flow_confirmed": flow_confirmed,
                    "call_entry": round(call_entry, 2) if call_entry else 0,
                    "put_entry": round(put_entry, 2) if put_entry else 0,
                    "straddle_entry": round(straddle_entry, 2) if straddle_entry else 0,
                    "stock_price": float(call["stock_price"]) if float(call["stock_price"]) > 0 else float(put["stock_price"]),
                    "call_row": call["row"],
                    "put_row": put["row"],
                })

        if best_candidates:
            best = sorted(best_candidates, key=lambda x: x["score"], reverse=True)[0]
            setups.append(best)

    setups = sorted(setups, key=lambda x: x["score"], reverse=True)
    return setups


def build_gamma_telegram_message(item):
    entry = clean_number(item.get("straddle_entry", 0))
    if entry and entry > 0:
        target_low = round(entry * (1 + CONFIG["gamma_target_pct_low"] / 100), 2)
        target_high = round(entry * (1 + CONFIG["gamma_target_pct_high"] / 100), 2)
        target = round((target_low + target_high) / 2, 2)
        stop = round(entry * (1 - CONFIG["gamma_stop_loss_pct"] / 100), 2)
    else:
        target = 0
        stop = 0

    max_score = CONFIG.get("option_signal_max_score", 45)
    score = clean_number(item.get("score", 0))
    emoji = get_option_confidence_emoji(score, max_score, "GAMMA")
    win_probability = estimate_option_win_probability(score, max_score)
    position_size = recommend_option_position_size(score, max_score)
    setup_bullets = [
        "Two-sided institutional activity",
        "Gamma / volatility positioning",
        "Wait for breakout if spread is wide",
    ]

    return build_professional_option_signal_message(
        ticker=item.get("ticker", ""),
        option_type="STRADDLE / GAMMA",
        strike=item.get("strike_zone", "ATM"),
        expiry=item.get("expiry", ""),
        entry=entry,
        target=target,
        stop=stop,
        premium=clean_number(item.get("total_premium", 0)),
        score=score,
        max_score=max_score,
        win_probability=win_probability,
        position_size=position_size,
        category="TRADE",
        emoji=emoji,
        setup_bullets=setup_bullets,
        is_gamma=True,
    )

def save_gamma_signal_to_excel(item):
    init_excel()

    wb = load_workbook(CONFIG["excel_file"])
    ws = wb["OptionSignals"]

    ws.append([
        get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        item["ticker"],
        "GAMMA_SETUP",
        item["score"],
        "STRADDLE",
        item["strike_zone"],
        item["expiry"],
        item.get("straddle_entry", ""),
        round(item.get("straddle_entry", 0) * (1 - CONFIG["gamma_stop_loss_pct"] / 100), 2) if item.get("straddle_entry", 0) else "",
        round(item.get("straddle_entry", 0) * (1 + CONFIG["gamma_target_pct_low"] / 100), 2) if item.get("straddle_entry", 0) else "",
        round(item.get("straddle_entry", 0) * (1 + CONFIG["gamma_target_pct_high"] / 100), 2) if item.get("straddle_entry", 0) else "",
        item["total_premium"],
        item["total_volume"],
        "",
        f"C:{item['call_vol_oi']} / P:{item['put_vol_oi']}",
        item["dte"],
        "GAMMA_DETECTED",
        "-",
        "-",
        ", ".join(item["reasons"]),
        "OPEN",
        "",
        "",
        ""
    ])

    wb.save(CONFIG["excel_file"])



# ============================================================
# FULL INSTITUTIONAL OPTION DECISION ENGINE
# ============================================================

def calculate_unusual_premium(row):
    """Returns premium from CSV or estimates premium from option price * volume * 100."""
    premium = clean_number(get_unusual_value(row, "premium"))
    if premium > 0:
        return premium

    last = clean_number(get_unusual_value(row, "last"))
    bid = clean_number(get_unusual_value(row, "bid"))
    ask = clean_number(get_unusual_value(row, "ask"))
    volume = clean_number(get_unusual_value(row, "volume"))

    if last <= 0 and bid > 0 and ask > 0:
        last = (bid + ask) / 2
    elif last <= 0 and ask > 0:
        last = ask

    if last > 0 and volume > 0:
        return last * volume * 100

    return 0.0


def calculate_flow_premium(row):
    """Returns premium from Flow CSV or estimates premium from trade * size * 100."""
    premium = clean_number(get_flow_value(row, "premium"))
    if premium > 0:
        return premium

    trade = get_flow_trade_price(row)
    size = clean_number(get_flow_value(row, "size"))
    if trade > 0 and size > 0:
        return trade * size * 100

    return 0.0


def option_entry_from_unusual(row):
    ask = clean_number(get_unusual_value(row, "ask"))
    last = clean_number(get_unusual_value(row, "last"))
    bid = clean_number(get_unusual_value(row, "bid"))
    if ask > 0:
        return ask
    if last > 0:
        return last
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    return 0.0


def option_entry_from_flow(row):
    trade = get_flow_trade_price(row)
    if trade > 0:
        return trade
    return 0.0


def normalize_unusual_records(unusual_df):
    records = []
    if unusual_df is None or unusual_df.empty:
        return records

    for _, row in unusual_df.iterrows():
        ticker = str(get_unusual_value(row, "symbol")).upper().strip()
        option_type = str(get_unusual_value(row, "type")).upper().strip()
        if option_type == "C":
            option_type = "CALL"
        if option_type == "P":
            option_type = "PUT"
        if ticker in ["", "NAN"] or option_type not in ["CALL", "PUT"]:
            continue

        volume = clean_number(get_unusual_value(row, "volume"))
        oi = clean_number(get_unusual_value(row, "open_interest"))
        vol_oi_raw = clean_number(get_unusual_value(row, "vol_oi"))
        vol_oi = vol_oi_raw if vol_oi_raw > 0 else (volume / oi if oi > 0 else volume)
        entry = option_entry_from_unusual(row)
        dte = get_row_dte(row, "unusual")
        premium = calculate_unusual_premium(row)

        records.append({
            "source": "UNUSUAL",
            "row": row,
            "ticker": ticker,
            "price": clean_number(get_unusual_value(row, "price")),
            "option_type": option_type,
            "direction": "BULLISH" if option_type == "CALL" else "BEARISH",
            "strike": clean_number(get_unusual_value(row, "strike")),
            "expiry": get_row_expiry(row, "unusual"),
            "dte": dte,
            "entry": entry,
            "premium": premium,
            "volume": volume,
            "oi": oi,
            "vol_oi": vol_oi,
            "delta": clean_number(get_unusual_value(row, "delta")),
            "iv": clean_number(get_unusual_value(row, "iv")),
            "code": "UNUSUAL",
        })
    return records


def normalize_flow_records(flow_df):
    records = []
    if flow_df is None or flow_df.empty:
        return records

    for _, row in flow_df.iterrows():
        ticker = str(get_flow_value(row, "symbol")).upper().strip()
        option_type = str(get_flow_value(row, "type")).upper().strip()
        if option_type == "C":
            option_type = "CALL"
        if option_type == "P":
            option_type = "PUT"
        if ticker in ["", "NAN"] or option_type not in ["CALL", "PUT"]:
            continue

        volume = clean_number(get_flow_value(row, "volume"))
        oi = clean_number(get_flow_value(row, "open_interest"))
        vol_oi = volume / oi if oi > 0 else volume
        premium = calculate_flow_premium(row)
        entry = option_entry_from_flow(row)
        dte = get_row_dte(row, "flow")
        code = get_flow_code(row)

        records.append({
            "source": "FLOW",
            "row": row,
            "ticker": ticker,
            "price": clean_number(get_flow_value(row, "price")),
            "option_type": option_type,
            "direction": "BULLISH" if option_type == "CALL" else "BEARISH",
            "strike": clean_number(get_flow_value(row, "strike")),
            "expiry": get_row_expiry(row, "flow"),
            "dte": dte,
            "entry": entry,
            "premium": premium,
            "volume": volume,
            "oi": oi,
            "vol_oi": vol_oi,
            "delta": clean_number(get_flow_value(row, "delta")),
            "iv": clean_number(get_flow_value(row, "iv")),
            "code": code,
        })
    return records


def build_ticker_institutional_bias(unusual_records, flow_records):
    """Aggregates CALL vs PUT activity by ticker so TSLA-style mixed activity is not treated as a blind directional trade."""
    bias = {}
    for rec in unusual_records + flow_records:
        ticker = rec["ticker"]
        if ticker not in bias:
            bias[ticker] = {
                "call_premium": 0.0,
                "put_premium": 0.0,
                "call_volume": 0.0,
                "put_volume": 0.0,
                "call_count": 0,
                "put_count": 0,
                "has_unusual": False,
                "has_flow": False,
            }

        side = "call" if rec["option_type"] == "CALL" else "put"
        bias[ticker][f"{side}_premium"] += rec.get("premium", 0) or 0
        bias[ticker][f"{side}_volume"] += rec.get("volume", 0) or 0
        bias[ticker][f"{side}_count"] += 1
        if rec["source"] == "UNUSUAL":
            bias[ticker]["has_unusual"] = True
        if rec["source"] == "FLOW":
            bias[ticker]["has_flow"] = True

    for ticker, b in bias.items():
        total_premium = b["call_premium"] + b["put_premium"]
        total_volume = b["call_volume"] + b["put_volume"]
        call_share = b["call_premium"] / total_premium if total_premium > 0 else (b["call_volume"] / total_volume if total_volume > 0 else 0)
        put_share = 1 - call_share if total_premium > 0 or total_volume > 0 else 0

        mixed = (
            b["call_count"] > 0 and b["put_count"] > 0 and
            total_premium >= CONFIG["ticker_bias_min_total_premium"] and
            total_volume >= CONFIG["ticker_bias_min_total_volume"] and
            call_share < CONFIG["mixed_direction_ratio"] and
            put_share < CONFIG["mixed_direction_ratio"]
        )

        if mixed:
            direction = "MIXED_GAMMA"
        elif call_share >= CONFIG["mixed_direction_ratio"]:
            direction = "BULLISH"
        elif put_share >= CONFIG["mixed_direction_ratio"]:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

        b["total_premium"] = total_premium
        b["total_volume"] = total_volume
        b["call_share"] = round(call_share, 2)
        b["put_share"] = round(put_share, 2)
        b["bias"] = direction

    return bias


def same_contract(a, b):
    if a["ticker"] != b["ticker"]:
        return False
    if a["option_type"] != b["option_type"]:
        return False
    if abs(clean_number(a["strike"]) - clean_number(b["strike"])) > 0.01:
        return False
    return str(a["expiry"]).strip() == str(b["expiry"]).strip()




def is_option_quality_window():
    """
    Accuracy filter: option alerts are highest quality during the strongest intraday windows.
    Default allowed windows:
    - 9:45 AM to 11:00 AM ET
    - 2:00 PM to 3:30 PM ET
    """
    if not CONFIG.get("enable_option_time_filter", True):
        return True, "Option time filter disabled"

    now = get_market_now().time()
    morning_start = dt_time(CONFIG["option_morning_start_hour"], CONFIG["option_morning_start_minute"])
    morning_end = dt_time(CONFIG["option_morning_end_hour"], CONFIG["option_morning_end_minute"])
    afternoon_start = dt_time(CONFIG["option_afternoon_start_hour"], CONFIG["option_afternoon_start_minute"])
    afternoon_end = dt_time(CONFIG["option_afternoon_end_hour"], CONFIG["option_afternoon_end_minute"])

    if morning_start <= now <= morning_end:
        return True, "Morning institutional window"
    if afternoon_start <= now <= afternoon_end:
        return True, "Afternoon institutional window"

    return False, "Outside preferred option windows"


def get_option_market_regime_alignment(option_type):
    """
    Accuracy filter: align option direction with SPY/QQQ regime.
    CALLs prefer bullish SPY+QQQ regime. PUTs prefer bearish SPY+QQQ regime.
    """
    try:
        regime = get_market_regime()
        is_call = str(option_type).upper() == "CALL"
        is_put = str(option_type).upper() == "PUT"

        if is_call and regime.get("bullish"):
            return {"aligned": True, "known": True, "reason": "SPY/QQQ bullish regime supports CALL"}
        if is_put and regime.get("bearish"):
            return {"aligned": True, "known": True, "reason": "SPY/QQQ bearish regime supports PUT"}
        if regime.get("bullish") or regime.get("bearish"):
            return {"aligned": False, "known": True, "reason": "SPY/QQQ regime conflicts with option direction"}

        return {"aligned": CONFIG.get("allow_neutral_market_regime", False), "known": False, "reason": "SPY/QQQ regime neutral or unavailable"}

    except Exception as e:
        return {"aligned": True, "known": False, "reason": f"Market regime check unavailable: {e}"}


def get_effective_option_thresholds():
    """Strict/high-probability mode raises thresholds without changing older .env keys."""
    strict = CONFIG.get("enable_strict_signal_filtering", True)
    high_prob = CONFIG.get("enable_high_probability_mode", True)

    min_premium = CONFIG.get("min_premium", 0)
    trade_score = CONFIG.get("option_trade_score", CONFIG.get("trade_score", 8))
    high_score = CONFIG.get("option_high_conviction_score", CONFIG.get("high_conviction_score", 14))
    price_score = CONFIG.get("price_reaction_min_score", 4)
    max_chase = CONFIG.get("price_reaction_max_chase_pct", 3.5)

    if strict:
        min_premium = max(min_premium, CONFIG.get("strict_min_premium", 0))
        trade_score = max(trade_score, CONFIG.get("strict_option_trade_score", 12))
        high_score = max(high_score, CONFIG.get("strict_option_high_conviction_score", 16))
        price_score = max(price_score, CONFIG.get("strict_price_reaction_min_score", 5))
        max_chase = min(max_chase, CONFIG.get("strict_price_reaction_max_chase_pct", 2.5))

    if high_prob:
        min_premium = max(min_premium, CONFIG.get("high_prob_min_premium", 300000))
        trade_score = max(trade_score, CONFIG.get("high_prob_min_score", 18))
        high_score = max(high_score, CONFIG.get("high_prob_min_score", 18) + 2)
        price_score = max(price_score, CONFIG.get("high_prob_min_price_reaction_score", 6))
        max_chase = min(max_chase, CONFIG.get("high_prob_max_chase_pct", 2.0))

    return {
        "min_premium": min_premium,
        "trade_score": trade_score,
        "high_conviction_score": high_score,
        "price_reaction_min_score": price_score,
        "max_chase_pct": max_chase,
    }

def get_enhanced_price_reaction(ticker, option_type):
    """
    TradingView-style price/structure confirmation using Yahoo intraday OHLCV.

    Checks the same items you would manually review on TradingView:
    VWAP alignment, support/resistance breakout, EMA 9/21 trend,
    volume expansion, candle close quality, no-chase distance, and chop filter.
    """
    if not CONFIG.get("enable_realtime_price_reaction", True):
        return {
            "enabled": False,
            "confirmed": False,
            "tradingview_confirmed": False,
            "score": 0,
            "tradingview_score": 0,
            "price": None,
            "vwap": None,
            "chase_pct": None,
            "reason": "Price-reaction confirmation disabled",
        }

    try:
        # v17: route intraday OHLCV through schwab_data.get_intraday_bars(),
        # which selects Schwab/Yahoo/auto based on CONFIG["intraday_data_source"]
        # and falls back to Yahoo silently in auto mode if Schwab fails.
        try:
            from schwab_data import get_intraday_bars
            df = get_intraday_bars(
                ticker,
                period=CONFIG.get("price_reaction_period", "1d"),
                interval=CONFIG.get("price_reaction_interval", "1m"),
            )
        except ImportError:
            # schwab_data module missing or schwab-py uninstalled: hard fallback to Yahoo.
            df = yf.Ticker(ticker).history(
                period=CONFIG.get("price_reaction_period", "1d"),
                interval=CONFIG.get("price_reaction_interval", "1m"),
                auto_adjust=False,
            )

        # v17: stale-data guard. If the most recent bar is older than the
        # configured threshold, skip the structure check entirely rather
        # than score on stale data. This is the second-largest accuracy
        # win after switching to Schwab for fresh data.
        if df is not None and not df.empty:
            try:
                last_bar_time = df.index[-1]
                if last_bar_time.tzinfo is None:
                    last_bar_time = pd.Timestamp(last_bar_time).tz_localize("UTC")
                now_ts = pd.Timestamp.now(tz=last_bar_time.tzinfo)
                data_age_seconds = (now_ts - last_bar_time).total_seconds()
                stale_threshold = CONFIG.get("intraday_stale_threshold_seconds", 300)
                if data_age_seconds > stale_threshold:
                    return {
                        "enabled": True,
                        "confirmed": False,
                        "tradingview_confirmed": False,
                        "score": 0,
                        "tradingview_score": 0,
                        "price": None,
                        "vwap": None,
                        "chase_pct": None,
                        "reason": (
                            f"Intraday data stale ({data_age_seconds:.0f}s old, "
                            f"threshold {stale_threshold}s) — skipping structure check"
                        ),
                    }
            except Exception:
                # If the staleness check itself errors, don't block the signal.
                pass

        if df is None or df.empty or len(df) < 30:
            return {
                "enabled": True,
                "confirmed": False,
                "tradingview_confirmed": False,
                "score": 0,
                "tradingview_score": 0,
                "price": None,
                "vwap": None,
                "chase_pct": None,
                "reason": "Insufficient intraday price data",
            }

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=["Close"])
        if df.empty or len(df) < 30:
            return {
                "enabled": True,
                "confirmed": False,
                "tradingview_confirmed": False,
                "score": 0,
                "tradingview_score": 0,
                "price": None,
                "vwap": None,
                "chase_pct": None,
                "reason": "No valid intraday close data",
            }

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col not in df.columns:
                df[col] = 0 if col == "Volume" else df["Close"]

        price = float(df["Close"].iloc[-1])
        open_price = float(df["Open"].dropna().iloc[0]) if "Open" in df else price
        day_high = float(df["High"].max())
        day_low = float(df["Low"].min())

        recent_bars = max(5, int(CONFIG.get("price_reaction_recent_bars", 15)))
        recent = df.tail(recent_bars)

        lookback = max(10, int(CONFIG.get("tv_breakout_lookback_bars", 30)))
        structure_window = df.iloc[-lookback-1:-1] if len(df) > lookback + 1 else df.iloc[:-1]
        if structure_window.empty:
            structure_window = df.iloc[:-1]

        volume = df["Volume"].fillna(0)
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
        vwap = float((typical_price * volume).sum() / volume.sum()) if volume.sum() > 0 else price

        recent_high = float(recent["High"].max())
        recent_low = float(recent["Low"].min())
        structure_high = float(structure_window["High"].max()) if not structure_window.empty else recent_high
        structure_low = float(structure_window["Low"].min()) if not structure_window.empty else recent_low

        recent_volume_avg = float(volume.tail(recent_bars).mean()) if len(volume) >= recent_bars else 0
        base_volume_avg = float(volume.iloc[:-recent_bars].mean()) if len(volume) > recent_bars else float(volume.mean())
        volume_multiplier = CONFIG.get("tv_volume_expansion_multiplier", 1.35)
        volume_expansion = recent_volume_avg > base_volume_avg * volume_multiplier if base_volume_avg > 0 else False

        ema_fast_len = int(CONFIG.get("tv_ema_fast", 9))
        ema_slow_len = int(CONFIG.get("tv_ema_slow", 21))
        ema_fast = df["Close"].ewm(span=ema_fast_len, adjust=False).mean()
        ema_slow = df["Close"].ewm(span=ema_slow_len, adjust=False).mean()
        ema_fast_now = float(ema_fast.iloc[-1])
        ema_slow_now = float(ema_slow.iloc[-1])
        ema_fast_prev = float(ema_fast.iloc[-5]) if len(ema_fast) >= 5 else ema_fast_now

        # EMA-21 needs at least ~2x its span to be stable. Below that, trend
        # signal is too noisy to use — record but do not score.
        ema_stable = len(df) >= ema_slow_len * 2

        # Recent (last N bars) VWAP. If session VWAP says "above" but recent
        # VWAP says "below", the stock is rolling over — common afternoon trap
        # the original code missed.
        recent_typical = (recent["High"] + recent["Low"] + recent["Close"]) / 3
        recent_vol = recent["Volume"].fillna(0)
        if recent_vol.sum() > 0:
            recent_vwap = float((recent_typical * recent_vol).sum() / recent_vol.sum())
        else:
            recent_vwap = vwap

        last_open = float(df["Open"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        candle_range = max(last_high - last_low, 0.0001)
        candle_body_pct = abs(last_close - last_open) / candle_range * 100
        close_location_pct = (last_close - last_low) / candle_range * 100

        is_call = str(option_type).upper() == "CALL" or "call" in str(option_type).lower()
        is_put = str(option_type).upper() == "PUT" or "put" in str(option_type).lower()

        score = 0
        reasons = []
        vwap_aligned = False
        breakout_aligned = False
        trend_aligned = False
        ema_aligned = False
        candle_confirmed = False
        structure_confirmed = False

        tolerance = CONFIG.get("tv_structure_tolerance_pct", 0.25) / 100
        min_body_pct = CONFIG.get("tv_min_body_pct", 35)

        if is_call:
            # VWAP: require price ABOVE session VWAP AND above recent-bars VWAP
            # to avoid the "above session VWAP but rolling over" trap.
            if price >= vwap and price >= recent_vwap:
                vwap_aligned = True
                score += CONFIG["price_reaction_vwap_bonus"]
                reasons.append("TV: above session+recent VWAP")
            elif price >= vwap:
                reasons.append("TV: above session VWAP but rolling over (recent VWAP higher)")
            else:
                reasons.append("TV: below VWAP")

            # Breakout tolerance only counts on the BREAKOUT side, not below.
            # Old bug: 0.25% BELOW resistance was being called "breaking" resistance.
            if price >= structure_high * (1 + tolerance):
                breakout_aligned = True
                structure_confirmed = True
                score += CONFIG["price_reaction_breakout_bonus"]
                reasons.append("TV: confirmed breakout above resistance")
            elif price >= structure_high:
                breakout_aligned = True
                structure_confirmed = True
                score += max(1, CONFIG["price_reaction_breakout_bonus"] - 1)
                reasons.append("TV: at resistance (early breakout)")

            if ema_stable and ema_fast_now > ema_slow_now and ema_fast_now >= ema_fast_prev:
                ema_aligned = True
                trend_aligned = True
                score += 2
                reasons.append("TV: EMA 9/21 bullish")
            elif not ema_stable:
                reasons.append("TV: EMA trend not yet stable (early session)")

            if close_location_pct >= 60 and candle_body_pct >= min_body_pct:
                candle_confirmed = True
                score += 1
                reasons.append("TV: bullish candle close")

            if price >= day_high * 0.997:
                score += 1
                reasons.append("TV: near day high")

        elif is_put:
            if price <= vwap and price <= recent_vwap:
                vwap_aligned = True
                score += CONFIG["price_reaction_vwap_bonus"]
                reasons.append("TV: below session+recent VWAP")
            elif price <= vwap:
                reasons.append("TV: below session VWAP but bouncing (recent VWAP lower)")
            else:
                reasons.append("TV: above VWAP")

            # Same tolerance fix for the put / breakdown side.
            if price <= structure_low * (1 - tolerance):
                breakout_aligned = True
                structure_confirmed = True
                score += CONFIG["price_reaction_breakout_bonus"]
                reasons.append("TV: confirmed breakdown below support")
            elif price <= structure_low:
                breakout_aligned = True
                structure_confirmed = True
                score += max(1, CONFIG["price_reaction_breakout_bonus"] - 1)
                reasons.append("TV: at support (early breakdown)")

            if ema_stable and ema_fast_now < ema_slow_now and ema_fast_now <= ema_fast_prev:
                ema_aligned = True
                trend_aligned = True
                score += 2
                reasons.append("TV: EMA 9/21 bearish")
            elif not ema_stable:
                reasons.append("TV: EMA trend not yet stable (early session)")

            if close_location_pct <= 40 and candle_body_pct >= min_body_pct:
                candle_confirmed = True
                score += 1
                reasons.append("TV: bearish candle close")

            if price <= day_low * 1.003:
                score += 1
                reasons.append("TV: near day low")

        if volume_expansion:
            score += CONFIG["price_reaction_volume_bonus"]
            reasons.append("TV: volume expansion")
        else:
            reasons.append("TV: volume not expanding")

        chase_pct = abs((price - vwap) / vwap) * 100 if vwap > 0 else 0
        effective_max_chase = get_effective_option_thresholds().get("max_chase_pct", CONFIG["price_reaction_max_chase_pct"])
        no_chase_ok = chase_pct <= effective_max_chase
        if chase_pct > effective_max_chase:
            score -= 2
            reasons.append(f"TV: no-chase warning {round(chase_pct, 2)}% from VWAP")

        recent_range_pct = ((recent_high - recent_low) / price) * 100 if price > 0 else 0
        chop_max = CONFIG.get("tv_chop_max_range_pct", 0.35)
        not_choppy = recent_range_pct >= chop_max
        if not_choppy:
            score += 1
            reasons.append("TV: enough intraday range")
        else:
            score -= 1
            reasons.append("TV: choppy/tight range")

        move_from_open_pct = ((price - open_price) / open_price) * 100 if open_price > 0 else 0
        price_confirmed = score >= get_effective_option_thresholds().get("price_reaction_min_score", CONFIG["price_reaction_min_score"])
        tradingview_confirmed = bool(
            CONFIG.get("enable_tradingview_style_confirmation", True)
            and score >= CONFIG.get("tradingview_min_score", 7)
            and vwap_aligned
            and breakout_aligned
            and ema_aligned
            and volume_expansion
            and no_chase_ok
        )

        return {
            "enabled": True,
            "confirmed": price_confirmed,
            "tradingview_confirmed": tradingview_confirmed,
            "score": score,
            "tradingview_score": score,
            "price": round(price, 2),
            "vwap": round(vwap, 2),
            "recent_vwap": round(recent_vwap, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "structure_high": round(structure_high, 2),
            "structure_low": round(structure_low, 2),
            "ema_fast": round(ema_fast_now, 2),
            "ema_slow": round(ema_slow_now, 2),
            "ema_stable": ema_stable,
            "chase_pct": round(chase_pct, 2),
            "move_from_open_pct": round(move_from_open_pct, 2),
            "recent_range_pct": round(recent_range_pct, 2),
            "volume_expansion": volume_expansion,
            "vwap_aligned": vwap_aligned,
            "breakout_aligned": breakout_aligned,
            "trend_aligned": trend_aligned,
            "ema_aligned": ema_aligned,
            "candle_confirmed": candle_confirmed,
            "structure_confirmed": structure_confirmed,
            "no_chase_ok": no_chase_ok,
            "not_choppy": not_choppy,
            "reason": ", ".join(reasons) if reasons else "TradingView-style structure weak/unclear",
        }

    except Exception as e:
        return {
            "enabled": True,
            "confirmed": False,
            "tradingview_confirmed": False,
            "score": 0,
            "tradingview_score": 0,
            "price": None,
            "vwap": None,
            "chase_pct": None,
            "reason": f"TradingView-style confirmation error: {e}",
        }


def find_latest_unusual_whales_csv():
    """
    Looks for an exported Unusual Whales CSV in the download folder.
    Recommended file name pattern: unusual_whales_YYYYMMDD.csv
    """
    try:
        if not CONFIG.get("enable_unusual_whales_confirmation", True):
            return None

        folder = CONFIG.get("download_folder")
        keyword = CONFIG.get("unusual_whales_file_keyword", "unusual_whales").lower()
        max_age_minutes = CONFIG.get("uw_max_file_age_minutes", 720)

        files = []
        for path in glob.glob(os.path.join(folder, "*.csv")):
            name = os.path.basename(path).lower()
            if keyword in name:
                age_minutes = (time.time() - os.path.getmtime(path)) / 60
                if age_minutes <= max_age_minutes:
                    files.append(path)

        return max(files, key=os.path.getmtime) if files else None
    except Exception as e:
        print("Unusual Whales CSV lookup error:", e)
        return None


def get_row_any_value(row, possible_names):
    """Case-insensitive flexible column lookup for external CSV files."""
    try:
        lookup = {str(c).strip().lower(): c for c in row.index}
        for name in possible_names:
            key = str(name).strip().lower()
            if key in lookup:
                return row.get(lookup[key])
        for c in row.index:
            col = str(c).strip().lower()
            for name in possible_names:
                if str(name).strip().lower() in col:
                    return row.get(c)
    except Exception:
        pass
    return ""


def normalize_external_option_type(value):
    text = str(value).upper().strip()
    if text in ["C", "CALL", "CALLS"] or "CALL" in text:
        return "CALL"
    if text in ["P", "PUT", "PUTS"] or "PUT" in text:
        return "PUT"
    return text


def load_unusual_whales_records():
    """
    Loads exported Unusual Whales flow CSV rows into a normalized list.

    Supported common columns include variations of:
    Ticker/Symbol, Type/Call Put, Strike, Expiry/Expiration, Premium, Price, Size, Side, Ask/Bid, Sentiment.
    """
    if not CONFIG.get("enable_unusual_whales_confirmation", True):
        return []

    csv_path = find_latest_unusual_whales_csv()
    if not csv_path:
        return []

    try:
        df = read_barchart_csv(csv_path)
        if df is None or df.empty:
            df = pd.read_csv(csv_path)
        df = normalize_columns(df)

        records = []
        for _, row in df.iterrows():
            ticker = str(get_row_any_value(row, ["Ticker", "Symbol", "Underlying", "Stock"])).upper().strip()
            option_type = normalize_external_option_type(get_row_any_value(row, ["Type", "Option Type", "Put/Call", "Call Put", "Contract Type"] ))
            strike = clean_number(get_row_any_value(row, ["Strike", "Strike Price"] ))
            expiry = str(get_row_any_value(row, ["Expiry", "Expiration", "Expiration Date", "Exp Date", "Expires"])).strip()

            premium = clean_number(get_row_any_value(row, ["Premium", "Total Premium", "Trade Value", "Value", "Cost"] ))
            trade_price = clean_number(get_row_any_value(row, ["Price", "Trade", "Fill", "Fill Price", "Option Price", "Last"] ))
            size = clean_number(get_row_any_value(row, ["Size", "Contracts", "Qty", "Quantity"] ))
            if premium <= 0 and trade_price > 0 and size > 0:
                premium = trade_price * size * 100

            side_text = " ".join(str(get_row_any_value(row, [x])) for x in ["Side", "Sentiment", "Ask/Bid", "Bid/Ask", "Condition", "Tags"])
            side_text = side_text.lower()
            aggressive = any(x in side_text for x in ["ask", "sweep", "above ask", "at ask", "aggressive", "aa"])

            if not ticker or ticker == "NAN" or option_type not in ["CALL", "PUT"]:
                continue
            if premium < CONFIG.get("uw_min_premium", 100000):
                continue

            records.append({
                "ticker": ticker,
                "option_type": option_type,
                "strike": strike,
                "expiry": expiry,
                "premium": premium,
                "size": size,
                "trade_price": trade_price,
                "aggressive": aggressive,
                "source_file": os.path.basename(csv_path),
                "raw_text": side_text,
            })

        if records:
            print(f"Unusual Whales confirmation records loaded: {len(records)} from {csv_path}")
        return records
    except Exception as e:
        print("Unusual Whales CSV read error:", e)
        return []


def expiry_matches_loose(a, b):
    """Loose expiry match because exports may use MM/DD/YY, MM/DD/YYYY, YYYY-MM-DD, etc."""
    a = str(a or "").strip()
    b = str(b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        da = pd.to_datetime(a).date()
        db = pd.to_datetime(b).date()
        return da == db
    except Exception:
        return a[-5:] == b[-5:] or a[:5] == b[:5]


def get_unusual_whales_confirmation(rec, uw_records=None):
    """
    Confirms a Barchart candidate using exported Unusual Whales data.

    Match quality:
    - SAME_CONTRACT: same ticker + CALL/PUT + strike + expiry when available
    - SAME_STRIKE: same ticker + CALL/PUT + strike
    - SAME_DIRECTION: same ticker + CALL/PUT
    - TICKER_ONLY: same ticker, weaker confirmation
    """
    if not CONFIG.get("enable_unusual_whales_confirmation", True):
        return {"confirmed": False, "level": "DISABLED", "score_bonus": 0, "confirmation": None, "reason": "Unusual Whales disabled"}

    uw_records = uw_records if uw_records is not None else load_unusual_whales_records()
    if not uw_records:
        return {"confirmed": False, "level": "NO_DATA", "score_bonus": 0, "confirmation": None, "reason": "No Unusual Whales CSV confirmation file found"}

    ticker = str(rec.get("ticker", "")).upper().strip()
    option_type = normalize_external_option_type(rec.get("option_type"))
    strike = clean_number(rec.get("strike"))
    expiry = str(rec.get("expiry", "")).strip()

    ticker_rows = [r for r in uw_records if r.get("ticker") == ticker]
    direction_rows = [r for r in ticker_rows if r.get("option_type") == option_type]
    same_strike_rows = [r for r in direction_rows if strike > 0 and clean_number(r.get("strike")) == strike]
    same_contract_rows = [r for r in same_strike_rows if expiry_matches_loose(r.get("expiry"), expiry)]

    if same_contract_rows:
        best = max(same_contract_rows, key=lambda x: x.get("premium", 0))
        bonus = CONFIG.get("uw_same_contract_bonus", 5)
        if best.get("premium", 0) >= CONFIG.get("uw_strong_premium", 300000):
            bonus += CONFIG.get("uw_trade_promotion_bonus", 3)
        if best.get("aggressive"):
            bonus += 2
        return {"confirmed": True, "level": "SAME_CONTRACT", "score_bonus": bonus, "confirmation": best,
                "reason": f"Unusual Whales confirms same contract, premium ${best.get('premium', 0):,.0f}"}

    if same_strike_rows and not CONFIG.get("uw_expiry_match_required_for_same_contract", False):
        best = max(same_strike_rows, key=lambda x: x.get("premium", 0))
        bonus = max(CONFIG.get("uw_same_contract_bonus", 5) - 1, CONFIG.get("uw_same_direction_bonus", 3))
        if best.get("aggressive"):
            bonus += 1
        return {"confirmed": True, "level": "SAME_STRIKE", "score_bonus": bonus, "confirmation": best,
                "reason": f"Unusual Whales confirms same strike/direction, premium ${best.get('premium', 0):,.0f}"}

    if direction_rows:
        best = max(direction_rows, key=lambda x: x.get("premium", 0))
        bonus = CONFIG.get("uw_same_direction_bonus", 3)
        if best.get("premium", 0) >= CONFIG.get("uw_strong_premium", 300000):
            bonus += 1
        return {"confirmed": True, "level": "SAME_DIRECTION", "score_bonus": bonus, "confirmation": best,
                "reason": f"Unusual Whales confirms same ticker/direction, premium ${best.get('premium', 0):,.0f}"}

    if ticker_rows:
        best = max(ticker_rows, key=lambda x: x.get("premium", 0))
        return {"confirmed": False, "level": "TICKER_ONLY", "score_bonus": CONFIG.get("uw_ticker_only_bonus", 1), "confirmation": best,
                "reason": f"Unusual Whales has ticker activity but not same direction, premium ${best.get('premium', 0):,.0f}"}

    return {"confirmed": False, "level": "NO_MATCH", "score_bonus": 0, "confirmation": None, "reason": "No Unusual Whales match"}

def get_contract_cluster_stats(rec, unusual_records, flow_records):
    """
    Detects institutional order splitting / clustering.
    Multiple rows for the same ticker, side, strike and expiry usually means a
    larger order was split into smaller prints or repeated at the same contract.
    """
    all_records = unusual_records + flow_records
    same_contract_records = [r for r in all_records if same_contract(rec, r)]
    same_ticker_side_records = [
        r for r in all_records
        if r.get("ticker") == rec.get("ticker") and r.get("option_type") == rec.get("option_type")
    ]

    same_contract_premium = sum((r.get("premium", 0) or 0) for r in same_contract_records)
    same_ticker_side_premium = sum((r.get("premium", 0) or 0) for r in same_ticker_side_records)

    return {
        "same_contract_count": len(same_contract_records),
        "same_contract_premium": same_contract_premium,
        "same_ticker_side_count": len(same_ticker_side_records),
        "same_ticker_side_premium": same_ticker_side_premium,
    }



# ============================================================
# WIN-RATE BASED AUTO FILTERING
# ============================================================

_WIN_RATE_RULE_CACHE = {
    "loaded_at": None,
    "rules": None,
}


def get_dte_bucket(dte):
    dte = int(clean_number(dte))
    if dte <= 2:
        return "DTE_0_2"
    if dte <= 6:
        return "DTE_3_6"
    if dte <= 13:
        return "DTE_7_13"
    if dte <= 30:
        return "DTE_14_30"
    if dte <= 45:
        return "DTE_31_45"
    if dte <= 90:
        return "DTE_46_90"
    return "DTE_90_PLUS"


def get_premium_bucket(premium):
    premium = clean_number(premium)
    if premium < 100000:
        return "PREMIUM_LT_100K"
    if premium < 300000:
        return "PREMIUM_100K_300K"
    if premium < 500000:
        return "PREMIUM_300K_500K"
    if premium < 1000000:
        return "PREMIUM_500K_1M"
    return "PREMIUM_1M_PLUS"


def get_score_bucket(score):
    score = clean_number(score)
    if score < 12:
        return "SCORE_LT_12"
    if score < 18:
        return "SCORE_12_17"
    if score < 24:
        return "SCORE_18_23"
    return "SCORE_24_PLUS"


def get_current_signal_buckets(ticker, option_type, dte, premium, score, decision=None):
    option_type = str(option_type).upper().strip()
    buckets = [
        f"SIDE_{option_type}",
        get_dte_bucket(dte),
        get_premium_bucket(premium),
        get_score_bucket(score),
    ]
    if decision:
        buckets.append(f"DECISION_{str(decision).upper().strip()}")
    if CONFIG.get("win_rate_filter_include_ticker_bucket", False):
        ticker = str(ticker).upper().strip()
        if ticker:
            buckets.append(f"TICKER_{ticker}")
    return buckets


def normalize_learning_outcome(status, result):
    text = f"{status} {result}".upper()
    if any(x in text for x in ["TARGET", "WIN", "SUCCESS", "PROFIT", "HIT_TARGET"]):
        return "WIN"
    if any(x in text for x in ["STOP", "LOSS", "FAILED", "LOSE"]):
        return "LOSS"
    return "OPEN"


def build_win_rate_filter_rules(force_reload=False):
    """
    Reads OptionSignals history and builds win-rate stats by bucket.
    Buckets are intentionally simple and robust: side, DTE, premium, score, decision.
    """
    if not CONFIG.get("enable_win_rate_auto_filtering", True):
        return {}

    cache = _WIN_RATE_RULE_CACHE
    now_ts = time.time()
    if (
        not force_reload
        and cache.get("rules") is not None
        and cache.get("loaded_at") is not None
        and now_ts - cache.get("loaded_at") < 300
    ):
        return cache["rules"]

    excel_file = CONFIG.get("excel_file")
    if not excel_file or not os.path.exists(excel_file):
        cache["rules"] = {}
        cache["loaded_at"] = now_ts
        return {}

    try:
        wb = load_workbook(excel_file, data_only=True)
        if "OptionSignals" not in wb.sheetnames:
            cache["rules"] = {}
            cache["loaded_at"] = now_ts
            return {}

        ws = wb["OptionSignals"]
        headers = [str(c.value).strip() if c.value else "" for c in ws[1]]
        h = {name: idx + 1 for idx, name in enumerate(headers) if name}

        required = ["DateTime", "Ticker", "Decision", "Score", "Option Type", "Premium", "DTE", "Status", "Result"]
        for col in required:
            if col not in h:
                cache["rules"] = {}
                cache["loaded_at"] = now_ts
                return {}

        cutoff_date = get_market_now().date() - pd.Timedelta(days=CONFIG.get("win_rate_filter_recent_days", 30))
        stats = {}

        def add(bucket, outcome):
            if outcome not in ["WIN", "LOSS"]:
                return
            if bucket not in stats:
                stats[bucket] = {"wins": 0, "losses": 0, "total": 0, "win_rate": 0.0}
            stats[bucket]["total"] += 1
            if outcome == "WIN":
                stats[bucket]["wins"] += 1
            else:
                stats[bucket]["losses"] += 1

        for r in range(2, ws.max_row + 1):
            dt_text = str(ws.cell(r, h["DateTime"]).value or "")
            try:
                row_date = pd.to_datetime(dt_text, errors="coerce").date()
                if row_date and row_date < cutoff_date:
                    continue
            except Exception:
                pass

            outcome = normalize_learning_outcome(
                ws.cell(r, h["Status"]).value,
                ws.cell(r, h["Result"]).value,
            )
            if outcome not in ["WIN", "LOSS"]:
                continue

            ticker = str(ws.cell(r, h["Ticker"]).value or "").upper().strip()
            decision = str(ws.cell(r, h["Decision"]).value or "").upper().strip()
            option_type = str(ws.cell(r, h["Option Type"]).value or "").upper().strip()
            score = clean_number(ws.cell(r, h["Score"]).value)
            premium = clean_number(ws.cell(r, h["Premium"]).value)
            dte = clean_number(ws.cell(r, h["DTE"]).value)

            for bucket in get_current_signal_buckets(ticker, option_type, dte, premium, score, decision):
                add(bucket, outcome)

        for bucket, s in stats.items():
            total = s["total"]
            s["win_rate"] = round((s["wins"] / total) * 100, 2) if total else 0.0

        cache["rules"] = stats
        cache["loaded_at"] = now_ts
        return stats

    except Exception as e:
        print("Win-rate auto filter build error:", e)
        cache["rules"] = {}
        cache["loaded_at"] = now_ts
        return {}


def apply_win_rate_auto_filtering(ticker, option_type, dte, premium, score, decision_hint=None):
    """
    Returns score adjustment and risk controls from historical performance.

    Good buckets boost score slightly.
    Bad buckets penalize score and can force WATCHLIST instead of TRADE.
    This prevents the system from repeating historically weak patterns.
    """
    result = {
        "score_adjustment": 0,
        "force_watchlist": False,
        "block_signal": False,
        "notes": [],
        "matched_good_buckets": [],
        "matched_bad_buckets": [],
    }

    if not CONFIG.get("enable_win_rate_auto_filtering", True):
        return result

    rules = build_win_rate_filter_rules()
    if not rules:
        result["notes"].append("Win-rate auto filter: no completed history yet")
        return result

    min_trades = CONFIG.get("win_rate_filter_min_trades", 5)
    good_pct = CONFIG.get("win_rate_filter_good_pct", 60)
    bad_pct = CONFIG.get("win_rate_filter_bad_pct", 40)
    boost_points = CONFIG.get("win_rate_filter_boost_points", 2)
    penalty_points = CONFIG.get("win_rate_filter_penalty_points", 4)

    buckets = get_current_signal_buckets(ticker, option_type, dte, premium, score, decision_hint)
    for bucket in buckets:
        s = rules.get(bucket)
        if not s or s.get("total", 0) < min_trades:
            continue

        note = f"{bucket}: {s['wins']}W/{s['losses']}L, WR {s['win_rate']}%"
        if s["win_rate"] >= good_pct:
            result["matched_good_buckets"].append(bucket)
            result["notes"].append("Positive historical bucket: " + note)
        elif s["win_rate"] <= bad_pct:
            result["matched_bad_buckets"].append(bucket)
            result["notes"].append("Weak historical bucket: " + note)

    if result["matched_good_buckets"]:
        result["score_adjustment"] += boost_points

    if result["matched_bad_buckets"]:
        result["score_adjustment"] -= penalty_points
        if CONFIG.get("win_rate_filter_force_watchlist", True):
            result["force_watchlist"] = True
        if CONFIG.get("win_rate_filter_block_bad_setup", False):
            result["block_signal"] = True

    if CONFIG.get("win_rate_filter_debug", False):
        print("Win-rate filter result:", result)

    return result

def categorize_option_signal(score, confirmations, premium=0, high_probability_passed=False, force_watchlist=False):
    """
    Converts the institutional score into a clean execution category.

    TRADE:
    - Best setups only; eligible for Telegram execution alert.

    WATCHLIST:
    - Good-but-not-perfect setups; saved to Excel/log so they are not missed.
    - Telegram only if SEND_WATCHLIST_TO_TELEGRAM=true.

    IGNORE:
    - Too weak to track.
    """
    if not CONFIG.get("enable_signal_categorization", True):
        return "TRADE" if score >= CONFIG.get("option_trade_score", 8) else "IGNORE"

    confirmation_count = len(confirmations or [])
    premium = clean_number(premium)

    if high_probability_passed and not force_watchlist:
        return "TRADE"

    if (
        not force_watchlist
        and score >= CONFIG.get("trade_min_score", 18)
        and confirmation_count >= CONFIG.get("trade_min_confirmations", 5)
    ):
        return "TRADE"

    if (
        score >= CONFIG.get("watchlist_min_score", 12)
        and confirmation_count >= CONFIG.get("watchlist_min_confirmations", 3)
        and premium >= CONFIG.get("watchlist_min_premium", 100000)
    ):
        return "WATCHLIST"

    return "IGNORE"

def detect_opposite_leg_structure(rec, unusual_records, flow_records):
    """
    v17.1: Detects structural trades (straddles, strangles, ratio spreads, etc.)
    masquerading as directional bets.

    The problem this solves:
      Institutions often hedge or speculate via two-sided structures, not
      single directional bets. The original scoring counted a $2M call print
      as "institutional bullish conviction" even when there was a near-equal
      $2M put print on the same ticker in the same scan — which is a strangle
      or straddle, not a directional bet.

    This function scans the same set of unusual+flow records that the rest
    of the scoring uses, looking for opposite-side prints that suggest the
    candidate signal is one leg of a multi-leg structure rather than a
    standalone directional bet.

    Returns a dict:
      - is_likely_structure: True if a probable structural trade is detected
      - structure_type: "straddle" | "strangle" | "spread" | "ratio" | None
      - opposite_premium: total premium on the opposite side for this ticker
      - same_side_premium: total premium on the same side
      - premium_ratio: same / opposite (1.0 = balanced; >2 = directionally heavy)
      - matched_strikes: count of opposite-side prints at same strike (straddle indicator)
      - reason: human-readable explanation

    The scoring caller can use is_likely_structure to demote the signal
    from TRADE to WATCHLIST, or skip it entirely.
    """
    if not CONFIG.get("enable_opposite_leg_detection", True):
        return {
            "is_likely_structure": False,
            "structure_type": None,
            "reason": "Opposite-leg detection disabled",
        }

    ticker = rec.get("ticker", "").upper().strip()
    candidate_type = str(rec.get("option_type", "")).lower().strip()
    candidate_strike = clean_number(rec.get("strike", 0))
    candidate_premium = clean_number(rec.get("premium", 0))

    if not ticker or candidate_premium <= 0:
        return {
            "is_likely_structure": False,
            "structure_type": None,
            "reason": "Insufficient candidate data for structure check",
        }

    # Only run the check on signals significant enough to plausibly be one
    # leg of an institutional structure. Below this premium, even a "matched"
    # opposite leg is more likely coincidence than structure.
    min_premium_for_check = CONFIG.get("opposite_leg_min_premium", 250000)
    if candidate_premium < min_premium_for_check:
        return {
            "is_likely_structure": False,
            "structure_type": None,
            "reason": f"Candidate premium ${candidate_premium:,.0f} below structure-check threshold",
        }

    is_call_candidate = "call" in candidate_type
    opposite_type_keyword = "put" if is_call_candidate else "call"

    all_records = (unusual_records or []) + (flow_records or [])

    same_side_total = 0.0
    opposite_side_total = 0.0
    same_strike_opposite_prints = []
    near_strike_opposite_prints = []  # within strangle-tolerance band

    # Strangle band: opposite-side strikes within ±X% of candidate strike
    # are considered "near" enough to be one leg of a strangle. 8% default.
    strangle_band_pct = CONFIG.get("opposite_leg_strangle_band_pct", 8.0) / 100.0
    strangle_low = candidate_strike * (1 - strangle_band_pct) if candidate_strike > 0 else 0
    strangle_high = candidate_strike * (1 + strangle_band_pct) if candidate_strike > 0 else 0

    for r in all_records:
        if str(r.get("ticker", "")).upper().strip() != ticker:
            continue
        r_type = str(r.get("option_type", "")).lower().strip()
        r_premium = clean_number(r.get("premium", 0))
        r_strike = clean_number(r.get("strike", 0))

        if r_premium <= 0:
            continue

        # Don't count the candidate itself.
        if (r_type == candidate_type
                and r_strike == candidate_strike
                and abs(r_premium - candidate_premium) < 0.01):
            continue

        if opposite_type_keyword in r_type:
            opposite_side_total += r_premium
            # Same-strike opposite = straddle indicator
            if candidate_strike > 0 and abs(r_strike - candidate_strike) < 0.01:
                same_strike_opposite_prints.append(r)
            # Near-strike opposite = strangle indicator
            elif candidate_strike > 0 and strangle_low <= r_strike <= strangle_high:
                near_strike_opposite_prints.append(r)
        elif candidate_type and (
            ("call" in candidate_type and "call" in r_type)
            or ("put" in candidate_type and "put" in r_type)
        ):
            same_side_total += r_premium

    # Add candidate itself to the same-side total for ratio calc.
    same_side_total += candidate_premium

    # Decision logic ------------------------------------------------------
    structure_type = None
    is_likely_structure = False
    reason_parts = []

    # Premium balance: if opposite-side premium is at least X% of same-side,
    # it's plausibly part of a structure. 60% is the default — catches roughly
    # equal hedges without flagging trivial counter-flow as a structure.
    balance_threshold_pct = CONFIG.get("opposite_leg_balance_threshold_pct", 60) / 100.0
    premium_ratio = (same_side_total / opposite_side_total) if opposite_side_total > 0 else float("inf")
    is_balanced = (
        opposite_side_total >= same_side_total * balance_threshold_pct
        and opposite_side_total >= min_premium_for_check
    )

    # Straddle: same-strike opposite-side prints with balanced premium
    if same_strike_opposite_prints and is_balanced:
        structure_type = "straddle"
        is_likely_structure = True
        same_strike_premium = sum(clean_number(r.get("premium", 0)) for r in same_strike_opposite_prints)
        reason_parts.append(
            f"Likely STRADDLE: {len(same_strike_opposite_prints)} opposite-side prints "
            f"at strike {candidate_strike} (${same_strike_premium:,.0f}); "
            f"same/opposite premium ratio {premium_ratio:.2f}"
        )

    # Strangle: near-strike opposite-side prints with balanced premium
    elif near_strike_opposite_prints and is_balanced:
        structure_type = "strangle"
        is_likely_structure = True
        near_strike_premium = sum(clean_number(r.get("premium", 0)) for r in near_strike_opposite_prints)
        reason_parts.append(
            f"Likely STRANGLE: {len(near_strike_opposite_prints)} opposite-side prints "
            f"near strike {candidate_strike} (±{int(strangle_band_pct*100)}%, ${near_strike_premium:,.0f}); "
            f"same/opposite premium ratio {premium_ratio:.2f}"
        )

    # Generic two-sided activity: opposite-side premium is meaningful but
    # not strike-aligned. Could be a complex spread, hedge, or just noise.
    # We flag this less severely.
    elif is_balanced:
        structure_type = "two_sided_activity"
        is_likely_structure = True
        reason_parts.append(
            f"Two-sided ticker activity: opposite-side premium "
            f"${opposite_side_total:,.0f} vs same-side ${same_side_total:,.0f} "
            f"(ratio {premium_ratio:.2f})"
        )

    # Heavy directional case: opposite-side premium small relative to same.
    elif opposite_side_total > 0:
        reason_parts.append(
            f"Directional dominant: opposite-side ${opposite_side_total:,.0f} "
            f"vs same-side ${same_side_total:,.0f} (ratio {premium_ratio:.2f})"
        )
    else:
        reason_parts.append("No opposite-side activity detected")

    return {
        "is_likely_structure": is_likely_structure,
        "structure_type": structure_type,
        "opposite_premium": round(opposite_side_total, 0),
        "same_side_premium": round(same_side_total, 0),
        "premium_ratio": round(premium_ratio, 2) if premium_ratio != float("inf") else None,
        "same_strike_opposite_count": len(same_strike_opposite_prints),
        "near_strike_opposite_count": len(near_strike_opposite_prints),
        "reason": "; ".join(reason_parts),
    }


def score_institutional_option_candidate(rec, unusual_records, flow_records, ticker_bias):
    score = 0
    reasons = []

    ticker = rec["ticker"]
    premium = rec.get("premium", 0) or 0
    volume = rec.get("volume", 0) or 0
    oi = rec.get("oi", 0) or 0
    vol_oi = rec.get("vol_oi", 0) or 0
    dte = rec.get("dte", 0) or 0
    entry = rec.get("entry", 0) or 0
    code = str(rec.get("code", "")).upper().strip()
    option_type = rec["option_type"]
    direction = rec["direction"]

    b = ticker_bias.get(ticker, {})
    thresholds = get_effective_option_thresholds()

    if not CONFIG.get("enable_option_signals", True):
        return None

    quality_window_ok, quality_window_reason = is_option_quality_window()
    if CONFIG.get("enable_strict_signal_filtering", True) and not quality_window_ok:
        reasons.append("Outside preferred option quality window; categorize as watchlist only if other confirmations are strong")
        score -= 2
    else:
        reasons.append(quality_window_reason)

    if dte < CONFIG["option_min_dte"] or dte > CONFIG["option_max_dte"]:
        return None
    if entry <= 0:
        return None
    minimum_trackable_premium = min(thresholds["min_premium"], CONFIG.get("watchlist_min_premium", thresholds["min_premium"]))
    if premium < minimum_trackable_premium:
        return None
    if volume < CONFIG["min_volume"] and rec["source"] == "UNUSUAL":
        return None

    # Avoid TSLA-style two-sided gamma traps as directional recommendations.
    if CONFIG.get("mixed_direction_no_directional_trade", True) and b.get("bias") == "MIXED_GAMMA":
        return None

    # Market regime alignment: CALLs prefer SPY/QQQ bullish, PUTs prefer SPY/QQQ bearish.
    market_alignment = get_option_market_regime_alignment(option_type)
    if CONFIG.get("require_market_regime_alignment", True):
        if market_alignment.get("known") and not market_alignment.get("aligned"):
            score -= 3
            reasons.append("Market regime not aligned; can only qualify as watchlist if other factors are strong")
        if not market_alignment.get("known") and not CONFIG.get("allow_neutral_market_regime", False):
            score -= 2
            reasons.append("Market regime unknown; can only qualify as watchlist if other factors are strong")
    if market_alignment.get("aligned"):
        score += 2
    reasons.append(market_alignment.get("reason", "Market regime checked"))

    # Ticker-level bias confirmation.
    if b.get("bias") == direction:
        score += 4
        reasons.append(f"Ticker-level bias confirms {direction}")
    elif b.get("bias") not in [None, "NEUTRAL"]:
        score -= 4
        reasons.append(f"Ticker-level bias conflict: {b.get('bias')}")

    if rec["source"] == "FLOW":
        score += 3
        reasons.append("Barchart option-flow row")
    else:
        score += 2
        reasons.append("Barchart unusual-activity row")

    if b.get("has_unusual") and b.get("has_flow"):
        score += 5
        reasons.append("Ticker appears in both Unusual Activity and Option Flow")

    # Same contract confirmation between unusual and flow.
    if rec["source"] == "FLOW":
        matched_same_contract = any(same_contract(rec, u) for u in unusual_records)
        matched_same_direction = any(u["ticker"] == ticker and u["option_type"] == option_type for u in unusual_records)
    else:
        matched_same_contract = any(same_contract(rec, f) for f in flow_records)
        matched_same_direction = any(f["ticker"] == ticker and f["option_type"] == option_type for f in flow_records)

    if matched_same_contract:
        score += 5
        reasons.append("Same strike/expiry confirmed across Flow + Unusual")
    elif matched_same_direction:
        score += 3
        reasons.append("Same direction confirmed across Flow + Unusual")

    related_flow_records = [
        f for f in flow_records
        if f.get("ticker") == ticker and f.get("option_type") == option_type
    ]
    related_flow_premium = sum((f.get("premium", 0) or 0) for f in related_flow_records)
    aggressive_related_flow = any(
        str(f.get("code", "")).upper().strip() in CONFIG.get("aggressive_flow_codes", [])
        for f in related_flow_records
    )
    if rec["source"] == "FLOW" and code in CONFIG.get("aggressive_flow_codes", []):
        aggressive_related_flow = True

    # Premium strength from screenshots: $100K minimum, $500K strong, $1M+ institutional.
    if premium >= CONFIG["option_premium_huge"]:
        score += 7
        reasons.append("Institutional premium $1M+")
    elif premium >= CONFIG["option_premium_large"]:
        score += 6
        reasons.append("Very large premium $500K+")
    elif premium >= CONFIG["option_premium_medium"]:
        score += 4
        reasons.append("Premium $100K+")
    elif premium >= CONFIG["option_premium_small"]:
        score += 2
        reasons.append("Premium $50K+")

    # Flow code priority.
    if rec["source"] == "FLOW":
        if code in CONFIG["aggressive_flow_codes"]:
            score += 5
            reasons.append(f"Aggressive Barchart flow code {code}")
        elif code in CONFIG["valid_flow_codes"]:
            if code == "AUTO" and premium < CONFIG["auto_flow_min_premium"]:
                score -= 2
                reasons.append("AUTO flow below institutional AUTO premium threshold")
            else:
                score += 2
                reasons.append(f"Valid Barchart flow code {code}")
        else:
            reasons.append("Flow code not classified")

    # Volume / OI.
    if volume >= CONFIG["option_volume_very_high"]:
        score += 3
        reasons.append("Very high option volume")
    elif volume >= CONFIG["option_volume_high"]:
        score += 2
        reasons.append("High option volume")
    elif volume >= CONFIG["option_volume_decent"]:
        score += 1
        reasons.append("Decent option volume")

    if oi <= 0 and volume >= CONFIG["option_volume_decent"]:
        score += 3
        reasons.append("Fresh opening activity: low/zero OI")
    elif vol_oi >= CONFIG["option_vol_oi_aggressive"]:
        score += 4
        reasons.append("Aggressive Vol/OI")
    elif vol_oi >= CONFIG["option_vol_oi_strong"]:
        score += 3
        reasons.append("Strong Vol/OI")
    elif vol_oi >= CONFIG["option_vol_oi_ok"]:
        score += 1
        reasons.append("Vol/OI acceptable")

    # DTE priority.
    if CONFIG["option_sweet_spot_min_dte"] <= dte <= CONFIG["option_sweet_spot_max_dte"]:
        score += 4
        reasons.append("Institutional DTE sweet spot 14–30")
    elif 3 <= dte <= 45:
        score += 2
        reasons.append("Tradable swing DTE")
    elif dte <= 2:
        score -= 2
        reasons.append("Very short DTE: 0DTE/1DTE gamma risk")

    delta = abs(rec.get("delta", 0) or 0)
    if delta > 0:
        if 0.25 <= delta <= 0.80:
            score += 2
            reasons.append("Delta quality acceptable")
        elif delta < 0.15:
            score -= 1
            reasons.append("Low delta / lottery-style contract")

    # v16: IV percentile gating. Buying calls/puts at IV%ile > 70 has lower
    # hit rate even on directionally-correct signals due to crush risk.
    current_iv = rec.get("iv", 0) or 0
    iv_adjustment, iv_reason, iv_data = score_iv_environment(
        ticker, current_iv, option_type, is_buying_premium=True
    )
    if iv_adjustment != 0:
        score += iv_adjustment
    reasons.append(f"IV: {iv_reason}")

    # Order-splitting / clustering detection.
    cluster_stats = get_contract_cluster_stats(rec, unusual_records, flow_records)
    if CONFIG.get("enable_order_splitting_detection", True):
        if cluster_stats["same_contract_count"] >= CONFIG["split_order_min_count"]:
            score += CONFIG["split_order_bonus"]
            reasons.append(
                f"Possible split order: {cluster_stats['same_contract_count']} prints same contract, "
                f"total premium ${cluster_stats['same_contract_premium']:,.0f}"
            )
        elif cluster_stats["same_ticker_side_count"] >= CONFIG["split_order_min_count"]:
            score += CONFIG["same_ticker_cluster_bonus"]
            reasons.append(
                f"Same ticker/side cluster: {cluster_stats['same_ticker_side_count']} prints, "
                f"side premium ${cluster_stats['same_ticker_side_premium']:,.0f}"
            )

    # v17.1: Opposite-leg / structural-trade detector.
    # If a candidate signal looks like one leg of a straddle, strangle, or
    # otherwise-balanced two-sided trade, it is NOT a directional bet. Demote
    # it before it pollutes the alert stream.
    structure_check = detect_opposite_leg_structure(rec, unusual_records, flow_records)
    structure_force_watchlist = False
    if structure_check.get("is_likely_structure"):
        structure_type = structure_check.get("structure_type", "structure")
        if structure_type in ("straddle", "strangle"):
            penalty = CONFIG.get("opposite_leg_strict_penalty", -6)
            structure_force_watchlist = True
        else:
            # Generic two-sided activity: softer penalty, may still trade if
            # other factors are very strong.
            penalty = CONFIG.get("opposite_leg_soft_penalty", -3)
        score += penalty
        reasons.append(
            f"v17.1 STRUCTURE FLAG ({penalty:+d}): {structure_check.get('reason', '')}"
        )
    else:
        # Log the directional dominance even when not flagged — useful audit trail.
        reasons.append(f"Structure check: {structure_check.get('reason', 'no opposite-leg activity')}")

    # External confirmation: Unusual Whales exported flow CSV.
    uw_confirmation = get_unusual_whales_confirmation(rec)
    if uw_confirmation.get("confirmed"):
        score += uw_confirmation.get("score_bonus", 0)
        reasons.append(uw_confirmation.get("reason", "Unusual Whales confirms flow"))
    else:
        if uw_confirmation.get("score_bonus", 0) > 0:
            score += uw_confirmation.get("score_bonus", 0)
        reasons.append(uw_confirmation.get("reason", "No Unusual Whales confirmation"))

    # Real-time style price-reaction confirmation.
    price_reaction = get_enhanced_price_reaction(ticker, option_type)
    if price_reaction.get("confirmed"):
        score += min(max(price_reaction.get("score", 0), 0), 6)
        reasons.append(f"Price reaction confirms flow: {price_reaction.get('reason')}")
    else:
        penalty = CONFIG.get("price_reaction_penalty", 3)
        if rec["source"] == "FLOW" and premium >= CONFIG["option_premium_large"]:
            # Large flow can still be valid before the stock fully reacts, but it should be marked lower conviction.
            score -= min(penalty, 2)
        else:
            score -= penalty
        reasons.append(f"Price reaction pending/weak: {price_reaction.get('reason')}")

    # Structure confirmation: require directional price structure, not just flow.
    structure_confirmed = bool(
        price_reaction.get("vwap_aligned")
        and price_reaction.get("breakout_aligned")
        and price_reaction.get("no_chase_ok", True)
    )
    tradingview_confirmed = bool(price_reaction.get("tradingview_confirmed"))
    if tradingview_confirmed:
        score += 2
        reasons.append(f"TradingView-style confirmation passed: {price_reaction.get('reason')}")
    else:
        reasons.append(f"TradingView-style confirmation not complete: {price_reaction.get('reason')}")
        if CONFIG.get("require_tradingview_for_trade", False):
            score -= 3
            reasons.append("TradingView-style confirmation required for TRADE; downgraded unless other quality is exceptional")

    if CONFIG.get("require_structure_confirmation", True) and not structure_confirmed:
        score -= 2
        reasons.append("Structure confirmation missing; saved only as watchlist if remaining quality is strong")

    # Multi-layer confirmation count.  This prevents one-factor trades.
    # Confirmations should be INDEPENDENT signals — a FLOW row matching
    # same-contract is one piece of evidence, not three.
    confirmations = []
    # Pick the strongest single flow-based confirmation rather than counting
    # source/same-direction/same-contract as separate items.
    if matched_same_contract:
        confirmations.append("flow_same_contract")
    elif rec["source"] == "FLOW" and matched_same_direction:
        confirmations.append("flow_same_direction")
    elif rec["source"] == "FLOW" or matched_same_direction:
        confirmations.append("flow_present")
    if price_reaction.get("confirmed"):
        confirmations.append("price_reaction")
    if b.get("bias") == direction:
        confirmations.append("ticker_bias")
    if premium >= thresholds["min_premium"]:
        confirmations.append("premium")
    # Cluster is independent from same-contract match if it's the same-ticker-side cluster
    if cluster_stats["same_contract_count"] >= CONFIG.get("split_order_min_count", 2) and not matched_same_contract:
        confirmations.append("cluster_same_contract")
    elif cluster_stats["same_ticker_side_count"] >= CONFIG.get("split_order_min_count", 2):
        confirmations.append("cluster_same_side")
    if market_alignment.get("aligned"):
        confirmations.append("market_regime")
    if structure_confirmed:
        confirmations.append("structure")
    if tradingview_confirmed:
        confirmations.append("tradingview_style")
    if uw_confirmation.get("confirmed"):
        confirmations.append("unusual_whales")

    reasons.append(f"Confirmations: {', '.join(confirmations) if confirmations else 'none'}")
    if CONFIG.get("require_multi_layer_confirmation", True) and len(confirmations) < CONFIG.get("option_min_confirmations", 3):
        reasons.append("Below preferred multi-layer confirmation count; eligible for watchlist only if score remains strong")

    # Win-rate filter applied BEFORE high-probability gate. Old code ran the
    # filter after high-prob already passed, so a historically losing bucket
    # could still earn a TRADE label.
    win_rate_filter = apply_win_rate_auto_filtering(ticker, option_type, dte, premium, score)
    if win_rate_filter.get("notes"):
        reasons.extend(win_rate_filter.get("notes", []))
    if win_rate_filter.get("block_signal"):
        reasons.append("Blocked by win-rate auto filter due to historically weak setup bucket")
        return None
    if win_rate_filter.get("score_adjustment", 0):
        score += win_rate_filter.get("score_adjustment", 0)
        reasons.append(f"Win-rate auto filter score adjustment: {win_rate_filter.get('score_adjustment', 0):+d}")

    high_probability_passed = False
    if CONFIG.get("enable_high_probability_mode", True):
        high_prob_rejects = []

        if premium < CONFIG.get("high_prob_min_premium", 300000):
            high_prob_rejects.append("premium below high-probability minimum")

        if related_flow_premium < CONFIG.get("high_prob_min_flow_premium", 200000):
            high_prob_rejects.append("related flow premium below high-probability minimum")

        if not (CONFIG.get("high_prob_min_dte", 7) <= dte <= CONFIG.get("high_prob_max_dte", 45)):
            high_prob_rejects.append("DTE outside high-probability range")

        if CONFIG.get("high_prob_require_flow", True) and not (rec["source"] == "FLOW" or matched_same_direction or matched_same_contract):
            high_prob_rejects.append("flow confirmation missing")

        if CONFIG.get("high_prob_require_aggressive_flow", True) and not aggressive_related_flow:
            high_prob_rejects.append("aggressive flow code missing")

        if code == "AUTO" and premium < CONFIG.get("high_prob_auto_min_premium", 1000000):
            high_prob_rejects.append("AUTO flow below high-probability AUTO threshold")

        same_contract_or_cluster = bool(
            matched_same_contract
            or cluster_stats["same_contract_count"] >= CONFIG.get("split_order_min_count", 2)
            or cluster_stats["same_ticker_side_count"] >= CONFIG.get("split_order_min_count", 2)
        )
        if CONFIG.get("high_prob_require_same_contract_or_cluster", True) and not same_contract_or_cluster:
            high_prob_rejects.append("same-contract or cluster confirmation missing")

        if CONFIG.get("high_prob_require_market_alignment", True) and not market_alignment.get("aligned"):
            high_prob_rejects.append("market regime not aligned")

        if CONFIG.get("high_prob_require_price_reaction", True):
            if not price_reaction.get("confirmed"):
                high_prob_rejects.append("price reaction not confirmed")
            if price_reaction.get("score", 0) < CONFIG.get("high_prob_min_price_reaction_score", 6):
                high_prob_rejects.append("price reaction score below high-probability minimum")

        if CONFIG.get("high_prob_require_tradingview_confirmation", True):
            if not price_reaction.get("tradingview_confirmed"):
                high_prob_rejects.append("TradingView-style confirmation missing")
            if price_reaction.get("tradingview_score", 0) < CONFIG.get("tradingview_high_prob_min_score", 8):
                high_prob_rejects.append("TradingView-style score below high-probability minimum")

        if CONFIG.get("high_prob_require_no_chase", True):
            if not price_reaction.get("no_chase_ok", True):
                high_prob_rejects.append("no-chase rule failed")
            if clean_number(price_reaction.get("chase_pct", 0)) > CONFIG.get("high_prob_max_chase_pct", 2.0):
                high_prob_rejects.append("stock extended too far from VWAP")

        earnings = get_earnings_risk(ticker)
        if earnings.get("earnings_near") and clean_number(earnings.get("days_to_earnings", 999)) <= CONFIG.get("high_prob_block_earnings_within_days", 2):
            high_prob_rejects.append(f"earnings risk: {earnings.get('reason')}")

        if CONFIG.get("high_prob_require_unusual_whales", False) and not uw_confirmation.get("confirmed"):
            high_prob_rejects.append("Unusual Whales external confirmation missing")

        if len(confirmations) < CONFIG.get("high_prob_min_confirmations", 5):
            high_prob_rejects.append("not enough independent confirmations")

        # v16: VIX regime adjusts the high-prob score floor dynamically.
        # Calm tape -> easier to hit; elevated/panic tape -> harder.
        # v16.1 fix: when VIX is in panic mode AND IV%ile is very high, cap
        # the combined penalty. Both filters are measuring elevated-volatility
        # risk — don't double-count them. The signal is already losing raw
        # score points to the IV penalty; don't also raise the floor by the
        # full panic adjustment.
        vix_regime = get_vix_regime()
        vix_adj = int(vix_regime.get("score_adjustment", 0))

        iv_data_for_combo = next(
            (r.replace("IV: ", "") for r in reasons if r.startswith("IV: IV%ile")),
            ""
        )
        iv_was_very_high = "VERY HIGH" in iv_data_for_combo

        if iv_was_very_high and vix_adj >= CONFIG.get("vix_elevated_score_adj", 2):
            combined_cap = CONFIG.get("vix_iv_combined_cap", 3)
            if vix_adj > combined_cap:
                reasons.append(
                    f"VIX+IV combined penalty capped: vix_adj {vix_adj} -> {combined_cap} "
                    f"(IV penalty already applied to raw score)"
                )
                vix_adj = combined_cap

        effective_min_score = CONFIG.get("high_prob_min_score", 18) + vix_adj
        if score < effective_min_score:
            high_prob_rejects.append(
                f"score below VIX-adjusted high-probability minimum "
                f"({effective_min_score}, base {CONFIG.get('high_prob_min_score', 18)}, "
                f"vix:{vix_regime.get('reason', 'n/a')})"
            )
        elif vix_adj != 0:
            reasons.append(f"VIX regime adjustment applied: vix_adj={vix_adj} ({vix_regime.get('reason')})")

        # Block high-prob promotion if win-rate filter forced watchlist.
        if win_rate_filter.get("force_watchlist"):
            high_prob_rejects.append("win-rate filter forced watchlist for this bucket")

        if high_prob_rejects:
            reasons.append("High-probability reject; downgraded to watchlist candidate if still strong: " + "; ".join(high_prob_rejects))
        else:
            high_probability_passed = True
            reasons.append("HIGH-PROBABILITY MODE PASSED")

    # Final clean categorization: TRADE, WATCHLIST, or IGNORE.
    # v17.1: combine all force_watchlist signals (win-rate filter and the new
    # structure detector both can demote to watchlist).
    combined_force_watchlist = (
        win_rate_filter.get("force_watchlist", False)
        or structure_force_watchlist
    )
    category = categorize_option_signal(
        score,
        confirmations,
        premium,
        high_probability_passed,
        force_watchlist=combined_force_watchlist,
    )

    if category == "IGNORE":
        return None

    decision = category

    if decision == "WATCHLIST":
        reasons.append("Categorized as WATCHLIST: good setup, but not enough quality for execution alert")
    else:
        reasons.append("Categorized as TRADE: execution-quality setup")

    target = round(entry * CONFIG["option_target_multiplier"], 2)
    stop = round(entry * CONFIG["option_stop_loss_multiplier"], 2)
    # v17.5: also compute a low/high target range for display in alerts.
    # The single `target` is kept for backward compat with the EOD outcome
    # labeler and backtest scoring; the range is purely for the Telegram alert.
    target_low_mult = CONFIG.get("option_target_low_multiplier", CONFIG["option_target_multiplier"])
    target_high_mult = CONFIG.get("option_target_high_multiplier",
                                  CONFIG["option_target_multiplier"] + 0.15)
    target_low = round(entry * target_low_mult, 2)
    target_high = round(entry * target_high_mult, 2)
    target_low_pct = round((target_low_mult - 1) * 100, 0)
    target_high_pct = round((target_high_mult - 1) * 100, 0)
    stop_pct = round((1 - CONFIG["option_stop_loss_multiplier"]) * 100, 0)

    # v16: Build the signal dict, then classify setup type using its contents.
    signal_dict = {
        "ticker": ticker,
        "source": rec["source"],
        "option_type": option_type,
        "direction": direction,
        "strike": rec["strike"],
        "expiry": rec["expiry"],
        "dte": dte,
        "entry": round(entry, 2),
        "target": target,
        # v17.5: target range for alert display.
        "target_low": target_low,
        "target_high": target_high,
        "target_low_pct": target_low_pct,
        "target_high_pct": target_high_pct,
        "stop_pct": stop_pct,
        "stop": stop,
        "premium": premium,
        "volume": volume,
        "oi": oi,
        "vol_oi": vol_oi,
        "delta": rec.get("delta", 0),
        "iv": rec.get("iv", 0),
        "code": code,
        "score": score,
        "decision": decision,
        "confirmations": confirmations,
        "confirmation_count": len(confirmations),
        "signal_quality": "HIGH_PROBABILITY" if high_probability_passed else ("WATCHLIST" if decision == "WATCHLIST" else "STANDARD"),
        "reasons": reasons,
        "ticker_bias": b,
        "price_reaction": price_reaction,
        "cluster_stats": cluster_stats,
        "unusual_whales_confirmation": uw_confirmation,
        "win_rate_filter": win_rate_filter,
        # v17.1: structural-trade detection metadata.
        "structure_check": structure_check,
        # v16 fields:
        "iv_data": iv_data if 'iv_data' in dir() else {},
        "vix_regime": get_vix_regime(),
        "row": rec["row"],
        "signal_key": f"OPTION_{ticker}_{rec['expiry']}_{rec['strike']}_{option_type}_{get_market_now().strftime('%Y-%m-%d')}",
    }

    signal_dict["setup_type"] = classify_setup_type(
        signal_dict, rec, cluster_stats, price_reaction,
        related_flow_premium=related_flow_premium,
    )
    reasons.append(f"Setup type: {signal_dict['setup_type']}")

    # v16: Mark detection timestamp now that the candidate is fully built.
    mark_signal_detected(signal_dict["signal_key"])

    return signal_dict


def _conviction_label(signal):
    """
    v17.5.2: Maps decision + score into a HIGH/MEDIUM/LOW label for traders.
    Combines two things the bot already knows:
      - Decision tier (HIGH_CONVICTION / TRADE / WATCHLIST)
      - Score relative to max
    A score of 40+/45 in a TRADE tier signal is roughly equivalent to a
    HIGH_CONVICTION tier signal at the lower end, so we treat it as HIGH.
    """
    decision = str(signal.get("decision", "")).upper().strip()
    score = signal.get("score", 0) or 0
    max_score = CONFIG.get("option_signal_max_score", 45)
    score_pct = (score / max_score) if max_score > 0 else 0

    if decision == "HIGH_CONVICTION":
        return "HIGH"
    if decision == "TRADE":
        if score_pct >= 0.85:  # 38+ out of 45
            return "HIGH"
        if score_pct >= 0.55:  # 25+ out of 45
            return "MEDIUM"
        return "LOW"
    if decision == "WATCHLIST":
        return "LOW"
    return "LOW"


def _is_aggressive_flow(signal):
    """
    v17.5.2: True if the trade was at-ask / sweep / tape-leading — i.e.,
    aggressive buying behavior. Uses the same flow-code set the scoring
    layer uses (default: SLAN, TLAT, MLAT, TLCT).
    """
    code = str(signal.get("code", "")).upper().strip()
    aggressive_codes = CONFIG.get("aggressive_flow_codes", ["SLAN", "TLAT", "MLAT", "TLCT"])
    return code in aggressive_codes


def _format_premium(premium):
    """Format premium value as $X.XK / $X.XM for compact display."""
    try:
        p = float(premium or 0)
    except (TypeError, ValueError):
        return "n/a"
    if p <= 0:
        return "n/a"
    if p >= 1_000_000:
        return f"${p/1_000_000:.2f}M"
    if p >= 1_000:
        return f"${p/1_000:.0f}K"
    return f"${p:.0f}"


def build_concise_option_signal_message(signal):
    """
    v17.5: Concise paid-channel format. Strikes only the essentials:
    ticker, type, strike, expiry, entry, target range, stop, chase context.
    Full detail (reasons, score breakdown, setup bullets) lives in Excel.

    v17.5.2: also surfaces institutional-flow context — premium $, multiple
    prints flag, aggressive buying indicator, and a HIGH/MED/LOW conviction
    label — so the trader can weight the signal at a glance.
    """
    option_type = str(signal.get("option_type", "")).upper()
    opt = "C" if option_type == "CALL" else "P"
    category = "WATCHLIST" if signal.get("decision") == "WATCHLIST" else "TRADE"
    brand = CONFIG.get("signal_brand_name", "Pal Trading Signals")

    entry = signal.get("entry")
    target_low = signal.get("target_low")
    target_high = signal.get("target_high")
    target_low_pct = signal.get("target_low_pct", 25)
    target_high_pct = signal.get("target_high_pct", 40)
    stop = signal.get("stop")
    stop_pct = signal.get("stop_pct", 25)
    chase = signal.get("chase_context", {}) or {}

    # Header — visual flag if chase is severe.
    chase_warn_pct = float(CONFIG.get("chase_warn_pct", 15.0))
    chase_block_pct = float(CONFIG.get("chase_block_pct", 25.0))
    chase_pct = chase.get("chase_pct") if chase.get("available") else None
    header_warn = ""
    if chase_pct is not None:
        if chase_pct >= chase_block_pct:
            header_warn = "⚠️🛑 LATE ENTRY — likely skip\n"
        elif chase_pct >= chase_warn_pct:
            header_warn = "⚠️ Late entry — consider waiting\n"

    if category == "WATCHLIST":
        title = "👀 Watchlist Setup"
    else:
        title = "🚀 Option Trade"

    lines = [
        f"{header_warn}{title}  •  {brand}",
        "",
        f"{signal['ticker']}  {signal['strike']}{opt}  exp {signal['expiry']}",
        f"Entry: {format_price_value(entry)}",
        f"Target: +{int(target_low_pct)}% to +{int(target_high_pct)}%  "
        f"(≈ {format_price_value(target_low)} – {format_price_value(target_high)})",
        f"Stop:   -{int(stop_pct)}%  (≈ {format_price_value(stop)})",
    ]

    # v17.5.2: institutional flow context block.
    # Shows premium value, multiple-print indicator, aggressive-buying flag,
    # and conviction level — all from data the bot already computed.
    conviction = _conviction_label(signal)
    premium_str = _format_premium(signal.get("premium"))
    aggressive = _is_aggressive_flow(signal)

    cluster = signal.get("cluster_stats") or {}
    same_contract_count = int(cluster.get("same_contract_count", 0) or 0)
    same_side_count = int(cluster.get("same_ticker_side_count", 0) or 0)
    split_min = CONFIG.get("split_order_min_count", 3)
    multi_print = same_contract_count >= split_min or same_side_count >= split_min

    conviction_marker = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "⚪"}.get(conviction, "")

    lines.append("")
    lines.append("📊 Flow:")
    lines.append(f"  Premium: {premium_str}")
    if multi_print:
        if same_contract_count >= split_min:
            lines.append(f"  Multiple prints: ✅ {same_contract_count} on same contract")
        else:
            lines.append(f"  Multiple prints: ✅ {same_side_count} on same ticker/side")
    else:
        lines.append(f"  Multiple prints: ❌ single print")
    lines.append(f"  Aggressive buying: {'✅ at-ask / sweep' if aggressive else '❌ no'}")
    lines.append(f"  Conviction: {conviction_marker} {conviction}")

    # Live quote block — only show if available.
    if chase.get("available"):
        bid = chase.get("current_bid")
        ask = chase.get("current_ask")
        mid = chase.get("current_mid")
        if bid is not None and ask is not None:
            lines.append("")
            lines.append(f"📡 Live: {format_price_value(bid)} / {format_price_value(ask)} "
                         f"(mid {format_price_value(mid)})")
            if chase_pct is not None:
                sign = "+" if chase_pct >= 0 else ""
                if chase_pct >= chase_block_pct:
                    marker = "🛑 STOP CHASE"
                elif chase_pct >= chase_warn_pct:
                    marker = "⚠️ caution"
                else:
                    marker = "✅"
                lines.append(f"Chase: {sign}{chase_pct}%  {marker}")
    elif chase and not chase.get("available"):
        lines.append("")
        lines.append(f"📡 Live quote unavailable: {chase.get('reason', 'n/a')[:60]}")

    return "\n".join(lines)


def build_institutional_option_message(signal):
    """
    v17.5: Dispatches to concise or verbose format based on
    SIGNAL_FORMAT_CONCISE config (default True for clean trader UX).
    Full reasoning detail always lives in signals.xlsx regardless of format.
    """
    if CONFIG.get("signal_format_concise", True):
        return build_concise_option_signal_message(signal)

    # Verbose legacy format.
    option_type = str(signal.get("option_type", "")).upper()
    opt = "C" if option_type == "CALL" else "P"
    category = "WATCHLIST" if signal.get("decision") == "WATCHLIST" else "TRADE"
    max_score = CONFIG.get("option_signal_max_score", 45)
    score_value = signal.get("score", 0)
    emoji = get_option_confidence_emoji(score_value, max_score, signal.get("signal_type"))
    win_probability = estimate_option_win_probability(score_value, max_score)
    position_size = recommend_option_position_size(score_value, max_score)
    setup_bullets = build_option_setup_bullets(signal=signal)

    return build_professional_option_signal_message(
        ticker=signal["ticker"],
        option_type=option_type,
        strike=f"{signal['strike']}{opt}",
        expiry=signal["expiry"],
        entry=signal["entry"],
        target=signal["target"],
        stop=signal["stop"],
        premium=signal["premium"],
        score=score_value,
        max_score=max_score,
        win_probability=win_probability,
        position_size=position_size,
        category=category,
        emoji=emoji,
        setup_bullets=setup_bullets,
        chase_context=signal.get("chase_context"),
    )

def save_institutional_option_signal_to_excel(signal):
    init_excel()
    wb = load_workbook(CONFIG["excel_file"])
    ws = wb["OptionSignals"]

    # v16: read header map so we write to the correct column even after migration.
    headers = [str(cell.value) if cell.value is not None else "" for cell in ws[1]]
    header_map = {name: idx for idx, name in enumerate(headers) if name}

    iv_data = signal.get("iv_data", {}) or {}
    vix_regime = signal.get("vix_regime", {}) or {}

    base_row = [
        get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        signal["ticker"],
        signal["decision"],
        signal["score"],
        signal["option_type"],
        signal["strike"],
        signal["expiry"],
        signal["entry"],
        signal["stop"],
        signal["target"],
        round(signal["entry"] * 1.70, 2) if signal["entry"] else "",
        signal["premium"],
        signal["volume"],
        signal["oi"],
        round(signal["vol_oi"], 2) if signal["vol_oi"] else "",
        signal["dte"],
        "INSTITUTIONAL_ENGINE",
        "-",
        "-",
        ", ".join(signal["reasons"]),
        "OPEN",
        "",
        "",
        ""
    ]

    # Build a row matching the actual sheet width.
    full_row = [""] * len(headers)
    base_columns = [
        "DateTime", "Ticker", "Decision", "Score", "Option Type", "Strike", "Expiry",
        "Entry", "Stop Loss", "Target 1", "Target 2", "Premium", "Volume", "OI",
        "Vol/OI", "DTE", "Chain Status", "Call Wall", "Put Wall", "Reasons",
        "Status", "Success Date", "Exit Price", "Result"
    ]
    for col_name, value in zip(base_columns, base_row):
        if col_name in header_map:
            full_row[header_map[col_name]] = value

    # v16 columns:
    if "Setup Type" in header_map:
        full_row[header_map["Setup Type"]] = signal.get("setup_type", "unclassified")
    if "IV %ile" in header_map:
        full_row[header_map["IV %ile"]] = iv_data.get("percentile", "")
    if "VIX Regime" in header_map:
        full_row[header_map["VIX Regime"]] = (
            f"{vix_regime.get('regime', 'n/a')} ({vix_regime.get('vix_level', '')})"
        )
    if "Latency (s)" in header_map:
        latency = signal.get("latency_seconds")
        full_row[header_map["Latency (s)"]] = round(latency, 2) if latency is not None else ""

    ws.append(full_row)
    wb.save(CONFIG["excel_file"])


# ============================================================
# v16 ACCURACY MODULES
# ============================================================
# These modules add: IV percentile gating, setup-type tagging,
# VIX regime filter, signal latency tracking, robust EOD outcome
# labeler, and a backtest harness against signals.xlsx history.
#
# Yahoo intraday is still the underlying data source. The biggest
# remaining accuracy improvement is replacing it with Schwab
# streaming — there is a TODO marker in get_enhanced_price_reaction
# where that swap should happen.
# ============================================================

# ----- A. IV PERCENTILE GATING --------------------------------

# Cache so we don't refetch IV history for the same ticker on every signal.
_IV_PERCENTILE_CACHE = {}
_IV_PERCENTILE_CACHE_TTL_SECONDS = 60 * 60  # refresh hourly during a session


def get_iv_percentile(ticker, current_iv=None, lookback_days=252):
    """
    Returns the percentile of the ticker's current IV vs its trailing
    `lookback_days` distribution. Used to gate signals against IV crush risk.

    Yahoo doesn't expose historical IV directly, so we approximate using
    realized volatility (close-to-close) from daily OHLCV. This is a known
    proxy — not perfect, but it tracks the same regime shifts.

    Returns dict:
      - percentile: 0–100 (None if data unavailable)
      - rv_mean / rv_std / rv_now: the distribution stats
      - current_iv: echoed back if provided
      - reason: human-readable explanation
    """
    if not CONFIG.get("enable_iv_percentile", True):
        return {"percentile": None, "reason": "IV percentile disabled"}

    cache_key = ticker.upper().strip()
    now_ts = time.time()
    cached = _IV_PERCENTILE_CACHE.get(cache_key)
    if cached and (now_ts - cached["fetched_at"]) < _IV_PERCENTILE_CACHE_TTL_SECONDS:
        result = dict(cached["result"])
        result["current_iv"] = current_iv
        return result

    try:
        df = yf.Ticker(ticker).history(period=f"{lookback_days + 30}d", interval="1d", auto_adjust=False)
        if df is None or df.empty or len(df) < 60:
            result = {"percentile": None, "reason": "Insufficient daily history for IV proxy"}
        else:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            log_returns = (df["Close"] / df["Close"].shift(1)).apply(lambda x: pd.NA if x is None or x <= 0 else x)
            log_returns = log_returns.dropna()
            # 20-day rolling realized vol, annualized.
            rv_series = log_returns.rolling(window=20).std() * (252 ** 0.5) * 100
            rv_series = rv_series.dropna()
            # v16.1: require 90+ samples instead of 30. Below that, percentile
            # is too noisy on recent IPOs / spinoffs and produces misleading
            # extreme readings on a thin sample.
            min_samples = CONFIG.get("iv_pct_min_samples", 90)
            if len(rv_series) < min_samples:
                result = {"percentile": None, "reason": f"Insufficient RV samples ({len(rv_series)} < {min_samples})"}
            else:
                rv_now = float(rv_series.iloc[-1])
                rv_dist = rv_series.tail(lookback_days)
                pct = float((rv_dist <= rv_now).sum() / len(rv_dist) * 100)
                result = {
                    "percentile": round(pct, 1),
                    "rv_now": round(rv_now, 2),
                    "rv_mean": round(float(rv_dist.mean()), 2),
                    "rv_std": round(float(rv_dist.std()), 2),
                    "samples": len(rv_dist),
                    "reason": f"RV percentile {round(pct, 1)} over {len(rv_dist)} days",
                }
    except Exception as e:
        result = {"percentile": None, "reason": f"IV percentile error: {e}"}

    _IV_PERCENTILE_CACHE[cache_key] = {"fetched_at": now_ts, "result": dict(result)}
    result["current_iv"] = current_iv
    return result


def score_iv_environment(ticker, current_iv, option_type, is_buying_premium=True):
    """
    Returns score adjustment based on IV percentile.

    Two calibration modes, selected by CONFIG["iv_pct_empirical_calibration"]:

    THEORETICAL (default; matches v16.x and prior behavior):
      For premium buyers:
        - IV pct > 70: penalty (IV crush risk after the move)
        - IV pct < 30: bonus (cheap premium relative to history)
      For premium sellers: inverted.

    EMPIRICAL (v17.x; opt-in via env IV_PCT_EMPIRICAL_CALIBRATION=true):
      Based on 21-day signals log analysis (n=515 credible outcomes):
        IV %ile 0-25   : 39.8% win rate
        IV %ile 25-50  : 43.0% win rate
        IV %ile 50-75  : 43.2% win rate
        IV %ile 75-100 : 67.9% win rate
      The penalty-on-high-IV theory does NOT survive contact with this
      signal source. The "IV percentile" here is realized-vol-based, so
      a high reading means "RV regime is elevated" — which is precisely
      when directional flow theses pay off. Empirical sign:
        - IV pct >= very_high_threshold (default 85): BONUS
        - IV pct >= high_threshold (default 70):     BONUS
        - IV pct <= low_threshold (default 30):      PENALTY

    v16.1 fix: skip the high-IV adjustment when a known catalyst (earnings)
    is within 14 days. Pre-catalyst IV is correctly priced and shouldn't
    be double-counted with the separate earnings filter.

    Returns: (score_adjustment, reason_string, iv_data_dict)
    """
    iv_data = get_iv_percentile(ticker, current_iv=current_iv)
    pct = iv_data.get("percentile")
    if pct is None:
        return 0, f"IV%ile unavailable: {iv_data.get('reason', 'no data')}", iv_data

    high_threshold = CONFIG.get("iv_pct_high_threshold", 70)
    very_high_threshold = CONFIG.get("iv_pct_very_high_threshold", 85)
    low_threshold = CONFIG.get("iv_pct_low_threshold", 30)

    # Catalyst awareness — high IV is expected near earnings.
    catalyst_window_days = CONFIG.get("iv_pct_catalyst_skip_days", 14)
    catalyst_near = False
    try:
        earnings = get_earnings_risk(ticker)
        days_to_er = clean_number(earnings.get("days_to_earnings", 999))
        if earnings.get("earnings_near") and days_to_er <= catalyst_window_days:
            catalyst_near = True
            iv_data["catalyst_near"] = True
            iv_data["days_to_earnings"] = days_to_er
    except Exception:
        pass

    empirical = CONFIG.get("iv_pct_empirical_calibration", False)

    if is_buying_premium:
        if empirical:
            # v17.x empirical sign — high IV%ile is a positive for this signal source.
            if catalyst_near and pct >= high_threshold:
                return 0, f"IV%ile {pct} elevated, catalyst within {catalyst_window_days}d (no adj)", iv_data
            if pct >= very_high_threshold:
                return CONFIG.get("iv_pct_empirical_very_high_bonus", 4), \
                       f"IV%ile {pct} VERY HIGH — empirical edge (emp.cal)", iv_data
            if pct >= high_threshold:
                return CONFIG.get("iv_pct_empirical_high_bonus", 2), \
                       f"IV%ile {pct} elevated — empirical edge (emp.cal)", iv_data
            if pct <= low_threshold:
                return CONFIG.get("iv_pct_empirical_low_penalty", -3), \
                       f"IV%ile {pct} low — empirical underperformance (emp.cal)", iv_data
            return 0, f"IV%ile {pct} neutral (emp.cal)", iv_data
        else:
            # Theoretical sign (legacy v16.x behavior, kept for rollback).
            if catalyst_near and pct >= high_threshold:
                return 0, f"IV%ile {pct} elevated but catalyst within {catalyst_window_days}d (no penalty)", iv_data
            if pct >= very_high_threshold:
                return CONFIG.get("iv_pct_very_high_penalty", -4), f"IV%ile {pct} VERY HIGH — major crush risk", iv_data
            if pct >= high_threshold:
                return CONFIG.get("iv_pct_high_penalty", -2), f"IV%ile {pct} elevated — crush risk", iv_data
            if pct <= low_threshold:
                return CONFIG.get("iv_pct_low_bonus", 2), f"IV%ile {pct} low — cheap premium", iv_data
            return 0, f"IV%ile {pct} neutral", iv_data
    else:
        # Premium sellers — unchanged. High IV is good, low IV is bad.
        if pct >= high_threshold:
            return CONFIG.get("iv_pct_high_bonus_seller", 2), f"IV%ile {pct} elevated — good for sellers", iv_data
        if pct <= low_threshold:
            return CONFIG.get("iv_pct_low_penalty_seller", -2), f"IV%ile {pct} low — sellers underpaid", iv_data
        return 0, f"IV%ile {pct} neutral", iv_data


# ----- B. SETUP-TYPE TAGGING ----------------------------------

def classify_setup_type(signal, rec, cluster_stats, price_reaction, related_flow_premium):
    """
    Tags each signal with a setup type so we can track win rates per type
    rather than treating all 'unusual flow' as one strategy.

    Setup types:
      - sweep_momentum:    aggressive sweep code + intraday momentum
      - block_position:    large premium, no momentum requirement, longer DTE
      - gamma_squeeze:     short DTE, very high vol/OI, low delta
      - earnings_play:     earnings within 7 days
      - end_of_day_add:    afternoon institutional position add
      - unclassified:      didn't fit any pattern cleanly
    """
    dte = signal.get("dte", 0) or 0
    premium = signal.get("premium", 0) or 0
    delta = abs(signal.get("delta", 0) or 0)
    vol_oi = signal.get("vol_oi", 0) or 0
    code = str(signal.get("code", "")).upper()
    aggressive = code in CONFIG.get("aggressive_flow_codes", [])

    # Earnings check first — overrides other classifications because IV
    # behavior around earnings is fundamentally different.
    try:
        earnings = get_earnings_risk(signal.get("ticker"))
        days_to_er = clean_number(earnings.get("days_to_earnings", 999))
        if earnings.get("earnings_near") and days_to_er <= 7:
            return "earnings_play"
    except Exception:
        pass

    # Gamma squeeze: short DTE, high vol/OI, low delta = OTM lottery
    if dte <= 5 and vol_oi >= CONFIG.get("option_vol_oi_aggressive", 3) and delta and delta < 0.30:
        return "gamma_squeeze"

    # Sweep momentum: aggressive code + price already moving in direction
    if aggressive and price_reaction.get("confirmed") and price_reaction.get("breakout_aligned"):
        return "sweep_momentum"

    # End-of-day add: afternoon timestamp + large premium + longer DTE
    now = get_market_now()
    afternoon_hour_threshold = 14  # 2pm ET
    if (
        now.hour >= afternoon_hour_threshold
        and premium >= CONFIG.get("option_premium_large", 500000)
        and dte >= 14
    ):
        return "end_of_day_add"

    # Block position: large premium, longer DTE, doesn't require momentum
    if premium >= CONFIG.get("option_premium_large", 500000) and dte >= 14:
        return "block_position"

    return "unclassified"


# ----- C. VIX REGIME FILTER -----------------------------------

_VIX_REGIME_CACHE = {"fetched_at": 0, "data": None}
# v16.1: tightened from 5min to 60s. The whole point of the VIX filter is to
# catch fast regime shifts; a 5-minute stale cache let signals through using
# pre-spike VIX values during fast moves.
_VIX_REGIME_CACHE_TTL = 60


def get_vix_regime():
    """
    Fetches current VIX level and returns regime classification.

    Why this matters: bullish flow into a rising-VIX tape has measurably
    lower hit rates than the same flow into calm conditions. This adjusts
    the high-probability score floor dynamically rather than using a fixed
    threshold for all market conditions.

    Returns dict:
      - vix_level: current VIX
      - vix_change_pct: today's % change
      - regime: 'calm' | 'normal' | 'elevated' | 'panic'
      - score_adjustment: int to add to high_prob_min_score floor
      - reason: human-readable
    """
    if not CONFIG.get("enable_vix_regime_filter", True):
        return {"regime": "disabled", "score_adjustment": 0, "reason": "VIX filter disabled"}

    now_ts = time.time()
    if _VIX_REGIME_CACHE["data"] and (now_ts - _VIX_REGIME_CACHE["fetched_at"]) < _VIX_REGIME_CACHE_TTL:
        return _VIX_REGIME_CACHE["data"]

    try:
        df = yf.Ticker("^VIX").history(period="2d", interval="5m", auto_adjust=False)
        if df is None or df.empty:
            data = {"regime": "unknown", "score_adjustment": 0, "reason": "VIX data unavailable"}
        else:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            vix_now = float(df["Close"].iloc[-1])

            # Try to get yesterday's close for % change.
            try:
                daily = yf.Ticker("^VIX").history(period="5d", interval="1d", auto_adjust=False)
                if isinstance(daily.columns, pd.MultiIndex):
                    daily.columns = daily.columns.get_level_values(0)
                daily = daily.dropna(subset=["Close"])
                prev_close = float(daily["Close"].iloc[-2]) if len(daily) >= 2 else vix_now
                vix_change_pct = ((vix_now - prev_close) / prev_close) * 100 if prev_close else 0
            except Exception:
                vix_change_pct = 0

            calm_max = CONFIG.get("vix_calm_max", 15)
            normal_max = CONFIG.get("vix_normal_max", 20)
            elevated_max = CONFIG.get("vix_elevated_max", 28)

            if vix_now < calm_max:
                regime = "calm"
                adjustment = CONFIG.get("vix_calm_score_adj", -1)  # easier to hit high-prob in calm
            elif vix_now < normal_max:
                regime = "normal"
                adjustment = 0
            elif vix_now < elevated_max:
                regime = "elevated"
                adjustment = CONFIG.get("vix_elevated_score_adj", 2)
            else:
                regime = "panic"
                adjustment = CONFIG.get("vix_panic_score_adj", 4)

            # Spike day adds extra penalty regardless of absolute level.
            if vix_change_pct > CONFIG.get("vix_spike_change_pct", 10):
                adjustment += CONFIG.get("vix_spike_score_adj", 2)

            data = {
                "vix_level": round(vix_now, 2),
                "vix_change_pct": round(vix_change_pct, 2),
                "regime": regime,
                "score_adjustment": adjustment,
                "reason": f"VIX {round(vix_now, 2)} ({regime}, {round(vix_change_pct, 1):+}%)",
            }
    except Exception as e:
        data = {"regime": "error", "score_adjustment": 0, "reason": f"VIX fetch error: {e}"}

    _VIX_REGIME_CACHE["fetched_at"] = now_ts
    _VIX_REGIME_CACHE["data"] = data
    return data


def get_effective_high_prob_min_score():
    """Returns the high_prob_min_score adjusted for the current VIX regime."""
    base = CONFIG.get("high_prob_min_score", 18)
    regime = get_vix_regime()
    return base + int(regime.get("score_adjustment", 0))


# ----- D. SIGNAL LATENCY TRACKING -----------------------------

_SIGNAL_TIMESTAMPS = {}  # signal_key -> {"detected_at": ts, "sent_at": ts}


def mark_signal_detected(signal_key):
    """Call this the moment a signal is first identified as a candidate."""
    if not CONFIG.get("enable_latency_tracking", True):
        return
    _SIGNAL_TIMESTAMPS.setdefault(signal_key, {})["detected_at"] = time.time()


def mark_signal_sent(signal_key):
    """Call this immediately after the Telegram alert is sent."""
    if not CONFIG.get("enable_latency_tracking", True):
        return
    entry = _SIGNAL_TIMESTAMPS.setdefault(signal_key, {})
    entry["sent_at"] = time.time()
    detected = entry.get("detected_at")
    if detected:
        latency_seconds = entry["sent_at"] - detected
        threshold = CONFIG.get("latency_warning_seconds", 60)
        if latency_seconds > threshold:
            print(f"⚠ LATENCY WARNING: {signal_key} took {latency_seconds:.1f}s "
                  f"detect-to-send (threshold {threshold}s)")
        else:
            print(f"latency: {signal_key} {latency_seconds:.1f}s")


def get_signal_latency(signal_key):
    """Returns latency in seconds, or None if not both timestamps recorded."""
    entry = _SIGNAL_TIMESTAMPS.get(signal_key)
    if not entry or "detected_at" not in entry or "sent_at" not in entry:
        return None
    return entry["sent_at"] - entry["detected_at"]


# ----- E. ROBUST EOD OUTCOME LABELER --------------------------

def run_eod_outcome_labeler(force=False):
    """
    Runs end-of-day to force every OPEN signal to a clean outcome label.

    Unlike monitor_open_option_signals (which can leave signals in OPEN
    state if the bot crashes mid-loop), this is idempotent and robust:
    every OPEN signal older than today gets resolved to one of:
      - TARGET_HIT
      - STOP_HIT
      - EXPIRED_OTM    (option expiry passed without target/stop)
      - STILL_OPEN     (option not yet expired, no target/stop hit)
      - UNRESOLVED     (data unavailable for resolution)

    Call from main() once per day after market close.
    """
    if not CONFIG.get("enable_eod_labeler", True) and not force:
        return {"labeled": 0, "skipped": 0, "reason": "EOD labeler disabled"}

    excel_file = CONFIG["excel_file"]
    if not os.path.exists(excel_file):
        return {"labeled": 0, "skipped": 0, "reason": "Excel file not found"}

    try:
        wb = load_workbook(excel_file)
        if "OptionSignals" not in wb.sheetnames:
            return {"labeled": 0, "skipped": 0, "reason": "OptionSignals sheet missing"}

        ws = wb["OptionSignals"]
        headers = [cell.value for cell in ws[1]]
        header_map = {str(name): idx + 1 for idx, name in enumerate(headers) if name}

        required = ["DateTime", "Ticker", "Option Type", "Strike", "Expiry", "Entry",
                    "Stop Loss", "Target 1", "Status", "Result"]
        for col in required:
            if col not in header_map:
                return {"labeled": 0, "skipped": 0, "reason": f"missing column {col}"}

        labeled = 0
        skipped = 0
        today = get_market_now().date()

        for row_num in range(2, ws.max_row + 1):
            status = str(ws.cell(row_num, header_map["Status"]).value or "").upper().strip()
            if status not in ["OPEN", "MONITOR_ERROR"]:
                continue

            signal_dt_raw = str(ws.cell(row_num, header_map["DateTime"]).value or "").strip()
            signal_date = None
            for fmt in ["%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    signal_date = datetime.strptime(signal_dt_raw.split(" ")[0], "%Y-%m-%d").date()
                    break
                except Exception:
                    continue
            if signal_date is None:
                skipped += 1
                continue

            # Don't touch today's signals — they're still live.
            if signal_date >= today:
                continue

            ticker = str(ws.cell(row_num, header_map["Ticker"]).value or "").upper().strip()
            option_type = str(ws.cell(row_num, header_map["Option Type"]).value or "").upper().strip()
            strike = clean_number(ws.cell(row_num, header_map["Strike"]).value)
            expiry_raw = str(ws.cell(row_num, header_map["Expiry"]).value or "").strip()
            entry = clean_number(ws.cell(row_num, header_map["Entry"]).value)
            stop_loss = clean_number(ws.cell(row_num, header_map["Stop Loss"]).value)
            target_1 = clean_number(ws.cell(row_num, header_map["Target 1"]).value)

            if not ticker or strike <= 0:
                skipped += 1
                continue

            # Parse expiry to determine if option has expired.
            expiry_date = None
            for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"]:
                try:
                    expiry_date = datetime.strptime(expiry_raw, fmt).date()
                    break
                except Exception:
                    continue

            try:
                # Pull daily history from signal date through today (or expiry).
                end_date = expiry_date if (expiry_date and expiry_date <= today) else today
                start_date = signal_date
                df = yf.Ticker(ticker).history(
                    start=start_date.strftime("%Y-%m-%d"),
                    end=(end_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    interval="1d",
                    auto_adjust=False,
                )
                if df is None or df.empty:
                    ws.cell(row_num, header_map["Status"]).value = "UNRESOLVED"
                    ws.cell(row_num, header_map["Result"]).value = "No price history available"
                    skipped += 1
                    continue

                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                # Determine if target or stop was ever hit during the holding period.
                # We use underlying price vs strike as a proxy (same approach as
                # monitor_open_option_signals — limited but consistent).
                period_high = float(df["High"].max())
                period_low = float(df["Low"].min())
                final_close = float(df["Close"].iloc[-1])

                result = None
                if "CALL" in option_type:
                    if period_high >= strike * 1.01:
                        result = "TARGET_HIT"
                    elif period_low <= strike * 0.985:
                        result = "STOP_HIT"
                elif "PUT" in option_type:
                    if period_low <= strike * 0.99:
                        result = "TARGET_HIT"
                    elif period_high >= strike * 1.015:
                        result = "STOP_HIT"

                if result is None:
                    if expiry_date and expiry_date <= today:
                        # Expired without target/stop — check if ITM at close.
                        if "CALL" in option_type:
                            result = "TARGET_HIT" if final_close > strike else "EXPIRED_OTM"
                        elif "PUT" in option_type:
                            result = "TARGET_HIT" if final_close < strike else "EXPIRED_OTM"
                        else:
                            result = "EXPIRED_OTM"
                    else:
                        result = "STILL_OPEN"

                ws.cell(row_num, header_map["Status"]).value = result
                ws.cell(row_num, header_map["Result"]).value = result
                if "Exit Price" in header_map:
                    ws.cell(row_num, header_map["Exit Price"]).value = round(final_close, 2)
                if "Success Date" in header_map:
                    ws.cell(row_num, header_map["Success Date"]).value = (
                        get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z")
                    )
                labeled += 1

            except Exception as e:
                ws.cell(row_num, header_map["Status"]).value = "UNRESOLVED"
                ws.cell(row_num, header_map["Result"]).value = f"Labeler error: {str(e)[:150]}"
                skipped += 1

        wb.save(excel_file)
        print(f"EOD outcome labeler complete. Labeled: {labeled}, Skipped: {skipped}")
        return {"labeled": labeled, "skipped": skipped, "reason": "ok"}

    except Exception as e:
        print(f"EOD labeler error: {e}")
        return {"labeled": 0, "skipped": 0, "reason": str(e)}


# ----- F. BACKTEST HARNESS ------------------------------------

def backtest_signals_history(lookback_days=90, sheet_name="OptionSignals", include_watchlist=False):
    """
    Re-reads signals.xlsx and computes win-rate stats by:
      - decision (TRADE vs WATCHLIST vs HIGH_CONVICTION)
      - score bucket
      - DTE bucket
      - premium bucket
      - setup type (if column present)

    v17.2: by default, watchlist signals are EXCLUDED from headline metrics
    (per-bucket win rates) but kept in raw data and shown in a separate
    "Watchlist Reference (excluded from headline)" section at the end so
    you can still inspect their behavior without polluting the main numbers.

    Pass include_watchlist=True to fold them back into the main metrics
    (the old v16 behavior).

    Run from CLI:
        python barchart_pro_bot.py --backtest 60                # excludes watchlist
        python barchart_pro_bot.py --backtest 60 --with-watchlist   # includes
    """
    excel_file = CONFIG["excel_file"]
    if not os.path.exists(excel_file):
        print("Backtest skipped: Excel file not found.")
        return []

    try:
        wb = load_workbook(excel_file, read_only=True)
        if sheet_name not in wb.sheetnames:
            print(f"Backtest skipped: {sheet_name} sheet not found.")
            return []

        ws = wb[sheet_name]
        rows = list(ws.values)
        if len(rows) < 2:
            print("Backtest skipped: no data rows.")
            return []

        headers = [str(h) if h is not None else "" for h in rows[0]]
        data = [dict(zip(headers, r)) for r in rows[1:]]

        # Filter by lookback window.
        cutoff = get_market_now().date() - pd.Timedelta(days=lookback_days)
        all_in_window = []
        for r in data:
            dt_raw = str(r.get("DateTime", "")).strip()
            try:
                d = datetime.strptime(dt_raw.split(" ")[0], "%Y-%m-%d").date()
                if d >= cutoff:
                    all_in_window.append(r)
            except Exception:
                continue

        if not all_in_window:
            print(f"Backtest: no signals in last {lookback_days} days.")
            return []

        # v17.2: split watchlist from headline data.
        def is_watchlist(r):
            return str(r.get("Decision", "")).strip().upper() == "WATCHLIST"

        if include_watchlist:
            filtered = all_in_window
            watchlist_only = []
        else:
            filtered = [r for r in all_in_window if not is_watchlist(r)]
            watchlist_only = [r for r in all_in_window if is_watchlist(r)]

        def is_win(r):
            result = str(r.get("Result", "")).upper().strip()
            status = str(r.get("Status", "")).upper().strip()
            return "TARGET" in result or "TARGET" in status

        def is_loss(r):
            result = str(r.get("Result", "")).upper().strip()
            status = str(r.get("Status", "")).upper().strip()
            return ("STOP" in result or "STOP" in status
                    or "EXPIRED_OTM" in result or "EXPIRED_OTM" in status)

        def is_resolved(r):
            return is_win(r) or is_loss(r)

        def bucket_score(s):
            try:
                s = float(s)
            except Exception:
                return "unknown"
            if s >= 22: return "score_22+"
            if s >= 18: return "score_18-21"
            if s >= 14: return "score_14-17"
            if s >= 10: return "score_10-13"
            return "score_<10"

        def bucket_dte(d):
            try:
                d = float(d)
            except Exception:
                return "unknown"
            if d <= 2: return "dte_0-2"
            if d <= 7: return "dte_3-7"
            if d <= 14: return "dte_8-14"
            if d <= 30: return "dte_15-30"
            if d <= 45: return "dte_31-45"
            return "dte_46+"

        def bucket_premium(p):
            try:
                p = float(p)
            except Exception:
                return "unknown"
            if p >= 1_000_000: return "prem_1M+"
            if p >= 500_000: return "prem_500K-1M"
            if p >= 200_000: return "prem_200-500K"
            if p >= 100_000: return "prem_100-200K"
            return "prem_<100K"

        groups = {}

        def add_to_group(group_name, key, row):
            g = groups.setdefault(group_name, {}).setdefault(key, {"total": 0, "wins": 0, "losses": 0, "resolved": 0})
            g["total"] += 1
            if is_resolved(row):
                g["resolved"] += 1
                if is_win(row):
                    g["wins"] += 1
                elif is_loss(row):
                    g["losses"] += 1

        for r in filtered:
            decision = str(r.get("Decision", "unknown")).strip() or "unknown"
            score = r.get("Score")
            dte = r.get("DTE")
            premium = r.get("Premium")
            setup_type = str(r.get("Setup Type", "")).strip() or "unclassified"

            add_to_group("decision", decision, r)
            add_to_group("score_bucket", bucket_score(score), r)
            add_to_group("dte_bucket", bucket_dte(dte), r)
            add_to_group("premium_bucket", bucket_premium(premium), r)
            add_to_group("setup_type", setup_type, r)

        # Build report rows.
        report = []
        for group_name, keys in groups.items():
            for key, stats in keys.items():
                resolved = stats["resolved"]
                win_rate = (stats["wins"] / resolved * 100) if resolved else 0
                report.append({
                    "group": group_name,
                    "key": key,
                    "total": stats["total"],
                    "resolved": resolved,
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "win_rate_pct": round(win_rate, 1),
                })

        report.sort(key=lambda x: (x["group"], -x["total"]))

        title = f"BACKTEST: last {lookback_days} days"
        if not include_watchlist:
            title += " (TRADE + HIGH_CONVICTION only — watchlist excluded)"
        print(f"\n========== {title} ==========")
        print(f"Headline signals: {len(filtered)}")
        if not include_watchlist:
            print(f"(Watchlist signals in window: {len(watchlist_only)} — shown separately below)")
        current_group = None
        for row in report:
            if row["group"] != current_group:
                current_group = row["group"]
                print(f"\n--- {current_group} ---")
            print(f"  {row['key']:<20} total:{row['total']:<4} "
                  f"resolved:{row['resolved']:<4} wins:{row['wins']:<4} "
                  f"losses:{row['losses']:<4} win_rate:{row['win_rate_pct']}%")
        print("=========================================================")

        # v17.2: separate watchlist reference section.
        if not include_watchlist and watchlist_only:
            wl_resolved = [r for r in watchlist_only if is_resolved(r)]
            wl_wins = [r for r in wl_resolved if is_win(r)]
            wl_losses = [r for r in wl_resolved if is_loss(r)]
            wl_wr = (len(wl_wins) / len(wl_resolved) * 100) if wl_resolved else 0
            print(f"\n--- Watchlist Reference (excluded from headline) ---")
            print(f"  Total: {len(watchlist_only)}  resolved: {len(wl_resolved)}  "
                  f"wins: {len(wl_wins)}  losses: {len(wl_losses)}  win_rate: {wl_wr:.1f}%")
            print(f"  Use --with-watchlist on the CLI to fold these into headline metrics.")
            print("=========================================================\n")
        else:
            print()

        return report

    except Exception as e:
        print(f"Backtest error: {e}")
        return []


def process_unusual_csv(driver, unusual_csv, flow_df):
    """
    Full institutional option engine.

    Matches screenshot behavior:
    - Large flow premium can create a signal even when Unusual CSV is not the exact same contract.
    - Same ticker + same direction across Flow and Unusual increases conviction.
    - Mixed CALL/PUT activity on the same ticker becomes gamma/straddle, not blind directional trade.
    - AUTO flow is allowed only with larger premium; SLAN/TLAT/MLAT/TLCT are treated as aggressive.
    - Market/news/sector checks are informational score bonuses, not hard blockers.
    """
    try:
        unusual_df = read_barchart_csv(unusual_csv) if unusual_csv else pd.DataFrame()
    except Exception as e:
        print("Could not read unusual CSV:", e)
        unusual_df = pd.DataFrame()

    if unusual_df.empty:
        print("Unusual CSV empty or unreadable.")
    else:
        print("Unusual rows found:", len(unusual_df))

    if flow_df is None:
        flow_df = pd.DataFrame()

    print("Flow rows available:", len(flow_df) if flow_df is not None else 0)

    gamma_sent_items = []

    if CONFIG.get("enable_gamma_detection", True) and unusual_df is not None and not unusual_df.empty:
        gamma_setups = detect_gamma_setups(unusual_df, flow_df)
        print(f"Gamma setups found: {len(gamma_setups)}")

        for gamma_item in gamma_setups[:CONFIG["gamma_max_alerts_per_scan"]]:
            gamma_key = make_gamma_signal_key(gamma_item)
            gamma_full_key = f"{gamma_key}_{get_market_now().strftime('%Y-%m-%d')}"

            if gamma_full_key in GLOBAL_ALERTED_GAMMA or was_gamma_already_sent_today(gamma_key):
                print(f"Duplicate gamma setup skipped: {gamma_key}")
                continue

            if send_option_telegram_message(build_gamma_telegram_message(gamma_item)):
                GLOBAL_ALERTED_GAMMA.add(gamma_full_key)

            save_gamma_signal_to_excel(gamma_item)
            gamma_sent_items.append(gamma_item)
            time.sleep(1)

    unusual_records = normalize_unusual_records(unusual_df)
    flow_records = normalize_flow_records(flow_df)

    ticker_bias = build_ticker_institutional_bias(unusual_records, flow_records)

    print("Top ticker institutional bias:")
    for ticker, b in sorted(ticker_bias.items(), key=lambda x: x[1].get("total_premium", 0), reverse=True)[:15]:
        print(
            ticker,
            "Bias:", b.get("bias"),
            "CallShare:", b.get("call_share"),
            "PutShare:", b.get("put_share"),
            "TotalPremium:", round(b.get("total_premium", 0), 2),
            "TotalVolume:", round(b.get("total_volume", 0), 2),
        )

    candidates = []
    candidate_inputs = []

    if CONFIG.get("prefer_flow_signals_over_unusual", True):
        candidate_inputs.extend(flow_records)
        candidate_inputs.extend(unusual_records)
    else:
        candidate_inputs.extend(unusual_records)
        candidate_inputs.extend(flow_records)

    for rec in candidate_inputs:
        signal = score_institutional_option_candidate(rec, unusual_records, flow_records, ticker_bias)
        if signal:
            candidates.append(signal)

    # Deduplicate by ticker/expiry/strike/type; keep highest score.
    best_by_key = {}
    for signal in candidates:
        key = f"{signal['ticker']}_{signal['expiry']}_{signal['strike']}_{signal['option_type']}"
        if key not in best_by_key or signal["score"] > best_by_key[key]["score"]:
            best_by_key[key] = signal

    candidates = sorted(best_by_key.values(), key=lambda x: (x["score"], x["premium"]), reverse=True)

    if not candidates:
        print("No institutional directional option candidates found this scan.")
        if gamma_sent_items:
            print(f"Gamma alerts sent: {len(gamma_sent_items)}")
        return gamma_sent_items

    max_option_alerts = CONFIG.get("high_prob_max_option_alerts_per_scan", CONFIG["max_option_alerts_per_scan"]) if CONFIG.get("enable_high_probability_mode", True) else CONFIG["max_option_alerts_per_scan"]

    trade_candidates = [s for s in candidates if s.get("decision") == "TRADE"]
    watchlist_candidates = [s for s in candidates if s.get("decision") == "WATCHLIST"]

    top_trades = trade_candidates[:max_option_alerts]
    top_watchlist = watchlist_candidates[:CONFIG.get("max_watchlist_alerts_per_scan", 5)]

    sent_items = list(gamma_sent_items)
    logged_watchlist = []

    # v17.4: helper to enrich signal with live option quote + chase context
    # just before the alert is sent. This converts a stale alert entry
    # (sourced from Barchart's 5-15min delayed CSV) into a decision-grade
    # message that shows where the option is trading RIGHT NOW.
    def _attach_live_chase_context(signal):
        if not CONFIG.get("enable_chase_context", True):
            return
        try:
            from schwab_data import get_option_quote, compute_chase_context
            quote = get_option_quote(
                signal["ticker"],
                signal["expiry"],
                signal["option_type"],
                signal["strike"],
            )
            chase = compute_chase_context(signal.get("entry"), quote)
            signal["chase_context"] = chase
            signal["live_quote"] = quote
            if chase.get("available"):
                cp = chase.get("chase_pct")
                print(f"[chase] {signal['ticker']} {signal['option_type']} "
                      f"{signal['strike']} entry={signal.get('entry')} "
                      f"mid={chase.get('current_mid')} chase={cp}%")
        except ImportError:
            pass
        except Exception as e:
            print(f"[chase] Live quote fetch failed for {signal.get('ticker')}: {e}")

    # TRADE signals: send to Telegram + save to Excel.
    for signal in top_trades:
        signal_key = signal["signal_key"]

        if signal_key in GLOBAL_ALERTED_OPTIONS or was_option_already_sent_today(signal_key):
            print(f"Duplicate institutional TRADE option skipped: {signal_key}")
            continue

        _attach_live_chase_context(signal)

        if send_option_telegram_message(build_institutional_option_message(signal)):
            GLOBAL_ALERTED_OPTIONS.add(signal_key)
            mark_signal_sent(signal_key)
            signal["latency_seconds"] = get_signal_latency(signal_key)

        save_institutional_option_signal_to_excel(signal)
        sent_items.append(signal)
        time.sleep(1)

    # WATCHLIST signals: save to Excel; Telegram only if explicitly enabled.
    for signal in top_watchlist:
        signal_key = signal["signal_key"]

        if signal_key in GLOBAL_ALERTED_OPTIONS or was_option_already_sent_today(signal_key):
            print(f"Duplicate institutional WATCHLIST option skipped: {signal_key}")
            continue

        if CONFIG.get("send_watchlist_to_telegram", False):
            _attach_live_chase_context(signal)
            if send_option_telegram_message(build_institutional_option_message(signal)):
                mark_signal_sent(signal_key)
                signal["latency_seconds"] = get_signal_latency(signal_key)

        GLOBAL_ALERTED_OPTIONS.add(signal_key)
        save_institutional_option_signal_to_excel(signal)
        logged_watchlist.append(signal)
        time.sleep(1)

    print("\n================ INSTITUTIONAL OPTION SUMMARY ================")
    print(f"Unusual normalized records: {len(unusual_records)}")
    print(f"Flow normalized records: {len(flow_records)}")
    print(f"Directional candidates ranked: {len(candidates)}")
    print(f"TRADE candidates: {len(trade_candidates)} | sent: {len(top_trades)}")
    print(f"WATCHLIST candidates: {len(watchlist_candidates)} | logged: {len(logged_watchlist)}")
    print(f"Gamma alerts sent: {len(gamma_sent_items)}")
    print(f"Total option/gamma alerts sent: {len(sent_items)}")

    for signal in (top_trades + top_watchlist):
        print(
            signal["ticker"],
            signal["strike"],
            signal["option_type"],
            "Expiry:", signal["expiry"],
            "DTE:", signal["dte"],
            "Score:", signal["score"],
            "Confirmations:", signal.get("confirmation_count", 0),
            "Decision:", signal["decision"],
            "Source:", signal["source"],
            "Premium:", round(signal["premium"], 2),
            "Bias:", signal["ticker_bias"].get("bias")
        )

    print("==============================================================\n")

    return sent_items + logged_watchlist


# ============================================================
# NEWS MOMENTUM RUNNER SCANNER
# ============================================================

NEWS_RUNNER_BULLISH_KEYWORDS = [
    "fda", "approval", "positive data", "phase 1", "phase 2", "phase 3",
    "trial", "clinical", "contract", "partnership", "collaboration",
    "merger", "acquisition", "buyout", "strategic investment",
    "earnings beat", "raises guidance", "guidance raised", "upgrade",
    "price target raised", "ai", "artificial intelligence", "crypto",
    "bitcoin", "defense", "patent", "commercial launch", "compliance regained",
    "breakthrough", "award", "launch", "record revenue"
]

NEWS_RUNNER_NEGATIVE_KEYWORDS = [
    "offering", "public offering", "registered direct", "atm offering",
    "bankruptcy", "delisting", "sec investigation", "lawsuit",
    "downgrade", "misses estimates", "cuts guidance", "going concern",
    "reverse split"
]


def make_news_runner_key(signal):
    return f"NEWS_RUNNER_{signal['ticker']}_{datetime.now().strftime('%Y-%m-%d')}"


def get_news_runner_float(ticker_obj):
    try:
        info = ticker_obj.get_info()
        shares_float = info.get("floatShares")
        return int(shares_float) if shares_float else None
    except Exception:
        return None


def calculate_intraday_vwap(intraday_df):
    try:
        df = intraday_df.copy()
        df = df[df["Volume"] > 0]

        if df.empty:
            return None

        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
        vwap = (typical_price * df["Volume"]).sum() / df["Volume"].sum()

        return float(vwap)
    except Exception:
        return None


def estimate_recent_spread_pct(intraday_df):
    try:
        recent = intraday_df.tail(5)

        if recent.empty:
            return 999.0

        last_price = float(recent["Close"].dropna().iloc[-1])
        recent_high = float(recent["High"].max())
        recent_low = float(recent["Low"].min())

        if last_price <= 0:
            return 999.0

        return round(((recent_high - recent_low) / last_price) * 100, 2)
    except Exception:
        return 999.0


def get_news_runner_market_data(ticker):
    try:
        ticker_obj = yf.Ticker(ticker)

        daily = ticker_obj.history(period="30d", interval="1d", auto_adjust=False)
        intraday = ticker_obj.history(period="1d", interval="1m", prepost=True, auto_adjust=False)

        if daily.empty or intraday.empty:
            return None

        if isinstance(daily.columns, pd.MultiIndex):
            daily.columns = daily.columns.get_level_values(0)

        if isinstance(intraday.columns, pd.MultiIndex):
            intraday.columns = intraday.columns.get_level_values(0)

        closes = intraday["Close"].dropna()
        if closes.empty or len(daily["Close"].dropna()) < 2:
            return None

        current_price = float(closes.iloc[-1])
        previous_close = float(daily["Close"].dropna().iloc[-2])
        gap_pct = ((current_price - previous_close) / previous_close) * 100 if previous_close > 0 else 0

        current_volume = int(intraday["Volume"].fillna(0).sum())
        avg_volume = int(daily["Volume"].tail(20).mean())
        rel_volume = current_volume / avg_volume if avg_volume > 0 else 0

        vwap = calculate_intraday_vwap(intraday)
        holds_vwap = bool(vwap and current_price >= vwap)

        return {
            "ticker": ticker,
            "price": round(current_price, 4),
            "previous_close": round(previous_close, 4),
            "gap_pct": round(gap_pct, 2),
            "volume": current_volume,
            "avg_volume": avg_volume,
            "rel_volume": round(rel_volume, 2),
            "float": get_news_runner_float(ticker_obj),
            "vwap": round(vwap, 4) if vwap else None,
            "holds_vwap": holds_vwap,
            "day_high": round(float(intraday["High"].max()), 4),
            "day_low": round(float(intraday["Low"].min()), 4),
            "spread_pct": estimate_recent_spread_pct(intraday),
        }

    except Exception as e:
        print(f"News runner market data error for {ticker}: {e}")
        return None


def get_finnhub_company_news(ticker):
    api_key = CONFIG.get("finnhub_api_key", "")

    if not api_key:
        return []

    today = get_market_now().strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/company-news"

    params = {
        "symbol": ticker,
        "from": today,
        "to": today,
        "token": api_key,
    }

    try:
        response = requests.get(url, params=params, timeout=15)

        if response.status_code != 200:
            return []

        rows = response.json()
        results = []

        for item in rows[:5]:
            results.append({
                "source": "Finnhub",
                "title": item.get("headline", ""),
                "summary": item.get("summary", ""),
                "url": item.get("url", ""),
                "published_at": item.get("datetime", ""),
            })

        return results
    except Exception:
        return []


def get_newsapi_company_news(ticker):
    api_key = CONFIG.get("newsapi_key", "")

    if not api_key:
        return []

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": f"{ticker} stock OR {ticker} shares",
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 5,
        "apiKey": api_key,
    }

    try:
        response = requests.get(url, params=params, timeout=15)

        if response.status_code != 200:
            return []

        data = response.json()
        articles = data.get("articles", [])

        results = []
        for item in articles[:5]:
            results.append({
                "source": "NewsAPI",
                "title": item.get("title", ""),
                "summary": item.get("description", ""),
                "url": item.get("url", ""),
                "published_at": item.get("publishedAt", ""),
            })

        return results
    except Exception:
        return []


def get_alphavantage_company_news(ticker):
    api_key = CONFIG.get("alphavantage_api_key", "")

    if not api_key:
        return []

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ticker,
        "apikey": api_key,
        "limit": 5,
    }

    try:
        response = requests.get(url, params=params, timeout=15)

        if response.status_code != 200:
            return []

        data = response.json()
        feed = data.get("feed", [])

        results = []
        for item in feed[:5]:
            results.append({
                "source": "AlphaVantage",
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "url": item.get("url", ""),
                "published_at": item.get("time_published", ""),
            })

        return results
    except Exception:
        return []


def get_yfinance_company_news(ticker):
    if not CONFIG.get("news_runner_yfinance_news_fallback", True):
        return []

    try:
        t = yf.Ticker(ticker)
        raw_news = getattr(t, "news", []) or []

        results = []
        for item in raw_news[:5]:
            title = item.get("title", "")
            if not title:
                continue

            results.append({
                "source": "Yahoo Finance",
                "title": title,
                "summary": item.get("summary", ""),
                "url": item.get("link", ""),
                "published_at": item.get("providerPublishTime", ""),
            })

        return results
    except Exception:
        return []


def get_news_runner_news(ticker):
    items = []
    items.extend(get_finnhub_company_news(ticker))
    items.extend(get_newsapi_company_news(ticker))
    items.extend(get_alphavantage_company_news(ticker))
    items.extend(get_yfinance_company_news(ticker))

    unique = []
    seen = set()

    for item in items:
        title = str(item.get("title", "")).strip()
        if not title:
            continue

        key = title.lower()
        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    return unique[:10]


def _is_news_item_fresh(item, max_age_hours):
    """
    v17.5.1: Returns True if the news item was published within the last
    `max_age_hours`. Handles multiple timestamp formats from different sources:
    - Yahoo: epoch seconds (int)
    - Finnhub: epoch seconds (int) inside "datetime" field
    - NewsAPI: ISO 8601 string
    - AlphaVantage: "YYYYMMDDTHHMMSS" format

    If we can't parse a timestamp, we default to NOT FRESH so we don't
    accidentally signal on stale news (safer to under-fire than over-fire).
    """
    if max_age_hours <= 0:
        return True  # caller opted out of freshness check

    pub = item.get("published_at") or item.get("datetime") or 0
    if not pub:
        return False

    try:
        # Yahoo / Finnhub: epoch seconds
        if isinstance(pub, (int, float)):
            pub_dt = datetime.fromtimestamp(float(pub))
        elif isinstance(pub, str):
            s = pub.strip()
            if not s:
                return False
            # NewsAPI: "2026-05-13T14:30:00Z" or with offset
            if "T" in s and ("Z" in s or "+" in s or "-" in s[10:]):
                pub_dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                # Strip tzinfo to compare naive
                if pub_dt.tzinfo is not None:
                    pub_dt = pub_dt.replace(tzinfo=None) + pub_dt.utcoffset()
            # AlphaVantage: "20260513T143000"
            elif len(s) >= 15 and "T" in s and ":" not in s:
                pub_dt = datetime.strptime(s[:15], "%Y%m%dT%H%M%S")
            else:
                try:
                    pub_dt = datetime.fromisoformat(s)
                except ValueError:
                    return False
        else:
            return False

        age_seconds = (datetime.now() - pub_dt).total_seconds()
        return age_seconds < (max_age_hours * 3600)
    except Exception:
        return False


def analyze_news_runner_news(news_items):
    # v17.5.1: filter out stale articles before declaring "has_news".
    # A stock with a 3-month-old press release in its feed should NOT trigger
    # the news-required gate. We filter for items in the last N hours.
    max_age = float(CONFIG.get("news_runner_max_news_age_hours", 24))
    fresh_items = [it for it in (news_items or []) if _is_news_item_fresh(it, max_age)]

    if not fresh_items:
        return {
            "has_news": False,
            "bullish_news": False,
            "negative_news": False,
            "headline": "",
            "source": "",
            "url": "",
            "bullish_hits": [],
            "negative_hits": [],
        }

    combined_text = " ".join(
        [str(item.get("title", "")) + " " + str(item.get("summary", "")) for item in fresh_items]
    ).lower()

    bullish_hits = [kw for kw in NEWS_RUNNER_BULLISH_KEYWORDS if kw in combined_text]
    negative_hits = [kw for kw in NEWS_RUNNER_NEGATIVE_KEYWORDS if kw in combined_text]

    best = fresh_items[0]

    return {
        "has_news": True,
        "bullish_news": len(bullish_hits) > 0,
        "negative_news": len(negative_hits) > 0,
        "headline": best.get("title", ""),
        "source": best.get("source", ""),
        "url": best.get("url", ""),
        "bullish_hits": bullish_hits[:6],
        "negative_hits": negative_hits[:6],
    }


def is_news_runner_base_candidate(stock, news):
    if not (CONFIG["news_runner_min_price"] <= stock["price"] <= CONFIG["news_runner_max_price"]):
        return False

    if stock["gap_pct"] < CONFIG["news_runner_min_gap_pct"]:
        return False

    if stock["rel_volume"] < CONFIG["news_runner_min_rel_volume"]:
        return False

    if stock["volume"] < CONFIG["news_runner_min_volume"]:
        return False

    shares_float = stock.get("float")
    if shares_float and shares_float > CONFIG["news_runner_max_float"]:
        return False

    if CONFIG["news_runner_require_fresh_news"] and not news["has_news"]:
        return False

    if CONFIG["news_runner_require_vwap_hold"] and not stock["holds_vwap"]:
        return False

    return True


def calculate_news_runner_score(stock, news):
    score = 0
    reasons = []

    if news["has_news"]:
        score += 10
        reasons.append("Fresh news detected")

    if news["bullish_news"]:
        score += 15
        reasons.append("Bullish catalyst keywords detected")

    if news["negative_news"]:
        score -= 30
        reasons.append("Negative/dilution risk keywords detected")

    if stock["rel_volume"] >= 20:
        score += 25
        reasons.append("Relative volume >= 20x")
    elif stock["rel_volume"] >= 10:
        score += 20
        reasons.append("Relative volume >= 10x")
    elif stock["rel_volume"] >= 5:
        score += 10
        reasons.append("Relative volume >= 5x")

    if stock["gap_pct"] >= 100:
        score += 25
        reasons.append("Gap >= 100%")
    elif stock["gap_pct"] >= 50:
        score += 20
        reasons.append("Gap >= 50%")
    elif stock["gap_pct"] >= 30:
        score += 15
        reasons.append("Gap >= 30%")
    elif stock["gap_pct"] >= 20:
        score += 10
        reasons.append("Gap >= 20%")

    shares_float = stock.get("float")
    if shares_float:
        if shares_float <= 5_000_000:
            score += 20
            reasons.append("Micro float <= 5M")
        elif shares_float <= 10_000_000:
            score += 15
            reasons.append("Low float <= 10M")
        elif shares_float <= 20_000_000:
            score += 10
            reasons.append("Float <= 20M")
    else:
        score -= 5
        reasons.append("Float unavailable")

    if stock["holds_vwap"]:
        score += 15
        reasons.append("Price holding above VWAP")
    else:
        score -= 10
        reasons.append("Not holding VWAP yet")

    if stock["volume"] >= 10_000_000:
        score += 15
        reasons.append("Volume >= 10M")
    elif stock["volume"] >= 5_000_000:
        score += 10
        reasons.append("Volume >= 5M")
    elif stock["volume"] >= 1_000_000:
        score += 5
        reasons.append("Volume >= 1M")

    if stock["spread_pct"] <= 2:
        score += 10
        reasons.append("Clean recent spread/chop proxy")
    elif stock["spread_pct"] <= CONFIG["news_runner_spread_warning_pct"]:
        score += 5
        reasons.append("Acceptable recent spread/chop proxy")
    else:
        score -= 10
        reasons.append("Wide spread/choppy recent candles")

    score = max(0, min(score, 100))
    return score, reasons


def classify_news_runner_score(score):
    if score >= 90:
        return "INSTITUTIONAL_GRADE_NEWS_RUNNER"
    if score >= 80:
        return "HIGH_CONVICTION_NEWS_RUNNER"
    if score >= 70:
        return "WATCHLIST_NEWS_RUNNER"
    return "LOW_QUALITY"


def format_runner_float(value):
    if not value:
        return "Unknown"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    return str(value)


def build_news_runner_message(signal):
    stock = signal["stock"]
    news = signal["news"]
    score = signal["score"]
    reasons = signal["reasons"]
    signal_type = classify_news_runner_score(score)

    entry = stock["day_high"]
    stop_loss = round(stock["price"] * (1 - CONFIG["news_runner_stop_loss_pct"] / 100), 2)
    target_1 = round(stock["price"] * (1 + CONFIG["news_runner_target_1_pct"] / 100), 2)
    target_2 = round(stock["price"] * (1 + CONFIG["news_runner_target_2_pct"] / 100), 2)

    catalysts = ", ".join(news.get("bullish_hits", [])) or "Fresh news catalyst"
    negatives = ", ".join(news.get("negative_hits", [])) or "None"

    return f"""
🧨 NEWS MOMENTUM RUNNER

Asset Type: STOCK
Signal Type: {signal_type}
Ticker: {stock["ticker"]}
Score: {score}/100
Time: {get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z")}

Price: ${stock["price"]}
Previous Close: ${stock["previous_close"]}
Gap: {stock["gap_pct"]}%
Volume: {stock["volume"]:,.0f}
Avg Volume: {stock["avg_volume"]:,.0f}
Rel Volume: {stock["rel_volume"]}x
Float: {format_runner_float(stock.get("float"))}

VWAP: {stock["vwap"]}
VWAP Hold: {"YES" if stock["holds_vwap"] else "NO"}
Day High: ${stock["day_high"]}

Entry Plan:
Break/hold above day high ${entry} or clean VWAP pullback hold.

Stop Loss:
${stop_loss} or below VWAP.

Target 1: ${target_1}
Target 2: ${target_2}

News Source: {news.get("source", "")}
Headline: {news.get("headline", "")}

Catalyst Tags:
{catalysts}

Negative Tags:
{negatives}

Reasons:
- {chr(10).join(reasons)}

Risk Note:
Low-float news runners can halt, reverse, and dilute quickly.
Use small size and hard stop.
""".strip()


def save_news_runner_to_excel(signal):
    init_excel()

    wb = load_workbook(CONFIG["excel_file"])
    ws = wb["NewsRunnerSignals"]

    stock = signal["stock"]
    news = signal["news"]
    score = signal["score"]
    reasons = signal["reasons"]

    entry = stock["day_high"]
    stop_loss = round(stock["price"] * (1 - CONFIG["news_runner_stop_loss_pct"] / 100), 2)
    target_1 = round(stock["price"] * (1 + CONFIG["news_runner_target_1_pct"] / 100), 2)
    target_2 = round(stock["price"] * (1 + CONFIG["news_runner_target_2_pct"] / 100), 2)

    ws.append([
        get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        stock["ticker"],
        "BUY",
        score,
        classify_news_runner_score(score),
        stock["price"],
        stock["previous_close"],
        stock["gap_pct"],
        stock["volume"],
        stock["avg_volume"],
        stock["rel_volume"],
        stock.get("float"),
        stock.get("vwap"),
        stock.get("holds_vwap"),
        stock["day_high"],
        stock["day_low"],
        entry,
        stop_loss,
        target_1,
        target_2,
        news.get("source"),
        news.get("headline"),
        ", ".join(news.get("bullish_hits", [])),
        ", ".join(news.get("negative_hits", [])),
        ", ".join(reasons),
        "OPEN",
        "",
        "",
        "",
    ])

    wb.save(CONFIG["excel_file"])


def scan_news_runner(ticker):
    try:
        stock = get_news_runner_market_data(ticker)
        if not stock:
            return None

        news_items = get_news_runner_news(ticker)
        news = analyze_news_runner_news(news_items)

        if not is_news_runner_base_candidate(stock, news):
            return None

        score, reasons = calculate_news_runner_score(stock, news)

        if score < CONFIG["news_runner_min_score"]:
            return None

        return {
            "ticker": ticker,
            "stock": stock,
            "news": news,
            "score": score,
            "reasons": reasons,
        }

    except Exception as e:
        print(f"News runner scan error for {ticker}: {e}")
        return None


def scan_and_send_news_runner_signals(tickers):
    if not CONFIG["enable_news_runner_scanner"]:
        return

    if not tickers:
        return

    print(f"Scanning {len(tickers)} tickers for News Momentum Runner setup...")

    signals = []

    for ticker in tickers:
        ticker = str(ticker).upper().strip()
        if not ticker:
            continue

        signal = scan_news_runner(ticker)
        if not signal:
            continue

        key = make_news_runner_key(signal)

        if key in GLOBAL_ALERTED_NEWS_RUNNERS:
            print(f"Duplicate news runner skipped: {key}")
            continue

        signals.append(signal)

    signals = sorted(signals, key=lambda x: x["score"], reverse=True)
    top_signals = signals[:CONFIG["news_runner_max_alerts_per_scan"]]

    for signal in top_signals:
        key = make_news_runner_key(signal)
        message = build_news_runner_message(signal)

        if send_investment_telegram_message(message):
            GLOBAL_ALERTED_NEWS_RUNNERS.add(key)

        save_news_runner_to_excel(signal)
        time.sleep(1)


def get_news_gist(ticker):
    try:
        t = yf.Ticker(ticker)
        news = getattr(t, "news", []) or []

        if not news:
            return "No fresh news found."

        headlines = []
        for item in news[:3]:
            title = item.get("title", "")
            if title:
                headlines.append(title)

        text = " | ".join(headlines)

        bullish_words = ["upgrade", "beats", "raises", "growth", "surge", "record", "partnership", "approval"]
        bearish_words = ["downgrade", "misses", "cuts", "falls", "lawsuit", "probe", "weak", "warning"]

        low = text.lower()

        if any(w in low for w in bullish_words):
            sentiment = "Bullish"
        elif any(w in low for w in bearish_words):
            sentiment = "Bearish"
        else:
            sentiment = "Neutral"

        return f"{sentiment}: {text[:350]}"

    except Exception as e:
        return f"News unavailable: {e}"


def scan_stock(ticker):
    try:
        df = yf.download(
            ticker,
            period="1y",
            interval="1d",
            progress=False,
            auto_adjust=True
        )

        if df.empty or len(df) < 220:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df["SMA20"] = df["Close"].rolling(20).mean()
        df["SMA50"] = df["Close"].rolling(50).mean()
        df["SMA200"] = df["Close"].rolling(200).mean()
        df["VOL20"] = df["Volume"].rolling(20).mean()
        df["VOL50"] = df["Volume"].rolling(50).mean()

        df["High20"] = df["Close"].rolling(20).max()
        df["Low20"] = df["Close"].rolling(20).min()
        df["High50"] = df["Close"].rolling(50).max()
        df["Low50"] = df["Close"].rolling(50).min()

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        prev_5 = df.iloc[-6]

        close = scalar(latest["Close"])
        prev_close = scalar(prev["Close"])
        close_5ago = scalar(prev_5["Close"])

        sma20 = scalar(latest["SMA20"])
        sma50 = scalar(latest["SMA50"])
        sma200 = scalar(latest["SMA200"])
        prev_sma50 = scalar(prev["SMA50"])
        prev_sma200 = scalar(prev["SMA200"])

        volume = scalar(latest["Volume"])
        avg_vol20 = scalar(latest["VOL20"])
        avg_vol50 = scalar(latest["VOL50"])

        high20 = scalar(latest["High20"])
        low20 = scalar(latest["Low20"])
        high50 = scalar(latest["High50"])
        low50 = scalar(latest["Low50"])

        if close < CONFIG["stock_min_price"]:
            return None

        rel_vol20 = volume / avg_vol20 if avg_vol20 > 0 else 0
        rel_vol50 = volume / avg_vol50 if avg_vol50 > 0 else 0

        range20_pct = ((high20 - low20) / close) * 100 if close > 0 else 999
        range50_pct = ((high50 - low50) / close) * 100 if close > 0 else 999

        dist_to_high20 = ((high20 - close) / close) * 100 if close > 0 else 999
        dist_to_low20 = ((close - low20) / close) * 100 if close > 0 else 999

        five_day_change_pct = ((close - close_5ago) / close_5ago) * 100 if close_5ago > 0 else 0

        # ================= BUY SETUP =================
        buy_score = 0
        buy_reasons = []
        setup = None

        if close >= high20 and rel_vol20 >= CONFIG["trap_volume_multiplier"]:
            buy_score += 5
            buy_reasons.append("Confirmed 20-day breakout with institutional volume")
            setup = "Confirmed Breakout"

        if close >= high50 and rel_vol50 >= 1.3:
            buy_score += 4
            buy_reasons.append("50-day breakout confirmation")

        if 0 <= dist_to_high20 <= 3 and close > sma20 and close > sma50:
            buy_score += 4
            buy_reasons.append("Probable breakout candidate near 20-day high")
            setup = setup or "Probable Breakout Candidate"

        if range20_pct <= 8 and range50_pct <= 18:
            buy_score += 3
            buy_reasons.append("Price squeeze / volatility contraction")

        if rel_vol20 < 0.8 and 0 <= dist_to_high20 <= 5:
            buy_score += 2
            buy_reasons.append("Volume dry-up near resistance")

        if close > sma50 and rel_vol20 >= 1.2 and five_day_change_pct > 2:
            buy_score += 3
            buy_reasons.append("Accumulation pattern: rising price with above-average volume")

        if prev_sma50 <= prev_sma200 and sma50 > sma200:
            buy_score += 5
            buy_reasons.append("Fresh golden crossover")
            setup = setup or "Golden Crossover"

        elif sma50 > sma200 and close > sma50:
            buy_score += 2
            buy_reasons.append("Golden cross structure active")

        if prev_close <= prev_sma200 and close > sma200:
            buy_score += 5
            buy_reasons.append("Strong reclaim above 200 SMA")
            setup = setup or "200 SMA Reclaim"

        elif close > sma200 and close > sma50:
            buy_score += 2
            buy_reasons.append("Holding above 200 SMA")

        if close > sma20 > sma50 and close > sma200:
            buy_score += 2
            buy_reasons.append("Trend confirmation above 20/50/200 SMA")

        # Fake breakout trap filter
        if close < high20 and prev_close >= high20 and rel_vol20 >= CONFIG["trap_volume_multiplier"]:
            buy_score -= 4
            buy_reasons.append("Fake breakout risk: failed above 20-day high")

        # No chase filter
        if setup == "Confirmed Breakout" and is_price_extended(close, high20, CONFIG["stock_no_chase_pct"]):
            buy_score -= 3
            buy_reasons.append("No-chase warning: price extended above breakout level")

        if buy_score >= 8:
            entry = round(max(close, high20), 2)
            return {
                "ticker": ticker,
                "signal": "BUY",
                "setup": setup or "Breakout / Squeeze Long",
                "score": buy_score,
                "max_score": CONFIG["stock_signal_max_score"],
                "price": round(close, 2),
                "entry": entry,
                "stop_loss": round(entry * 0.92, 2),
                "target_1": round(entry * 1.12, 2),
                "target_2": round(entry * 1.25, 2),
                "news_gist": get_news_gist(ticker),
                "reasons": buy_reasons,
            }

        # ================= SHORT SETUP =================
        short_score = 0
        short_reasons = []
        short_setup = None

        if close <= low20 and rel_vol20 >= CONFIG["trap_volume_multiplier"]:
            short_score += 5
            short_reasons.append("Confirmed 20-day breakdown with institutional volume")
            short_setup = "Confirmed Breakdown"

        if close <= low50 and rel_vol50 >= 1.3:
            short_score += 4
            short_reasons.append("50-day breakdown confirmation")

        if 0 <= dist_to_low20 <= 3 and close < sma20 and close < sma50:
            short_score += 4
            short_reasons.append("Probable breakdown candidate near 20-day low")
            short_setup = short_setup or "Probable Breakdown Candidate"

        if range20_pct <= 8 and close < sma50:
            short_score += 3
            short_reasons.append("Bearish price squeeze below 50 SMA")

        if close < sma50 and rel_vol20 >= 1.2 and five_day_change_pct < -2:
            short_score += 3
            short_reasons.append("Distribution pattern: falling price with above-average volume")

        if prev_sma50 >= prev_sma200 and sma50 < sma200:
            short_score += 5
            short_reasons.append("Fresh death crossover")
            short_setup = short_setup or "Death Crossover"

        elif sma50 < sma200 and close < sma50:
            short_score += 2
            short_reasons.append("Bearish structure below 50/200 SMA")

        if prev_close >= prev_sma200 and close < sma200:
            short_score += 5
            short_reasons.append("Breakdown below 200 SMA")
            short_setup = short_setup or "200 SMA Breakdown"

        elif close < sma200 and close < sma50:
            short_score += 2
            short_reasons.append("Holding below 200 SMA")

        # Fake breakdown trap filter
        if close > low20 and prev_close <= low20 and rel_vol20 >= CONFIG["trap_volume_multiplier"]:
            short_score -= 4
            short_reasons.append("Fake breakdown risk: reclaimed 20-day low")

        # No chase filter
        if short_setup == "Confirmed Breakdown" and is_price_extended(close, low20, CONFIG["stock_no_chase_pct"]):
            short_score -= 3
            short_reasons.append("No-chase warning: price extended below breakdown level")

        if short_score >= 8:
            entry = round(min(close, low20), 2)
            return {
                "ticker": ticker,
                "signal": "SHORT",
                "setup": short_setup or "Breakdown / Bearish Squeeze",
                "score": short_score,
                "max_score": CONFIG["stock_signal_max_score"],
                "price": round(close, 2),
                "entry": entry,
                "stop_loss": round(entry * 1.08, 2),
                "target_1": round(entry * 0.90, 2),
                "target_2": round(entry * 0.82, 2),
                "news_gist": get_news_gist(ticker),
                "reasons": short_reasons,
            }

        return None

    except Exception as e:
        print(f"Stock scan error {ticker}: {e}")
        return None


def build_stock_message(signal):
    """
    Clean Telegram stock recommendation format.

    Important:
    - Do NOT show reasons in Telegram. Reasons are still saved in Excel.
    - Do NOT show news gist or neutral news text in Telegram. News gist is still saved in Excel.
    - Show score as score/total score for easier interpretation.
    """

    score = clean_number(signal.get("score", 0))
    max_score = clean_number(signal.get("max_score", CONFIG.get("stock_signal_max_score", 33)))

    score_text = (
        f"{int(score)}/{int(max_score)}"
        if float(score).is_integer() and float(max_score).is_integer()
        else f"{round(score, 2)}/{round(max_score, 2)}"
    )

    signal_icon = "🟢" if str(signal.get("signal", "")).upper() == "BUY" else "🔴"

    return f"""
📈 STOCK SIGNAL {signal_icon}

Ticker: {signal["ticker"]}
Signal: {signal["signal"]}
Setup: {signal["setup"]}
Score: {score_text}

Price: ${signal["price"]}
Entry: ${signal["entry"]}
Stop Loss: ${signal["stop_loss"]}
Target 1: ${signal["target_1"]}
Target 2: ${signal["target_2"]}
""".strip()


# ============================================================
# PERFORMANCE DASHBOARD + AUTO LEARNING
# ============================================================

def _safe_percent(wins, losses):
    closed = wins + losses
    return round((wins / closed) * 100, 2) if closed > 0 else 0.0


def _normalize_status_for_learning(status, result):
    text = f"{status} {result}".upper()
    if "TARGET" in text or "WIN" in text or "PROFIT" in text:
        return "WIN"
    if "STOP" in text or "LOSS" in text or "FAILED" in text:
        return "LOSS"
    if "OPEN" in text or "STILL" in text or not text.strip():
        return "OPEN"
    return "OTHER"


def _bucket_dte(dte):
    dte = clean_number(dte)
    if dte <= 0:
        return "Unknown DTE"
    if dte <= 2:
        return "0-2 DTE"
    if dte <= 7:
        return "3-7 DTE"
    if dte <= 13:
        return "8-13 DTE"
    if dte <= 30:
        return "14-30 DTE"
    if dte <= 45:
        return "31-45 DTE"
    return "46+ DTE"


def _bucket_premium(premium):
    premium = clean_number(premium)
    if premium >= 1_000_000:
        return "$1M+ premium"
    if premium >= 500_000:
        return "$500K-$1M premium"
    if premium >= 100_000:
        return "$100K-$500K premium"
    if premium >= 50_000:
        return "$50K-$100K premium"
    return "<$50K premium"


def _bucket_score(score):
    score = clean_number(score)
    if score >= 20:
        return "20+ score"
    if score >= 15:
        return "15-19 score"
    if score >= 10:
        return "10-14 score"
    return "<10 score"


def _load_sheet_as_dataframe(wb, sheet_name):
    if sheet_name not in wb.sheetnames:
        return pd.DataFrame()
    ws = wb[sheet_name]
    rows = list(ws.values)
    if len(rows) <= 1:
        return pd.DataFrame()
    headers = [str(x) if x is not None else "" for x in rows[0]]
    data = rows[1:]
    return pd.DataFrame(data, columns=headers)


def _summarize_group(df, group_name, category_col, generated_at):
    rows = []
    if df.empty or category_col not in df.columns:
        return rows

    for category, part in df.groupby(category_col, dropna=False):
        statuses = [
            _normalize_status_for_learning(row.get("Status", ""), row.get("Result", ""))
            for _, row in part.iterrows()
        ]
        wins = statuses.count("WIN")
        losses = statuses.count("LOSS")
        open_count = statuses.count("OPEN")
        total = len(part)
        avg_score = round(pd.to_numeric(part.get("Score", pd.Series(dtype=float)), errors="coerce").fillna(0).mean(), 2)
        avg_premium = ""
        avg_dte = ""
        if "Premium" in part.columns:
            avg_premium = round(pd.to_numeric(part["Premium"], errors="coerce").fillna(0).mean(), 2)
        if "DTE" in part.columns:
            avg_dte = round(pd.to_numeric(part["DTE"], errors="coerce").fillna(0).mean(), 2)

        rows.append([
            generated_at,
            group_name,
            str(category),
            total,
            wins,
            losses,
            open_count,
            _safe_percent(wins, losses),
            avg_score,
            avg_premium,
            avg_dte,
            "Closed trades only are used for win rate"
        ])
    return rows


def rebuild_performance_dashboard(send_to_telegram=False):
    """
    Rebuilds two workbook sheets:
    - PerformanceDashboard: grouped win/loss/open stats
    - AutoLearning: filter recommendations based on actual outcomes

    This does not change active trading filters automatically.
    It generates recommendations so you can tune .env safely after reviewing enough data.
    """
    if not CONFIG.get("enable_performance_dashboard", True):
        return "Performance dashboard disabled."

    init_excel()
    excel_file = CONFIG["excel_file"]

    if not os.path.exists(excel_file):
        return "Performance dashboard skipped: Excel file not found."

    generated_at = get_market_now().strftime("%Y-%m-%d %H:%M:%S %Z")
    wb = load_workbook(excel_file)

    option_df = _load_sheet_as_dataframe(wb, "OptionSignals")
    stock_df = _load_sheet_as_dataframe(wb, "StockSignals")
    news_df = _load_sheet_as_dataframe(wb, "NewsRunnerSignals")

    dashboard_rows = []

    if not option_df.empty:
        option_df["DTE Bucket"] = option_df.get("DTE", pd.Series(dtype=float)).apply(_bucket_dte)
        option_df["Premium Bucket"] = option_df.get("Premium", pd.Series(dtype=float)).apply(_bucket_premium)
        option_df["Score Bucket"] = option_df.get("Score", pd.Series(dtype=float)).apply(_bucket_score)
        option_df["Option Direction"] = option_df.get("Option Type", "").astype(str).str.upper()

        # v17.2: split watchlist out of headline groupings. Watchlist signals
        # are kept and shown as separate "Watchlist Reference" rows at the
        # bottom of the dashboard so they don't pollute the per-bucket win
        # rates of TRADE/HIGH_CONVICTION signals.
        decision_col = option_df.get("Decision", pd.Series(dtype=str)).astype(str).str.upper().str.strip()
        is_watchlist_mask = decision_col == "WATCHLIST"
        option_headline = option_df[~is_watchlist_mask]
        option_watchlist = option_df[is_watchlist_mask]

        # Headline groupings (TRADE + HIGH_CONVICTION only).
        if not option_headline.empty:
            dashboard_rows += _summarize_group(option_headline, "Options", "Decision", generated_at)
            dashboard_rows += _summarize_group(option_headline, "Options", "Option Direction", generated_at)
            dashboard_rows += _summarize_group(option_headline, "Options", "DTE Bucket", generated_at)
            dashboard_rows += _summarize_group(option_headline, "Options", "Premium Bucket", generated_at)
            dashboard_rows += _summarize_group(option_headline, "Options", "Score Bucket", generated_at)

        # Separate reference section for watchlist — rolled up so it doesn't
        # bury the headline rows but is still inspectable.
        if not option_watchlist.empty:
            dashboard_rows += _summarize_group(
                option_watchlist, "Options (Watchlist Reference)", "Decision", generated_at
            )

    if not stock_df.empty:
        stock_df["Score Bucket"] = stock_df.get("Score", pd.Series(dtype=float)).apply(_bucket_score)
        dashboard_rows += _summarize_group(stock_df, "Stocks", "Signal", generated_at)
        dashboard_rows += _summarize_group(stock_df, "Stocks", "Setup", generated_at)
        dashboard_rows += _summarize_group(stock_df, "Stocks", "Score Bucket", generated_at)

    if not news_df.empty:
        news_df["Score Bucket"] = news_df.get("Score", pd.Series(dtype=float)).apply(_bucket_score)
        dashboard_rows += _summarize_group(news_df, "News Runners", "Signal", generated_at)
        dashboard_rows += _summarize_group(news_df, "News Runners", "Setup", generated_at)
        dashboard_rows += _summarize_group(news_df, "News Runners", "Score Bucket", generated_at)

    if "PerformanceDashboard" in wb.sheetnames:
        del wb["PerformanceDashboard"]
    ws = wb.create_sheet("PerformanceDashboard")
    ws.append([
        "Generated At", "Signal Group", "Category", "Total", "Wins", "Losses",
        "Open", "Win Rate %", "Avg Score", "Avg Premium", "Avg DTE", "Notes"
    ])
    for row in dashboard_rows:
        ws.append(row)

    learning_rows = build_auto_learning_recommendations(dashboard_rows, generated_at)

    if "AutoLearning" in wb.sheetnames:
        del wb["AutoLearning"]
    ws2 = wb.create_sheet("AutoLearning")
    ws2.append([
        "Generated At", "Area", "Finding", "Current Result", "Suggested Action",
        "Reason", "Priority"
    ])
    for row in learning_rows:
        ws2.append(row)

    wb.save(excel_file)

    summary = build_performance_summary_message(dashboard_rows, learning_rows)
    if send_to_telegram and CONFIG.get("send_performance_report_to_telegram", True):
        send_investment_telegram_message(summary)

    return summary


def build_auto_learning_recommendations(dashboard_rows, generated_at):
    if not CONFIG.get("enable_auto_learning", True):
        return [[generated_at, "Auto Learning", "Disabled", "N/A", "No action", "ENABLE_AUTO_LEARNING=false", "LOW"]]

    min_trades = CONFIG.get("auto_learning_min_trades", 5)
    good_wr = CONFIG.get("auto_learning_good_win_rate", 60)
    bad_wr = CONFIG.get("auto_learning_bad_win_rate", 40)
    rows = []

    for item in dashboard_rows:
        _, group_name, category, total, wins, losses, open_count, win_rate, avg_score, avg_premium, avg_dte, _notes = item
        closed = wins + losses
        if closed < min_trades:
            continue

        label = f"{group_name}: {category}"
        current = f"Closed={closed}, Wins={wins}, Losses={losses}, Win Rate={win_rate}%"

        if win_rate >= good_wr:
            rows.append([
                generated_at,
                group_name,
                f"Strong performer: {category}",
                current,
                "Keep or prioritize this bucket. Consider allowing slightly more signals only if risk remains controlled.",
                f"Win rate is above {good_wr}% with at least {min_trades} closed trades.",
                "MEDIUM"
            ])

        elif win_rate <= bad_wr:
            action = "Tighten or avoid this bucket until performance improves."
            if "0-2 DTE" in str(category) or "3-7 DTE" in str(category):
                action = "Consider increasing MIN_DTE or requiring stronger price reaction for this DTE bucket."
            elif "<$50K" in str(category) or "$50K-$100K" in str(category):
                action = "Consider increasing MIN_PREMIUM or requiring flow confirmation for this premium bucket."
            elif "<10 score" in str(category) or "10-14 score" in str(category):
                action = "Consider increasing OPTION_TRADE_SCORE or sending this bucket to watchlist only."

            rows.append([
                generated_at,
                group_name,
                f"Weak performer: {category}",
                current,
                action,
                f"Win rate is below {bad_wr}% with at least {min_trades} closed trades.",
                "HIGH"
            ])

    if not rows:
        rows.append([
            generated_at,
            "Auto Learning",
            "Not enough closed trades yet",
            f"Minimum required closed trades per bucket: {min_trades}",
            "Keep collecting results. Do not over-tune before enough samples.",
            "Auto-learning needs actual TARGET/STOP results to make useful recommendations.",
            "LOW"
        ])

    return rows


def build_performance_summary_message(dashboard_rows, learning_rows):
    # v17.2: watchlist is now segregated into "Options (Watchlist Reference)"
    # group and excluded from Telegram summary headline metrics.
    option_rows = [r for r in dashboard_rows if r[1] == "Options" and r[2] in ["TRADE", "HIGH_CONVICTION"]]
    watchlist_ref_rows = [r for r in dashboard_rows if r[1] == "Options (Watchlist Reference)"]
    stock_rows = [r for r in dashboard_rows if r[1] == "Stocks"]
    news_rows = [r for r in dashboard_rows if r[1] == "News Runners"]

    def best_and_worst(rows):
        usable = [r for r in rows if (r[4] + r[5]) > 0]
        if not usable:
            return None, None
        best = sorted(usable, key=lambda x: x[7], reverse=True)[0]
        worst = sorted(usable, key=lambda x: x[7])[0]
        return best, worst

    best_opt, worst_opt = best_and_worst([r for r in dashboard_rows if r[1] == "Options"])

    high_priority = [r for r in learning_rows if str(r[-1]).upper() == "HIGH"]
    medium_priority = [r for r in learning_rows if str(r[-1]).upper() == "MEDIUM"]

    lines = ["📊 PERFORMANCE DASHBOARD", ""]
    lines.append(f"Options categories analyzed (TRADE+HC): {len([r for r in dashboard_rows if r[1] == 'Options'])}")
    if watchlist_ref_rows:
        wl_total = sum((r[4] + r[5]) for r in watchlist_ref_rows)
        lines.append(f"Watchlist signals (reference, excluded): {wl_total} closed")
    lines.append(f"Stock categories analyzed: {len(stock_rows)}")
    lines.append(f"News runner categories analyzed: {len(news_rows)}")

    if best_opt:
        lines.append("")
        lines.append(f"Best option bucket: {best_opt[2]} | WR {best_opt[7]}% | Closed {best_opt[4] + best_opt[5]}")
    if worst_opt:
        lines.append(f"Weakest option bucket: {worst_opt[2]} | WR {worst_opt[7]}% | Closed {worst_opt[4] + worst_opt[5]}")

    lines.append("")
    lines.append(f"High-priority learning items: {len(high_priority)}")
    lines.append(f"Positive performers found: {len(medium_priority)}")

    top_items = high_priority[:3] if high_priority else medium_priority[:3]
    if top_items:
        lines.append("")
        lines.append("Top learning notes:")
        for row in top_items:
            lines.append(f"- {row[2]} → {row[4]}")

    lines.append("")
    lines.append("Full dashboard saved in signals.xlsx → PerformanceDashboard + AutoLearning sheets.")
    return "\n".join(lines)


def maybe_send_daily_performance_report(last_daily_report_date):
    now = get_market_now()
    if now.time() < make_time("daily_report_hour", "daily_report_minute"):
        return last_daily_report_date

    today = now.strftime("%Y-%m-%d")
    if last_daily_report_date == today:
        return last_daily_report_date

    try:
        # v17.5: split reports per channel.
        # Options summary goes to the option channel; stocks/news to investment channel.
        if CONFIG.get("enable_eod_channel_reports", True):
            send_eod_option_channel_report()
            send_eod_stock_channel_report()
            # Weekly stock report only on Fridays.
            if now.weekday() == 4:  # 4 = Friday
                send_weekly_stock_signal_report()
        # Keep legacy combined dashboard for Excel even if Telegram is split.
        rebuild_performance_dashboard(send_to_telegram=False)
        return today
    except Exception as e:
        print("Performance report error:", e)
        return last_daily_report_date


def _today_str():
    return get_market_now().strftime("%Y-%m-%d")


def _signals_from_sheet(sheet_name, date_filter=None):
    """Read a signals sheet and optionally filter by DateTime starting with date_filter."""
    excel_file = CONFIG["excel_file"]
    if not os.path.exists(excel_file):
        return []
    try:
        wb = load_workbook(excel_file, read_only=True)
        if sheet_name not in wb.sheetnames:
            return []
        ws = wb[sheet_name]
        rows = list(ws.values)
        if not rows or len(rows) < 2:
            return []
        headers = [str(h) if h is not None else "" for h in rows[0]]
        data = [dict(zip(headers, r)) for r in rows[1:]]
        if date_filter:
            data = [r for r in data if str(r.get("DateTime", "")).startswith(date_filter)]
        return data
    except Exception as e:
        print(f"[eod] _signals_from_sheet error for {sheet_name}: {e}")
        return []


def send_eod_option_channel_report():
    """
    v17.5: End-of-day report sent to the OPTION channel. Summarizes today's
    option signals, decision mix, and recent rolling win rates.
    """
    today = _today_str()
    today_signals = _signals_from_sheet("OptionSignals", date_filter=today)
    if not today_signals:
        return  # nothing to report

    # Today's breakdown
    decision_counts = Counter(str(r.get("Decision", "")).strip() for r in today_signals)
    today_resolved = [r for r in today_signals
                      if "TARGET" in str(r.get("Status", "")).upper()
                      or "STOP" in str(r.get("Status", "")).upper()]
    today_wins = [r for r in today_resolved if "TARGET" in str(r.get("Status", "")).upper()]

    # Rolling 7-day window for TRADE+HC only (exclude watchlist from headline)
    cutoff_7 = (get_market_now().date() - timedelta(days=7)).isoformat()
    cutoff_30 = (get_market_now().date() - timedelta(days=30)).isoformat()
    all_signals = _signals_from_sheet("OptionSignals")

    def _rolling_wr(signals, since_date_iso):
        s = [r for r in signals
             if str(r.get("DateTime", "")).split(" ")[0] >= since_date_iso
             and str(r.get("Decision", "")).strip().upper() in ("TRADE", "HIGH_CONVICTION")]
        resolved = [r for r in s
                    if "TARGET" in str(r.get("Status", "")).upper()
                    or "STOP" in str(r.get("Status", "")).upper()]
        wins = [r for r in resolved if "TARGET" in str(r.get("Status", "")).upper()]
        wr = (len(wins) / len(resolved) * 100) if resolved else None
        return len(s), len(resolved), len(wins), wr

    s7, r7, w7, wr7 = _rolling_wr(all_signals, cutoff_7)
    s30, r30, w30, wr30 = _rolling_wr(all_signals, cutoff_30)

    lines = ["📊 Options — End of Day Report", today, ""]
    lines.append(f"Today's signals: {len(today_signals)}")
    for d in ["HIGH_CONVICTION", "TRADE", "WATCHLIST"]:
        if decision_counts.get(d, 0) > 0:
            lines.append(f"  {d}: {decision_counts[d]}")
    if today_resolved:
        lines.append("")
        lines.append(f"Resolved today: {len(today_resolved)} ({len(today_wins)} wins, "
                     f"{len(today_resolved) - len(today_wins)} losses)")

    lines.append("")
    lines.append("Rolling win rate (TRADE + HC only):")
    if wr7 is not None:
        lines.append(f"  7-day: {wr7:.0f}% ({w7}/{r7} resolved of {s7} total)")
    else:
        lines.append(f"  7-day: insufficient resolved trades ({r7} resolved)")
    if wr30 is not None:
        lines.append(f"  30-day: {wr30:.0f}% ({w30}/{r30} resolved of {s30} total)")
    else:
        lines.append(f"  30-day: insufficient resolved trades ({r30} resolved)")

    lines.append("")
    lines.append("(Win-rate samples below 20 are statistical noise — interpret with care.)")

    send_option_telegram_message("\n".join(lines))


def send_eod_stock_channel_report():
    """
    v17.5: End-of-day report sent to the INVESTMENT channel. Summarizes
    today's stock signals and rolling win rates.
    """
    today = _today_str()
    today_signals = _signals_from_sheet("StockSignals", date_filter=today)
    today_news = _signals_from_sheet("NewsRunnerSignals", date_filter=today)

    if not today_signals and not today_news:
        return

    lines = ["📊 Stocks — End of Day Report", today, ""]

    if today_signals:
        signal_counts = Counter(str(r.get("Signal", "")).strip() for r in today_signals)
        lines.append(f"Today's stock signals: {len(today_signals)}")
        for sig in ["BUY", "SHORT"]:
            if signal_counts.get(sig, 0) > 0:
                lines.append(f"  {sig}: {signal_counts[sig]}")
    else:
        lines.append("No stock signals today.")

    if today_news:
        lines.append("")
        lines.append(f"News runner signals: {len(today_news)}")

    # Rolling stock win rates
    cutoff_7 = (get_market_now().date() - timedelta(days=7)).isoformat()
    cutoff_30 = (get_market_now().date() - timedelta(days=30)).isoformat()
    all_stocks = _signals_from_sheet("StockSignals")

    def _stock_rolling_wr(signals, since_date_iso):
        s = [r for r in signals if str(r.get("DateTime", "")).split(" ")[0] >= since_date_iso]
        resolved = [r for r in s if str(r.get("Result", "")).upper() in ("WIN", "LOSS")]
        wins = [r for r in resolved if str(r.get("Result", "")).upper() == "WIN"]
        wr = (len(wins) / len(resolved) * 100) if resolved else None
        return len(s), len(resolved), len(wins), wr

    s7, r7, w7, wr7 = _stock_rolling_wr(all_stocks, cutoff_7)
    s30, r30, w30, wr30 = _stock_rolling_wr(all_stocks, cutoff_30)

    lines.append("")
    lines.append("Rolling win rate:")
    if wr7 is not None:
        lines.append(f"  7-day: {wr7:.0f}% ({w7}/{r7} resolved of {s7} signaled)")
    else:
        lines.append(f"  7-day: insufficient resolved ({r7} resolved)")
    if wr30 is not None:
        lines.append(f"  30-day: {wr30:.0f}% ({w30}/{r30} resolved of {s30} signaled)")
    else:
        lines.append(f"  30-day: insufficient resolved ({r30} resolved)")

    send_investment_telegram_message("\n".join(lines))


def send_weekly_stock_signal_report():
    """
    v17.5: Weekly summary of stock signals for the past 7 days. Sent Fridays
    after market close. Shows resolved % up/down, best/worst, open positions.
    """
    cutoff = (get_market_now().date() - timedelta(days=7)).isoformat()
    all_stocks = _signals_from_sheet("StockSignals")
    week_signals = [r for r in all_stocks
                    if str(r.get("DateTime", "")).split(" ")[0] >= cutoff]

    if not week_signals:
        return

    # Performance breakdown — needs Entry, Exit Price
    resolved = [r for r in week_signals if str(r.get("Result", "")).upper() in ("WIN", "LOSS")]
    wins = [r for r in resolved if str(r.get("Result", "")).upper() == "WIN"]
    losses = [r for r in resolved if str(r.get("Result", "")).upper() == "LOSS"]
    open_signals = [r for r in week_signals if str(r.get("Status", "")).upper() == "OPEN"]

    # Compute % moves for each resolved signal
    def _pct_move(r):
        try:
            entry = float(r.get("Entry", 0) or 0)
            exit_price = float(r.get("Exit Price", 0) or 0)
            if entry <= 0 or exit_price <= 0:
                return None
            move = (exit_price - entry) / entry * 100
            if str(r.get("Signal", "")).upper().strip() == "SHORT":
                move = -move
            return move
        except Exception:
            return None

    win_moves = [_pct_move(r) for r in wins]
    win_moves = [m for m in win_moves if m is not None]
    loss_moves = [_pct_move(r) for r in losses]
    loss_moves = [m for m in loss_moves if m is not None]

    avg_win = (sum(win_moves) / len(win_moves)) if win_moves else None
    avg_loss = (sum(loss_moves) / len(loss_moves)) if loss_moves else None

    # Best / worst
    all_moves = []
    for r in resolved:
        m = _pct_move(r)
        if m is not None:
            all_moves.append((m, r))
    best = max(all_moves, key=lambda x: x[0]) if all_moves else None
    worst = min(all_moves, key=lambda x: x[0]) if all_moves else None

    end_date = get_market_now().strftime("%Y-%m-%d")
    lines = [
        "📈 Weekly Stock Signal Report",
        f"Week ending {end_date}",
        "",
        f"Total signals issued: {len(week_signals)}",
        f"  Resolved: {len(resolved)} ({len(wins)} wins, {len(losses)} losses)",
        f"  Still open: {len(open_signals)}",
    ]
    if resolved:
        wr = len(wins) / len(resolved) * 100
        lines.append(f"  Win rate (resolved): {wr:.0f}%")

    if avg_win is not None or avg_loss is not None:
        lines.append("")
        lines.append("Average move:")
        if avg_win is not None:
            lines.append(f"  Winners: +{avg_win:.1f}%")
        if avg_loss is not None:
            lines.append(f"  Losers:  {avg_loss:.1f}%")

    if best:
        lines.append("")
        lines.append(f"Best:  {best[1].get('Ticker', '?')} ({best[0]:+.1f}%)")
    if worst:
        lines.append(f"Worst: {worst[1].get('Ticker', '?')} ({worst[0]:+.1f}%)")

    if len(resolved) < 10:
        lines.append("")
        lines.append("(Small sample — interpret weekly numbers as anecdote, not edge.)")

    send_investment_telegram_message("\n".join(lines))


def send_daily_report():
    try:
        summary = rebuild_performance_dashboard(send_to_telegram=False)
        if CONFIG.get("send_performance_report_to_telegram", True):
            send_investment_telegram_message(summary)
    except Exception as e:
        print("Daily report error:", e)

def scan_premarket_squeeze(ticker):
    try:
        df = yf.download(
            ticker,
            period="5d",
            interval="1m",
            prepost=True,
            progress=False,
            auto_adjust=True
        )

        if df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        latest_price = scalar(df["Close"].dropna().iloc[-1])
        if latest_price < CONFIG["premarket_squeeze_min_price"]:
            return None

        today = get_market_now().date()
        df = df.copy()
        df["dt"] = df.index

        premarket_df = df[
            (df["dt"].dt.date == today)
            & (df["dt"].dt.time >= make_time("premarket_start_hour", "premarket_start_minute"))
            & (df["dt"].dt.time < make_time("premarket_end_hour", "premarket_end_minute"))
        ]

        if premarket_df.empty:
            return None

        premarket_volume = int(premarket_df["Volume"].fillna(0).sum())
        if premarket_volume < CONFIG["premarket_squeeze_min_volume"]:
            return None

        daily = yf.download(
            ticker,
            period="10d",
            interval="1d",
            progress=False,
            auto_adjust=True
        )

        if daily.empty or len(daily) < 2:
            return None

        if isinstance(daily.columns, pd.MultiIndex):
            daily.columns = daily.columns.get_level_values(0)

        prev_close = scalar(daily["Close"].dropna().iloc[-2])
        gap_pct = ((latest_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0

        if gap_pct < CONFIG["premarket_squeeze_min_gap_pct"]:
            return None

        day_high = scalar(premarket_df["High"].max())
        day_low = scalar(premarket_df["Low"].min())

        range_pct = ((day_high - day_low) / latest_price) * 100 if latest_price > 0 else 999

        score = 0
        reasons = []

        if gap_pct >= 20:
            score += 30
            reasons.append("Gap >= 20%")
        elif gap_pct >= 10:
            score += 20
            reasons.append("Gap >= 10%")
        else:
            score += 10
            reasons.append("Gap >= 5%")

        if premarket_volume >= 5_000_000:
            score += 30
            reasons.append("Premarket volume >= 5M")
        elif premarket_volume >= 1_000_000:
            score += 20
            reasons.append("Premarket volume >= 1M")
        else:
            score += 10
            reasons.append("Premarket volume >= 300K")

        if range_pct <= 8:
            score += 20
            reasons.append("Tight squeeze range")
        elif range_pct <= 15:
            score += 10
            reasons.append("Acceptable squeeze range")

        if latest_price >= day_high * 0.97:
            score += 20
            reasons.append("Holding near premarket high")

        return {
            "ticker": ticker,
            "price": round(latest_price, 2),
            "gap_pct": round(gap_pct, 2),
            "premarket_volume": premarket_volume,
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "range_pct": round(range_pct, 2),
            "score": score,
            "reasons": reasons,
        }

    except Exception as e:
        print(f"Premarket squeeze error {ticker}: {e}")
        return None


def build_premarket_squeeze_message(signals):
    lines = ["🔥 TOP PREMARKET SQUEEZE WATCHLIST", ""]

    for i, s in enumerate(signals, start=1):
        lines.append(
            f"{i}. {s['ticker']} | Price ${s['price']} | Gap {s['gap_pct']}% | "
            f"Vol {s['premarket_volume']:,.0f} | High ${s['day_high']} | Score {s['score']}"
        )

    lines.append("")
    lines.append("Rule: wait for premarket high break or VWAP hold after open.")

    return "\n".join(lines)


def scan_and_send_premarket_squeeze_alert(tickers):
    if not CONFIG.get("enable_premarket_squeeze_alert", True):
        return

    signals = []

    for ticker in tickers:
        signal = scan_premarket_squeeze(ticker)
        if signal:
            signals.append(signal)

    signals = sorted(signals, key=lambda x: x["score"], reverse=True)
    top = signals[:CONFIG["premarket_squeeze_top_n"]]

    if top:
        send_investment_telegram_message(build_premarket_squeeze_message(top))
        

def cleanup_weekly_data(last_cleanup_date=None):
    """
    Weekly maintenance for the bot folders.

    Fixes the prior runtime error:
        name 'cleanup_weekly_data' is not defined

    Behavior:
    - Runs only when CONFIG['weekly_cleanup_enabled'] is True.
    - Runs once per configured cleanup day.
    - Moves leftover CSV files from download_folder to archive_folder.
    - Moves leftover PNG files to image_folder.
    - Deletes old archived files older than CONFIG['archive_retention_days'].
    - Returns the last cleanup date so the same day is not cleaned repeatedly.
    """
    try:
        if not CONFIG.get("weekly_cleanup_enabled", True):
            return last_cleanup_date

        now = get_market_now()
        today = now.date()

        cleanup_day = int(CONFIG.get("cleanup_day", 5))
        cleanup_time = dt_time(
            int(CONFIG.get("cleanup_hour", 18)),
            int(CONFIG.get("cleanup_minute", 0))
        )

        if now.weekday() != cleanup_day:
            return last_cleanup_date

        if now.time() < cleanup_time:
            return last_cleanup_date

        if last_cleanup_date == today:
            return last_cleanup_date

        print("Weekly cleanup started.")

        # Move any screenshots first.
        move_png_files()

        # Archive any CSV files left in the download folder.
        download_folder = CONFIG.get("download_folder")
        if download_folder and os.path.exists(download_folder):
            for csv_file in glob.glob(os.path.join(download_folder, "*.csv")):
                try:
                    archive_csv(csv_file)
                except Exception as e:
                    print(f"Cleanup archive error for {csv_file}: {e}")

        # Delete old files from archive folder based on retention.
        archive_folder = CONFIG.get("archive_folder")
        retention_days = int(CONFIG.get("archive_retention_days", 7))
        cutoff_ts = time.time() - (retention_days * 24 * 60 * 60)

        if archive_folder and os.path.exists(archive_folder):
            for root, dirs, files in os.walk(archive_folder, topdown=False):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    try:
                        if os.path.getmtime(file_path) < cutoff_ts:
                            os.remove(file_path)
                            print("Deleted old archive file:", file_path)
                    except Exception as e:
                        print(f"Cleanup delete error for {file_path}: {e}")

                # Remove empty archive subfolders.
                for dirname in dirs:
                    dir_path = os.path.join(root, dirname)
                    try:
                        if not os.listdir(dir_path):
                            os.rmdir(dir_path)
                    except Exception:
                        pass

        print("Weekly cleanup completed.")
        return today

    except Exception as e:
        print("Weekly cleanup error:", e)
        return last_cleanup_date

def main():
    init_excel()

    driver = LazyDriver()

    print(f"Bot started. SCRIPT_VERSION = {SCRIPT_VERSION}")
    print("Using persistent Chrome profile:")
    print(CONFIG["chrome_profile_folder"])

    # v17: print Schwab integration status at startup so we can see at a
    # glance whether real-time data is wired up correctly.
    try:
        from schwab_data import schwab_health_check
        status = schwab_health_check()
        print("[schwab] Integration status:")
        for k, v in status.items():
            print(f"  {k}: {v}")
        if status["data_source"] in ("schwab", "auto") and not status["token_file_exists"]:
            print("[schwab] WARNING: data_source includes Schwab but no token file found.")
            print("[schwab] Run once: python barchart_pro_bot.py --setup-schwab")
            print("[schwab] Bot will run on Yahoo data until token is set up (auto mode).")
    except ImportError:
        print("[schwab] schwab_data module not found. Bot will use Yahoo only.")
    except Exception as e:
        print(f"[schwab] Status check error: {e}")

    # v17.3: register a Telegram alert callback for Schwab token health.
    # The schwab_data module will call this when token expiry warnings or
    # auth-failure thresholds are crossed.
    try:
        from schwab_data import register_alert_callback, get_token_expiry_info
        def _schwab_alert_to_telegram(subject, body):
            try:
                send_option_telegram_message(body)
            except Exception as e:
                print(f"[schwab-monitor] Failed to send Telegram alert: {e}")
        register_alert_callback(_schwab_alert_to_telegram)
        print("[schwab] Token-health monitoring registered (Telegram alerts enabled).")

        # Print initial expiry info so we know the baseline
        expiry_info = get_token_expiry_info()
        if expiry_info.get("refresh_expires_in_seconds") is not None:
            hours_left = expiry_info["refresh_expires_in_seconds"] / 3600
            print(f"[schwab] Refresh token expires in {hours_left:.1f} hours "
                  f"({expiry_info.get('refresh_expires_at_iso')})")
    except ImportError:
        pass
    except Exception as e:
        print(f"[schwab-monitor] Setup error: {e}")

    driver.get(CONFIG["barchart_unusual_url"])
    time.sleep(CONFIG["initial_login_wait_seconds"])
    handle_login_popup(driver)

    last_cleanup_date = None
    last_daily_report_date = None
    last_eod_labeler_date = None  # v16: tracks when the EOD labeler last ran

    while True:
        last_cleanup_date = cleanup_weekly_data(last_cleanup_date)
        move_png_files()

        # v17.3: check Schwab token health once per cycle. Fires Telegram
        # alerts at 24h before expiry, 4h before expiry, and at expiry.
        # Each alert sends at most once per token lifecycle.
        try:
            from schwab_data import check_token_expiry_and_alert
            check_token_expiry_and_alert()
        except Exception as e:
            print(f"[schwab-monitor] Cycle check error: {e}")

        # v17.4.1: reset per-cycle Schwab call counters so progress logging
        # starts fresh each scan cycle.
        try:
            from schwab_data import reset_circuit_breaker_cycle_counters
            reset_circuit_breaker_cycle_counters()
        except Exception:
            pass

        now = get_market_now()

        if not is_market_day():
            print(get_market_status_message())
            if os.getenv("EXIT_ON_NON_MARKET_DAY", "true" if IS_LINUX else "false").lower() == "true":
                print("Market closed today (weekend/NYSE holiday). Exiting; the timer starts the bot next trading day.")
                driver.release()
                break
            _idle(driver, CONFIG["market_closed_sleep_seconds"])
            continue

        if is_premarket():
            print("Pre-market squeeze scan active.")

            stock_movers_csv = download_stock_movers_csv(driver)

            dynamic_tickers = get_dynamic_stock_tickers(
                stock_movers_csv,
                None,
                pd.DataFrame()
            )

            scan_and_send_premarket_squeeze_alert(dynamic_tickers)

            archive_csv(stock_movers_csv)
            move_png_files()

            print(f"Sleeping {CONFIG['check_interval_seconds']} seconds.")
            _idle(driver, CONFIG["check_interval_seconds"])
            continue

        if is_after_hours():
            print("After-hours mode active.")

            stock_movers_csv = download_stock_movers_csv(driver)

            dynamic_tickers = get_dynamic_stock_tickers(
                stock_movers_csv,
                None,
                pd.DataFrame()
            )

            scan_and_send_dynamic_stock_signals(dynamic_tickers)

            if CONFIG.get("enable_news_runner_scanner", True):
                scan_and_send_news_runner_signals(dynamic_tickers)

            # v16: run EOD outcome labeler once per market day after close.
            today_date = get_market_now().date()
            if last_eod_labeler_date != today_date:
                try:
                    eod_result = run_eod_outcome_labeler()
                    print(f"EOD labeler result: {eod_result}")
                    last_eod_labeler_date = today_date
                except Exception as e:
                    print(f"EOD labeler exception: {e}")

            archive_csv(stock_movers_csv)
            move_png_files()

            print(f"Sleeping {CONFIG['check_interval_seconds']} seconds.")
            _idle(driver, CONFIG["check_interval_seconds"])
            continue

        if now.time() > make_time("after_hours_end_hour", "after_hours_end_minute"):
            print("After-hours ended. Bot stopped.")
            send_investment_telegram_message("📴 After-hours ended. Bot stopped.")
            # Close Chrome browser before exiting
            try:
                driver.quit()
                print("Chrome browser closed.")
            except Exception as _e:
                print(f"Chrome close error (ignored): {_e}")
            break

        if not is_regular_market_hours():
            print(get_market_status_message())
            _idle(driver, CONFIG["market_closed_sleep_seconds"])
            continue

        if not is_scan_window_open():
            print(get_market_status_message())
            _idle(driver, CONFIG["outside_scan_window_sleep_seconds"])
            continue

        if not is_new_signal_allowed():
            print(get_market_status_message())
            _idle(driver, CONFIG["outside_scan_window_sleep_seconds"])
            continue

        print(get_market_status_message())

        flow_csv = download_option_flow_csv(driver)
        flow_df = pd.DataFrame()

        if flow_csv:
            try:
                flow_df = read_barchart_csv(flow_csv)
            except Exception as e:
                print("Could not read option flow CSV:", e)

        unusual_csv = download_unusual_options_csv(driver)

        if unusual_csv:
            process_unusual_csv(driver, unusual_csv, flow_df)

        if CONFIG["enable_trade_monitoring"]:
            monitor_open_option_signals()
            
        stock_movers_csv = download_stock_movers_csv(driver)

        dynamic_tickers = get_dynamic_stock_tickers(
            stock_movers_csv,
            unusual_csv,
            flow_df
        )

        print("Dynamic stock tickers:", dynamic_tickers)

        scan_and_send_dynamic_stock_signals(dynamic_tickers)
        if "monitor_open_signals" in globals():
            monitor_open_signals()

        if CONFIG["news_runner_scan_dynamic_tickers"]:
            scan_and_send_news_runner_signals(dynamic_tickers)

        for csv_file in [flow_csv, unusual_csv, stock_movers_csv]:
            archive_csv(csv_file)

        move_png_files()

        last_daily_report_date = maybe_send_daily_performance_report(last_daily_report_date)

        print(f"Sleeping {CONFIG['check_interval_seconds']} seconds.")
        _idle(driver, CONFIG["check_interval_seconds"])

    # v17.6: return driver so the __main__ finally block can quit Chrome
    # on any exit path not already handled above (crash, KeyboardInterrupt)
    return driver


if __name__ == "__main__":
    import sys

    # v16: simple CLI dispatch so backtest and EOD labeler can run standalone
    # without launching the Selenium scraper. Examples:
    #   python barchart_pro_bot.py --backtest 60
    #   python barchart_pro_bot.py --label-eod
    # v17: added --setup-schwab and --schwab-status.
    if len(sys.argv) >= 2:
        cmd = sys.argv[1].lower()
        if cmd == "--backtest":
            # v17.2: parse optional days arg (int) and --with-watchlist flag
            # in any order. Examples:
            #   --backtest 60
            #   --backtest 60 --with-watchlist
            #   --backtest --with-watchlist 60
            days = 90
            include_watchlist = False
            for arg in sys.argv[2:]:
                arg_lower = arg.lower()
                if arg_lower == "--with-watchlist":
                    include_watchlist = True
                else:
                    try:
                        days = int(arg)
                    except ValueError:
                        pass
            backtest_signals_history(lookback_days=days, include_watchlist=include_watchlist)
            sys.exit(0)
        elif cmd == "--label-eod":
            result = run_eod_outcome_labeler(force=True)
            print(result)
            sys.exit(0)
        elif cmd == "--setup-schwab":
            try:
                from schwab_data import setup_schwab_oauth
                ok = setup_schwab_oauth()
                sys.exit(0 if ok else 1)
            except ImportError as e:
                print(f"ERROR: schwab_data module not found. Make sure schwab_data.py "
                      f"is in the same folder as this script. Detail: {e}")
                sys.exit(1)
        elif cmd == "--refresh-schwab":
            # v17.3: smart refresh — only opens browser if refresh token is
            # actually expired. Otherwise just confirms state.
            try:
                from schwab_data import refresh_schwab_smart
                ok = refresh_schwab_smart()
                sys.exit(0 if ok else 1)
            except ImportError as e:
                print(f"ERROR: schwab_data module not found: {e}")
                sys.exit(1)
        elif cmd == "--schwab-status":
            try:
                from schwab_data import schwab_health_check, get_token_expiry_info
                status = schwab_health_check()
                print("Schwab integration status:")
                for k, v in status.items():
                    print(f"  {k}: {v}")
                expiry = get_token_expiry_info()
                print("\nToken expiry:")
                for k, v in expiry.items():
                    print(f"  {k}: {v}")
                sys.exit(0)
            except ImportError as e:
                print(f"ERROR: schwab_data module not found: {e}")
                sys.exit(1)
        elif cmd == "--schwab-monitor":
            # v17.3: shows the current monitoring state (failure counters,
            # alert callback registered, expiry info).
            try:
                from schwab_data import get_monitoring_status
                status = get_monitoring_status()
                print("Schwab token-health monitoring:")
                import json as _json
                print(_json.dumps(status, indent=2, default=str))
                sys.exit(0)
            except ImportError as e:
                print(f"ERROR: schwab_data module not found: {e}")
                sys.exit(1)
        elif cmd == "--test-download":
            # VM smoke test: start Chrome, log in, download the Unusual Activity
            # CSV once, report, close. No Telegram, no Excel writes.
            _d = LazyDriver()
            _ok = False
            try:
                _logged = ensure_logged_in(_d)
                print("TEST login:", "OK" if _logged else "FAILED")
                _f = download_unusual_options_csv(_d)
                if _f:
                    try:
                        _rows = len(read_barchart_csv(_f))
                    except Exception as _e:
                        _rows = f"unreadable ({_e})"
                    print(f"TEST OK: downloaded {os.path.basename(_f)} -- rows: {_rows}")
                    archive_csv(_f)
                    _ok = True
                else:
                    print("TEST FAILED: no CSV downloaded. Screenshot saved in", CONFIG["debug_folder"])
            finally:
                _d.release()
            sys.exit(0 if _ok else 1)
        elif cmd in ("--help", "-h"):
            print("Usage:")
            print("  python barchart_pro_bot.py                                 # run bot")
            print("  python barchart_pro_bot.py --backtest [N]                  # backtest last N days (excludes watchlist)")
            print("  python barchart_pro_bot.py --backtest [N] --with-watchlist # include watchlist in headline")
            print("  python barchart_pro_bot.py --label-eod                     # force EOD outcome labeler")
            print("  python barchart_pro_bot.py --setup-schwab                  # one-time Schwab OAuth handshake")
            print("  python barchart_pro_bot.py --refresh-schwab                # smart refresh: re-auth only if needed")
            print("  python barchart_pro_bot.py --schwab-status                 # show Schwab integration status + token expiry")
            print("  python barchart_pro_bot.py --test-download                # VM smoke test: login + one CSV download")
            print("  python barchart_pro_bot.py --schwab-monitor                # show token-health monitoring state")
            sys.exit(0)

    _driver = None
    _exit_code = 0
    try:
        _driver = main()
    except KeyboardInterrupt:
        print("\nBot interrupted by user (Ctrl+C).")
    except Exception as e:
        print("FATAL ERROR:", e)
        import traceback
        traceback.print_exc()
        if sys.stdin is not None and sys.stdin.isatty():
            input("Press Enter to close...")
        _exit_code = 1
    finally:
        # v17.6: always close Chrome on any exit path
        if _driver is not None:
            try:
                _driver.quit()
                print("Chrome browser closed.")
            except Exception as _qe:
                print(f"Chrome close error (ignored): {_qe}")
    sys.exit(_exit_code)
