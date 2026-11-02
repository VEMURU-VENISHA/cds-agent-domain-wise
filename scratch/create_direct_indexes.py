import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "backend" / "data" / "sap_cds.db"
conn = sqlite3.connect(str(db_path))
c = conn.cursor()

c.execute("CREATE INDEX IF NOT EXISTS idx_cds_views_parent_upper ON cds_views(UPPER(parent_name))")
c.execute("CREATE INDEX IF NOT EXISTS idx_cds_joins_right_upper ON cds_joins(UPPER(right_entity))")
conn.commit()
print("Indexes created successfully!")
conn.close()
