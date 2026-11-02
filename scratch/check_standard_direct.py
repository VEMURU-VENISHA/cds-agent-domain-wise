import sqlite3
import re
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "backend" / "data" / "sap_cds.db"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
c = conn.cursor()

def check_views(vnames):
    for v in vnames:
        row = c.execute("SELECT ddl_name, source_code FROM cds_sources WHERE UPPER(ddl_name) = ?", (v.upper(),)).fetchone()
        if not row:
            row = c.execute("SELECT cds_name as ddl_name, source_code FROM cds_views WHERE UPPER(cds_name) = ?", (v.upper(),)).fetchone()
        
        if row and row["source_code"]:
            src = row["source_code"]
            # check from and joins
            m_from = re.findall(r"\bas\s+select\s+from\s+([A-Za-z0-9_\/]+)", src, re.IGNORECASE)
            m_join = re.findall(r"\bjoin\s+([A-Za-z0-9_\/]+)", src, re.IGNORECASE)
            m_ext = re.search(r"@Analytics(?:\.dataExtraction\.enabled\s*:\s*true|\s*:\s*\{[^\}]*\bdataExtraction\.enabled\s*:\s*true)", src, re.IGNORECASE)
            print(f"View: {v}")
            print(f"  FROM: {m_from}")
            print(f"  JOIN: {m_join}")
            print(f"  Extraction in source: {bool(m_ext)}")
        else:
            print(f"View: {v} - NO SOURCE")

check_views(["I_PRODUCT", "I_PRODPLNTINPTASSISTANT", "I_PRODUCTSALES", "I_PRODUCTSTORAGE_2"])
