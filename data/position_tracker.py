"""
data/position_tracker.py
========================
Exit alert monitor for UW Options Bot.

Runs at the end of every scan cycle.
Checks current price of all open positions against:
  - Target (+65% from entry)    → TAKE PROFIT alert
  - Stop   (-70% from entry)    → STOP LOSS alert
  - Breakeven trail (+30%)      → MOVE STOP TO BREAKEVEN alert

No orders are placed. Alerts only. You execute manually.

How to add a position:
  From Python:
    from database.models_db import add_position
    add_position("QQQ", 720.0, "C", "2026-07-02", 1, entry_premium=5.42)

  Or via main.py:
    python main.py --mode addposition

Price source waterfall (same as signal quotes):
  1. IBKR
  2. Schwab
  3. UW chain
"""

import logging
from database.models_db import (
    get_open_positions, update_position_price,
    mark_position_alerted, close_position,
)

log = logging.getLogger("UWBot.PositionTracker")


class PositionTracker:

    def __init__(self, uw_client, ibkr_client, schwab_client, telegram):
        self.uw       = uw_client
        self.ibkr     = ibkr_client
        self.schwab   = schwab_client
        self.telegram = telegram

    def check_all_positions(self) -> list[dict]:
        """
        Check all open positions for exit conditions.
        Called at end of every scan cycle.
        Returns list of alerts fired.
        """
        positions = get_open_positions()
        if not positions:
            return []

        log.info("Checking {} open position(s)...".format(len(positions)))
        alerts_fired = []

        for pos in positions:
            try:
                alert = self._check_position(pos)
                if alert:
                    alerts_fired.append(alert)
            except Exception as e:
                log.error("Position check error {}: {}".format(pos["id"], e))

        return alerts_fired

    def _check_position(self, pos: dict) -> dict | None:
        """
        Check a single position against exit conditions.
        Returns alert dict if alert fired, else None.
        """
        # Get current price
        current = self._get_current_price(
            pos["symbol"], pos["expiry"],
            pos["strike"], pos["right"]
        )

        if current <= 0:
            log.debug("Position {}: no quote available".format(pos["id"]))
            return None

        update_position_price(pos["id"], current)

        entry  = pos["entry_premium"]
        target = pos["target_premium"]
        stop   = pos["stop_premium"]
        trail  = pos.get("breakeven_premium") or entry
        pid    = pos["id"]

        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0

        log.debug("Position {} {} {}{}  entry={:.2f}  current={:.2f}  pnl={:+.1f}%".format(
            pid, pos["symbol"], pos["strike"], pos["right"],
            entry, current, pnl_pct
        ))

        # ── TARGET HIT (+65%) ─────────────────────────────────────────────────
        if current >= target and not pos["target_alerted"]:
            mark_position_alerted(pid, "target")
            msg = self._format_exit_alert(pos, current, "TARGET HIT", pnl_pct)
            self.telegram.send(msg)
            log.info("TARGET HIT: {} {}{}  +{:.1f}%  current={:.2f}  target={:.2f}".format(
                pos["symbol"], pos["strike"], pos["right"], pnl_pct, current, target))
            return {"type": "target", "position": pos, "current": current}

        # ── STOP HIT (-70%) ───────────────────────────────────────────────────
        if current <= stop and not pos["stop_alerted"]:
            mark_position_alerted(pid, "stop")
            msg = self._format_exit_alert(pos, current, "STOP LOSS", pnl_pct)
            self.telegram.send(msg)
            log.info("STOP HIT: {} {}{}  {:.1f}%  current={:.2f}  stop={:.2f}".format(
                pos["symbol"], pos["strike"], pos["right"], pnl_pct, current, stop))
            return {"type": "stop", "position": pos, "current": current}

        # ── BREAKEVEN TRAIL (+30%) ────────────────────────────────────────────
        # When position is up 30%, alert to move stop to breakeven
        trail_trigger = entry * 1.30
        if (current >= trail_trigger
                and not pos["breakeven_alerted"]
                and not pos["target_alerted"]):
            mark_position_alerted(pid, "breakeven")
            msg = self._format_trail_alert(pos, current, entry, pnl_pct)
            self.telegram.send(msg)
            log.info("TRAIL: {} {}{}  +{:.1f}%  move stop to {:.2f}".format(
                pos["symbol"], pos["strike"], pos["right"], pnl_pct, entry))
            return {"type": "breakeven", "position": pos, "current": current}

        # ── EXPIRY WARNING ────────────────────────────────────────────────────
        from datetime import date
        try:
            dte = (date.fromisoformat(pos["expiry"]) - date.today()).days
            if dte <= 2 and not pos["target_alerted"] and not pos["stop_alerted"]:
                msg = self._format_expiry_alert(pos, current, dte, pnl_pct)
                self.telegram.send(msg)
                log.info("EXPIRY WARNING: {} {}{}  {} DTE  pnl={:+.1f}%".format(
                    pos["symbol"], pos["strike"], pos["right"], dte, pnl_pct))
                return {"type": "expiry", "position": pos, "current": current}
        except Exception:
            pass

        return None

    def _get_current_price(
        self, symbol: str, expiry: str, strike: float, right: str
    ) -> float:
        """Get current option mid price. Same waterfall as signal quotes."""
        # IBKR
        q = self.ibkr.get_option_quote(
            symbol, expiry.replace("-", ""), strike, right)
        if q and q.get("ask", 0) > 0:
            bid = q.get("bid", 0)
            ask = q.get("ask", 0)
            return round((bid + ask) / 2, 2)

        # Schwab
        if self.schwab.available:
            q = self.schwab.get_option_quote(symbol, expiry, strike, right)
            if q and q.get("ask", 0) > 0:
                bid = q.get("bid", 0)
                ask = q.get("ask", 0)
                return round((bid + ask) / 2, 2)

        # UW chain
        q = self.uw.find_contract_quote(symbol, expiry, strike, right)
        if q and q.get("ask", 0) > 0:
            bid = q.get("bid", 0)
            ask = q.get("ask", 0)
            return round((bid + ask) / 2, 2)

        return 0.0

    # ── Alert formatters ──────────────────────────────────────────────────────
    def _format_exit_alert(
        self, pos: dict, current: float, alert_type: str, pnl_pct: float
    ) -> str:
        is_target = "TARGET" in alert_type
        emoji     = "💰" if is_target else "🛑"
        pnl_usd   = round(
            (current - pos["entry_premium"]) * pos["contracts"] * 100, 2)
        sep       = "-" * 20

        lines = [
            "{} <b>{}: {} {}{}</b>".format(
                emoji, alert_type,
                pos["symbol"], pos["strike"], pos["right"]),
            sep,
            "<b>Expiry:</b>   {}".format(pos["expiry"]),
            "<b>Contracts:</b> {}".format(pos["contracts"]),
            sep,
            "<b>Entry:</b>   {:.2f}".format(pos["entry_premium"]),
            "<b>Current:</b> {:.2f}".format(current),
            "<b>P&L:</b>     {:+.1f}%  (${:+,.0f})".format(pnl_pct, pnl_usd),
            sep,
        ]

        if is_target:
            lines += [
                "ACTION: Consider closing position.",
                "You can also close half and trail the rest.",
            ]
        else:
            lines += [
                "ACTION: Consider closing position to limit losses.",
                "Check if thesis still valid before closing.",
            ]

        lines.append("<i>Alert only -- no order placed</i>")
        return "\n".join(lines)

    def _format_trail_alert(
        self, pos: dict, current: float, entry: float, pnl_pct: float
    ) -> str:
        sep = "-" * 20
        lines = [
            "📈 <b>MOVE STOP TO BREAKEVEN: {} {}{}</b>".format(
                pos["symbol"], pos["strike"], pos["right"]),
            sep,
            "Position is up <b>{:+.1f}%</b>  (current: {:.2f})".format(
                pnl_pct, current),
            sep,
            "<b>ACTION:</b> Move your stop loss to <b>{:.2f}</b> (entry cost)".format(entry),
            "This locks in a breakeven trade minimum.",
            "Target still: {:.2f}  (+65%)".format(pos["target_premium"]),
            sep,
            "<i>Alert only -- no order placed</i>",
        ]
        return "\n".join(lines)

    def _format_expiry_alert(
        self, pos: dict, current: float, dte: int, pnl_pct: float
    ) -> str:
        sep = "-" * 20
        lines = [
            "⏰ <b>EXPIRY WARNING: {} {}{}</b>".format(
                pos["symbol"], pos["strike"], pos["right"]),
            sep,
            "<b>Expires in {} day(s):</b>  {}".format(dte, pos["expiry"]),
            "<b>Current:</b>  {:.2f}  ({:+.1f}%)".format(current, pnl_pct),
            sep,
            "<b>ACTION:</b> Review position -- {}".format(
                "consider closing for profit." if pnl_pct > 0
                else "consider cutting loss before expiry."
            ),
            sep,
            "<i>Alert only -- no order placed</i>",
        ]
        return "\n".join(lines)
