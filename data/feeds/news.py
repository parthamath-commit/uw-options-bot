"""
data/feeds/news.py
==================
News feed -- market-moving headlines from multiple sources.

Verified field names from live API (2026-06-22):

  /api/news/headlines:
    headline, source, created_at, tickers (list), sentiment, is_major, tags

  /api/market/economic-calendar:
    event, time (ISO datetime), prev, forecast, type, reported_period

  /api/market/fda-calendar:
    ticker, drug, catalyst, start_date, target_date, event_type,
    description, status, outcome, indication

  /api/earnings/premarket + /afterhours:
    symbol, full_name, actual_eps, street_mean_est, report_date,
    report_time, reaction, expected_move_perc, sector
"""

import logging
from datetime import datetime, date, timedelta, timezone
from models_feed import FeedSignal

log = logging.getLogger("UWBot.Feed.News")

MARKET_MOVING_KEYWORDS = [
    "fed", "federal reserve", "fomc", "interest rate", "rate hike", "rate cut",
    "cpi", "inflation", "pce", "jobs report", "nonfarm", "gdp", "recession",
    "tariff", "trade war", "geopolitical", "war", "sanctions",
    "debt ceiling", "government shutdown", "treasury",
    "powell", "yellen", "jerome",
]

STOCK_MOVING_KEYWORDS = [
    "earnings", "beat", "miss", "guidance", "revenue", "eps",
    "fda", "approval", "rejected", "trial", "clinical",
    "merger", "acquisition", "buyout", "takeover", "deal",
    "upgrade", "downgrade", "price target", "overweight", "underweight",
    "lawsuit", "investigation", "sec", "doj",
    "ceo", "resignation", "hired", "fired",
    "recall", "shortage", "supply chain",
]

HIGH_IMPACT_EVENTS = [
    "fomc", "cpi", "pce", "nonfarm payroll", "gdp", "retail sales",
    "fed meeting", "interest rate decision", "jackson hole",
    "jobs report", "unemployment", "consumer sentiment",
]


def fetch(client, lookback_hours: int = 4) -> list[FeedSignal]:
    signals = []
    signals.extend(_fetch_headlines(client, lookback_hours))
    signals.extend(_fetch_economic_calendar(client))
    signals.extend(_fetch_fda_calendar(client))
    signals.extend(_fetch_earnings(client))
    signals.sort(key=lambda x: x.score, reverse=True)
    log.info("News: {} market-moving signals".format(len(signals)))
    return signals


def _fetch_headlines(client, lookback_hours: int) -> list[FeedSignal]:
    """Fetch UW news headlines. API fields: headline, source, created_at, tickers, sentiment."""
    raw       = client._get("/api/news/headlines", {"limit": 50})
    headlines = client._as_list(raw)
    signals   = []

    for h in headlines:
        try:
            # API: 'headline' not 'title', no 'body' field
            title = (h.get("headline") or h.get("title") or "").strip()
            if not title:
                continue

            full = title.lower()

            # API: 'tickers' is a list (may be JSON string '[]' or actual list)
            raw_tickers = h.get("tickers") or h.get("ticker") or []
            if isinstance(raw_tickers, str):
                import json
                try:
                    raw_tickers = json.loads(raw_tickers)
                except Exception:
                    raw_tickers = [raw_tickers] if raw_tickers else []
            ticker = raw_tickers[0].upper() if raw_tickers else "MARKET"

            source   = h.get("source") or h.get("publisher") or ""
            # API: 'created_at' not 'published_at'
            pub_time = h.get("created_at") or h.get("published_at") or ""
            is_major = str(h.get("is_major", "False")).lower() == "true"

            is_market = any(kw in full for kw in MARKET_MOVING_KEYWORDS)
            is_stock  = any(kw in full for kw in STOCK_MOVING_KEYWORDS)

            if not is_market and not is_stock and not is_major:
                continue

            # API: 'sentiment' field already classified — use it directly
            api_sentiment = (h.get("sentiment") or "neutral").lower()
            if api_sentiment in ("bullish", "bearish", "neutral"):
                intent = api_sentiment
            else:
                intent = _classify_news_intent(full)

            score = _score_news(full, is_market, is_stock, is_major, source)
            if score < 40:
                continue

            news_type = "MARKET NEWS" if is_market else "STOCK NEWS"

            signals.append(FeedSignal(
                feed_type = "news",
                symbol    = ticker if not is_market else "MARKET",
                intent    = intent,
                score     = score,
                notes     = "[{}] {}\nSource: {}".format(news_type, title[:120], source),
                source_id = h.get("id", "") or pub_time,
                raw       = h,
            ))

        except Exception as e:
            log.debug("News parse error: {}".format(e))

    return signals


