"""
cleanup_signals.py
==================

Removes corrupt placeholder rows from signals.xlsx that were created by an
earlier bug. These rows have:
  - Score = 3
  - No Premium, no Entry, no Stop Loss, no Target 1
  - Reasons field is exactly "Flow confirmed"
  - But they got tagged with TARGET_HIT or STOP_HIT outcomes by the EOD labeler

These rows have been inflating the bot's win-rate statistics and corrupting
the auto-learning loop. This script removes them.

Usage:
    cd C:\\BarchartBot

    # First, see what WOULD be removed (no changes made):
    python cleanup_signals.py --dry-run

    # If the count looks right, do the cleanup:
    python cleanup_signals.py

    # If you want to skip the safety prompt:
    python cleanup_signals.py --yes

The script always creates a timestamped backup before modifying anything.
"""

import os
import sys
import shutil
from datetime import datetime
from openpyxl import load_workbook


SIGNALS_FILE = "signals.xlsx"
SHEET_NAME = "OptionSignals"


def is_corrupt_row(row_dict):
    """
    Returns True if this row matches the known corrupt-placeholder signature.
    All five conditions must match to avoid false positives.
    """
    score = row_dict.get("Score")
    premium = row_dict.get("Premium")
    entry = row_dict.get("Entry")
    stop = row_dict.get("Stop Loss")
    target1 = row_dict.get("Target 1")
    reasons = str(row_dict.get("Reasons", "")).strip()

    return (
        score == 3
        and (premium is None or premium == 0)
        and (entry is None or entry == 0)
        and stop is None
        and target1 is None
        and reasons == "Flow confirmed"
    )


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    skip_confirm = "--yes" in args

    if not os.path.exists(SIGNALS_FILE):
        print(f"ERROR: {SIGNALS_FILE} not found in current directory.")
        print(f"Run this from your bot folder (C:\\BarchartBot\\).")
        sys.exit(1)

    print(f"Loading {SIGNALS_FILE}...")
    wb = load_workbook(SIGNALS_FILE)

    if SHEET_NAME not in wb.sheetnames:
        print(f"ERROR: Sheet '{SHEET_NAME}' not found.")
        print(f"Available sheets: {wb.sheetnames}")
        sys.exit(1)

    ws = wb[SHEET_NAME]
    headers = [str(cell.value) if cell.value is not None else "" for cell in ws[1]]

    # Find corrupt rows by row number (1-indexed; header is row 1).
    corrupt_rows = []
    for row_num in range(2, ws.max_row + 1):
        row_dict = {}
        for col_idx, header in enumerate(headers, start=1):
            row_dict[header] = ws.cell(row=row_num, column=col_idx).value
        if is_corrupt_row(row_dict):
            corrupt_rows.append({
                "row_num": row_num,
                "datetime": row_dict.get("DateTime", ""),
                "ticker": row_dict.get("Ticker", ""),
                "type": row_dict.get("Option Type", ""),
                "strike": row_dict.get("Strike", ""),
                "status": row_dict.get("Status", ""),
            })

    print(f"\nFound {len(corrupt_rows)} corrupt placeholder rows.")
    print(f"Total rows in {SHEET_NAME}: {ws.max_row - 1}")
    print(f"After cleanup: {ws.max_row - 1 - len(corrupt_rows)}")

    if not corrupt_rows:
        print("\nNothing to clean up. Exiting.")
        sys.exit(0)

    print("\nPreview (first 10 rows that would be deleted):")
    for row_info in corrupt_rows[:10]:
        print(
            f"  Row {row_info['row_num']:4} | "
            f"{row_info['datetime']!s:20} | "
            f"{row_info['ticker']!s:6} | "
            f"{row_info['type']!s:5} | "
            f"strike {row_info['strike']!s:8} | "
            f"status {row_info['status']!s}"
        )
    if len(corrupt_rows) > 10:
        print(f"  ... and {len(corrupt_rows) - 10} more.")

    if dry_run:
        print("\n--dry-run specified. No changes made.")
        sys.exit(0)

    if not skip_confirm:
        print()
        response = input("Proceed with deletion? Type 'yes' to confirm: ").strip().lower()
        if response != "yes":
            print("Cancelled. No changes made.")
            sys.exit(0)

    # Backup the workbook before modifying.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{SIGNALS_FILE}.backup_{timestamp}"
    print(f"\nCreating backup: {backup_path}")
    shutil.copy2(SIGNALS_FILE, backup_path)

    # Delete rows in reverse order so row numbers stay stable.
    print(f"Deleting {len(corrupt_rows)} rows...")
    for row_info in sorted(corrupt_rows, key=lambda r: -r["row_num"]):
        ws.delete_rows(row_info["row_num"], 1)

    print(f"Saving {SIGNALS_FILE}...")
    wb.save(SIGNALS_FILE)
    print("Done.")
    print()
    print(f"Backup is at: {backup_path}")
    print(f"If anything looks wrong, restore by:")
    print(f"  copy {backup_path} {SIGNALS_FILE}")
    print()
    print("Recommended next step: run the backtest to see the cleaned numbers:")
    print("  python barchart_pro_bot.py --backtest 30")


if __name__ == "__main__":
    main()
