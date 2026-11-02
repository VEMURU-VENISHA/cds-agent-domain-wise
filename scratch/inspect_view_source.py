import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "backend" / "data" / "sap_cds.db"
conn = sqlite3.connect(str(db_path))
c = conn.cursor()

c.execute("SELECT ddl_name, source_code FROM cds_sources WHERE UPPER(source_code) LIKE '%ACDOCA%' AND UPPER(source_code) LIKE '%MATDOC%'")
rows = c.fetchall()
print("Views in cds_sources mentioning both ACDOCA and MATDOC:", len(rows))
for r in rows:
    print("  -", r[0])