def _fetch_economic_calendar(client) -> list[FeedSignal]:
    """
    API fields: event, time (ISO datetime), prev, forecast, type, reported_period
    No 'impact'/'date' fields — parse date from 'time' field.
    """
    raw    = client._get("/api/market/economic-calendar")
    events = client._as_list(raw)
    signals = []
    today = date.today()

    for e in events:
        try:
            name = (e.get("event") or e.get("name") or "").lower()

            # API: 'time' is full ISO datetime e.g. '2026-06-26T14:00:00Z'
            time_str = e.get("time") or e.get("date") or e.get("event_date") or ""
            if not time_str:
                continue

            # Parse date from time field
            try:
                if "T" in time_str:
                    event_date = datetime.fromisoformat(
                        time_str.replace("Z", "+00:00")).date()
                else:
                    event_date = date.fromisoformat(time_str[:10])
            except Exception:
                continue

            days_away = (event_date - today).days
            if days_away < 0 or days_away > 3:
                continue

            # No impact field in API — score based on event keywords
            is_high_impact = any(kw in name for kw in HIGH_IMPACT_EVENTS)
            if not is_high_impact:
                continue

            score = 85 if days_away == 0 else 70 if days_away == 1 else 55
            if any(kw in name for kw in ["fomc", "cpi", "nonfarm", "gdp"]):
                score = min(score + 10, 100)

            forecast = e.get("forecast")
            prev     = e.get("prev")
            period   = e.get("reported_period") or ""

            note_parts = ["[ECON EVENT] {}{}".format(
                (e.get("event") or "")[:80],
                " ({})".format(period) if period else "")]
            note_parts.append("Date: {}".format(event_date.isoformat()))
            if forecast and str(forecast) != "None":
                note_parts.append("Forecast: {}  Previous: {}".format(
                    forecast, prev or "N/A"))

            signals.append(FeedSignal(
                feed_type = "news",
                symbol    = "MARKET",
                intent    = "neutral",
                score     = score,
                notes     = "\n".join(note_parts),
                raw       = e,
            ))

        except Exception as e_:
            log.debug("Econ calendar parse: {}".format(e_))

    return signals


