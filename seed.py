"""
seed.py -- run once: `python seed.py`

Loads the original static ENTRIES from tech_landscape_data.py into the
new SQLite database as the starting 'curated' dataset. Safe to re-run;
uses INSERT OR IGNORE so it won't overwrite anything you've since
edited or approved through the app.
"""

from tech_landscape_data import ENTRIES
import db

db.init_db()
db.seed_from_static(ENTRIES)
print(f"Seeded {len(ENTRIES)} entries into {db.DB_PATH}")
