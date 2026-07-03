"""
init_db.py -- Initialize / migrate the bot database without starting the full bot.

Run this once to create all tables including score_evidence:
    python init_db.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from database.db import init_db

print("Initializing database...")
init_db()
print("Done. All tables created / migrated.")
print("You can now run: python ml/weight_optimizer.py")