def _fetch_fda_calendar(client) -> list[FeedSignal]:
    """
    API fields: ticker, drug, catalyst, start_date, target_date,
                event_type, description, status, outcome, indication
    """
    raw    = client._get("/api/market/fda-calendar")
    events = client._as_list(raw)
    signals = []
    today  = date.today()

    for e in events:
        try:
            ticker = (e.get("ticker") or e.get("symbol") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            # API: 'drug' not 'drug_name', dates in 'start_date'/'target_date'
            drug_name = e.get("drug") or e.get("drug_name") or e.get("catalyst") or ""
            date_str  = (e.get("target_date") or e.get("start_date") or
                         e.get("date") or e.get("pdufa_date") or "")[:10]

            if not date_str:
                continue

            try:
                event_date = date.fromisoformat(date_str)
            except Exception:
                continue

            days_away = (event_date - today).days
            if days_away < 0 or days_away > 14:
                continue

            event_type = e.get("event_type") or e.get("catalyst") or "FDA Event"
            score = 90 if days_away <= 1 else 80 if days_away <= 3 else 65

            signals.append(FeedSignal(
                feed_type = "news",
                symbol    = ticker,
                intent    = "neutral",
                score     = score,
                notes     = "[FDA CATALYST] {} -- {}\nDate: {}  Days: {}  Type: {}".format(
                    ticker, drug_name or event_type,
                    date_str, days_away, event_type),
                raw = e,
            ))

        except Exception as e_:
            log.debug("FDA calendar parse: {}".format(e_))

    return signals


def _fetch_earnings(client) -> list[FeedSignal]:
    """
    API fields: symbol (not ticker), full_name, actual_eps, street_mean_est,
                report_date, report_time, reaction, expected_move_perc
    """
    raw_pre  = client._get("/api/earnings/premarket")
    raw_aft  = client._get("/api/earnings/afterhours")
    signals  = []

    all_earnings = (
        [(e, "PREMARKET")  for e in client._as_list(raw_pre)] +
        [(e, "AFTERHOURS") for e in client._as_list(raw_aft)]
    )

    for e, session in all_earnings:
        try:
            # API: 'symbol' not 'ticker'
            ticker = (e.get("symbol") or e.get("ticker") or "").upper()
            if not ticker or len(ticker) > 5:
                continue

            # API: 'street_mean_est' not 'eps_estimate'/'estimated_eps'
            eps_est = e.get("street_mean_est") or e.get("eps_estimate") or e.get("estimated_eps")
            eps_act = e.get("actual_eps")

            # API: 'full_name' not 'company_name'/'name'
            name = e.get("full_name") or e.get("company_name") or e.get("name") or ticker

            # API: 'reaction' = post-earnings price move %, 'expected_move_perc'
            reaction      = e.get("reaction")
            expected_move = e.get("expected_move_perc")

            intent    = "neutral"
            beat_miss = ""
            if eps_act is not None and eps_est is not None:
                try:
                    if float(eps_act) > float(eps_est):
                        intent    = "bullish"
                        beat_miss = "EPS BEAT"
                    elif float(eps_act) < float(eps_est):
                        intent    = "bearish"
                        beat_miss = "EPS MISS"
                except Exception:
                    pass

            # If actual reaction available, use that as intent
            if reaction is not None:
                try:
                    rxn = float(reaction)
                    if rxn > 2:   intent = "bullish"
                    elif rxn < -2: intent = "bearish"
                except Exception:
                    pass

            score = 80 if beat_miss else 65

            note_parts = ["[EARNINGS {}] {}".format(session, name)]
            if eps_est:
                note_parts.append("EPS est: {}  act: {}{}".format(
                    eps_est, eps_act or "TBD",
                    "  ** {} **".format(beat_miss) if beat_miss else ""))
            if expected_move:
                note_parts.append("Expected move: ±{}%".format(expected_move))
            if reaction is not None:
                note_parts.append("Reaction: {}%".format(reaction))

            signals.append(FeedSignal(
                feed_type = "news",
                symbol    = ticker,
                intent    = intent,
                score     = score,
                notes     = "\n".join(note_parts),
                raw       = e,
            ))

        except Exception as e_:
            log.debug("Earnings parse: {}".format(e_))

    log.debug("Earnings: {} signals".format(len(signals)))
    return signals


def _classify_news_intent(text: str) -> str:
    bullish_words = [
        "beat","beats","surge","soar","rally","gain","rise",
        "positive","strong","record","approval","approved",
        "upgrade","buy","outperform","bullish","growth",
    ]
    bearish_words = [
        "miss","misses","drop","fall","decline","loss","weak",
        "negative","rejected","rejection","downgrade","sell",
        "underperform","bearish","cut","lower","recession",
        "lawsuit","fine","penalty","recall",
    ]
    bull_count = sum(1 for w in bullish_words if w in text)
    bear_count = sum(1 for w in bearish_words if w in text)
    if bull_count > bear_count:   return "bullish"
    if bear_count > bull_count:   return "bearish"
    return "neutral"


def _score_news(text: str, is_market: bool, is_stock: bool,
                is_major: bool, source: str) -> int:
    s = 30
    if is_major:   s += 20
    if is_market:  s += 25
    if is_stock:   s += 15
    if any(kw in text for kw in ["fomc", "fed rate", "cpi", "nonfarm"]):
        s += 20
    if any(kw in text for kw in ["fda approval", "merger", "acquisition"]):
        s += 15
    if any(kw in text for kw in ["earnings beat", "earnings miss"]):
        s += 10
    reputable = ["reuters", "bloomberg", "wsj", "ft", "cnbc", "ap ", "dow jones"]
    if any(src in source.lower() for src in reputable):
        s += 10
    return max(0, min(100, s))
