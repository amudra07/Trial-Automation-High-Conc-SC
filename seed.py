"""seed.py -- run once: `python seed.py`

Loads the static ENTRIES from tech_landscape_data.py into the SQLite
database as the starting curated dataset. Safe to re-run.
"""
from tech_landscape_data import ENTRIES
import db

db.init_db()
db.seed_from_static(ENTRIES)
print(f"Seeded {len(ENTRIES)} entries into {db.DB_PATH}")
