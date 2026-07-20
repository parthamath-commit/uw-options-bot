"""
persistence/excel.py
====================
Signal logger — CSV hot path + on-demand styled xlsx export.

WHY THE REWRITE (2026-07-20):
  The old logger did load_workbook() -> append -> save() for EVERY
  signal (~132/cycle). openpyxl rewrites the entire file on save, so
  each signal cost ~20-25s on the 1-vCPU Oracle Micro VM, stretching
  scan cycles from ~5 min to ~50 min (py-spy confirmed 3/3 stack dumps
  inside openpyxl save/load). A process restart mid-save also corrupted
  the workbook repeatedly (signals_corrupt_*.xlsx).

NEW DESIGN:
  log_signal(sig)  -> appends ONE line to signals.csv (microseconds,
                      append-only, corruption-proof). Same signature
                      as before — scanner.py call sites unchanged.
  export_xlsx()    -> rebuilds the styled signals.xlsx from the CSV.
                      Call once per cycle (or on demand). Atomic
                      write (tmp file + os.replace) so a kill mid-save
                      can never corrupt the visible file.

ALSO FIXED:
  Column misalignment — the old row order wrote `structure` under the
  "Signal Type" header, shifting Signal Type/Rule Type/ML Validation/
  Structure each one cell to the right. Row order now matches COLUMNS.

Colour coding (unchanged):
  Green  = bullish (not blocked)
  Red    = bearish (not blocked)
  Amber  = GEX regime blocked
  Navy header
"""

import csv
import logging
import os
from pathlib import Path

from config import cfg
from models import ScoredSignal

log = logging.getLogger("UWBot.Excel")

try:
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False
    log.warning("openpyxl not installed — run: pip install openpyxl")


class ExcelLogger:
    COLUMNS = [
        "Timestamp", "Symbol", "Strike", "Right", "Expiry", "Signal Type", "Rule Type", "ML Validation",
        "Structure", "Ask Side", "Intent", "Premium ($)",
        "UW Score", "Additive", "Institutional", "Composite",
        "IV Pct", "Dark Pool", "GEX ($M)", "DEX ($M)",
        "Regime", "Gamma Flip", "Call Wall", "Put Wall",
        "Flow Dir", "GEX Blocked",
        "Bid", "Ask", "Delta",
        "Contracts", "Max Risk $", "Target Premium", "Stop Premium",
    ]
    WIDTHS = [
        22, 8, 9, 6, 12, 14, 12, 14,
        10, 9, 10, 13,
        9, 9, 13, 10,
        8, 12, 10, 10,
        18, 11, 11, 11,
        12, 11,
        7, 7, 7,
        10, 12, 15, 13,
    ]

    # Column indexes used for row colouring on export (0-based, match COLUMNS)
    _IDX_INTENT  = COLUMNS.index("Intent")
    _IDX_BLOCKED = COLUMNS.index("GEX Blocked")

    def __init__(self):
        self.path = Path(cfg.EXCEL_PATH)                     # signals.xlsx (view file)
        self.csv_path = self.path.with_suffix(".csv")        # signals.csv  (hot path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dirty = False                                  # True when CSV has rows not yet exported
        if OPENPYXL_OK:
            self._hdr  = PatternFill("solid", fgColor="1F3864")
            self._bull = PatternFill("solid", fgColor="C6EFCE")
            self._bear = PatternFill("solid", fgColor="FFC7CE")
            self._blok = PatternFill("solid", fgColor="FFEB9C")
        self._ensure_csv()

    # ── CSV hot path ──────────────────────────────────────────────────────────
    def _ensure_csv(self):
        """Create the CSV with a header row if missing or empty."""
        try:
            if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self.COLUMNS)
                log.info("Created: {}".format(self.csv_path))
        except Exception as e:
            log.error("CSV init error: {}".format(e))

    def log_signal(self, sig: ScoredSignal, _retry: bool = True):
        """
        Append one signal to the CSV. Microseconds; never loads the xlsx.
        Signature unchanged from the old implementation (scanner.py and
        tests call log_signal(sig)).
        """
        row = [
            sig.timestamp,
            sig.symbol,
            sig.strike,
            sig.right,
            sig.expiry,
            getattr(sig, "signal_type",   "unidirectional"),
            getattr(sig, "rule_type",     "unidirectional"),
            getattr(sig, "ml_validation", "rule_only"),
            sig.structure,
            "YES" if sig.ask_side else "NO",
            sig.intent,
            sig.premium,
            sig.uw_score,
            sig.additive_score,
            sig.institutional_score,
            sig.composite_score,
            sig.iv_percentile,
            sig.darkpool_sentiment,
            round(sig.dealer_gex / 1_000_000, 2),
            round(sig.dealer_dex / 1_000_000, 2),
            sig.dealer_regime,
            sig.gamma_flip,
            sig.call_wall,
            sig.put_wall,
            sig.flow_direction,
            "YES" if sig.gex_regime_blocked else "NO",
            sig.live_bid,
            sig.live_ask,
            sig.live_delta,
            sig.suggested_contracts,
            sig.max_loss_dollar,
            sig.target_exit_premium,
            sig.stop_premium,
        ]
        try:
            self._ensure_csv()
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
            self._dirty = True
        except Exception as e:
            log.error("CSV write error: {}".format(e))

    # ── Styled xlsx export (call once per cycle / on demand) ─────────────────
    def export_xlsx(self, force: bool = False):
        """
        Rebuild signals.xlsx from signals.csv with the original styling.
        Costs one full workbook write — the same price the old code paid
        PER SIGNAL — so calling this once per scan cycle is ~132x cheaper.
        Atomic: writes to a temp file, then os.replace() over the target,
        so a kill mid-save can never leave a corrupt signals.xlsx.
        No-op unless new rows were logged since the last export (or force=True).
        """
        if not OPENPYXL_OK:
            return
        if not self._dirty and not force:
            return
        try:
            with open(self.csv_path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            if not rows:
                return

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Signals"

            # Header
            ws.append(self.COLUMNS)
            for i, (cell, w) in enumerate(zip(ws[1], self.WIDTHS), 1):
                cell.fill      = self._hdr
                cell.font      = Font(color="FFFFFF", bold=True)
                cell.alignment = Alignment(horizontal="center")
                ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
            ws.freeze_panes = "A2"

            # Data rows (skip CSV header)
            for row in rows[1:]:
                ws.append(row)
                blocked = len(row) > self._IDX_BLOCKED and row[self._IDX_BLOCKED] == "YES"
                intent  = row[self._IDX_INTENT] if len(row) > self._IDX_INTENT else ""
                fill = (
                    self._blok if blocked
                    else self._bull if intent == "bullish"
                    else self._bear
                )
                for cell in ws[ws.max_row]:
                    cell.fill = fill

            # Atomic replace — never corrupts the visible file
            tmp = self.path.with_name(self.path.stem + ".tmp.xlsx")
            wb.save(tmp)
            os.replace(tmp, self.path)
            self._dirty = False
            log.info("Exported {} signals -> {}".format(len(rows) - 1, self.path))
        except Exception as e:
            log.error("xlsx export error: {}".format(e))