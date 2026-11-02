import sqlite3
import re
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "backend" / "data" / "sap_cds.db"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
c = conn.cursor()

tables_to_check = ["MARA", "ACDOCA", "MATDOC", "BKPF", "VBAK"]

for tbl in tables_to_check:
    print(f"\n=================== TABLE: {tbl} ===================")
    
    # 1. Check in cds_views where parent_name = tbl (AS SELECT FROM tbl)
    c.execute("SELECT cds_name, parent_name, source_type FROM cds_views WHERE UPPER(parent_name) = ?", (tbl,))
    parent_views = [r["cds_name"] for r in c.fetchall()]
    print(f"Direct AS SELECT FROM {tbl} in cds_views: {len(parent_views)} views")
    
    # 2. Check in cds_joins where right_entity = tbl (JOIN tbl)
    c.execute("SELECT cds_name, join_type, right_entity FROM cds_joins WHERE UPPER(right_entity) = ?", (tbl,))
    join_views = [r["cds_name"] for r in c.fetchall()]
    print(f"Direct JOIN {tbl} in cds_joins: {len(join_views)} views")
    
    # Combined direct candidates
    direct_candidates = set(parent_views + join_views)
    print(f"Total direct candidates for {tbl}: {len(direct_candidates)}")
    
    # Check extraction enablement in sap_extraction_catalog
    ph = ",".join(["?"] * len(direct_candidates)) if direct_candidates else "''"
    c.execute(f"SELECT view_name FROM sap_extraction_catalog WHERE UPPER(view_name) IN ({ph})", [v.upper() for v in direct_candidates])
    ext_catalog = [r["view_name"] for r in c.fetchall()]
    print(f"Found in sap_extraction_catalog: {len(ext_catalog)}")
    for v in ext_catalog[:10]:
        print(f"   -> {v}")

    # Check cds_annotations for extraction enabled = true
    c.execute(f"""
        SELECT ddl_name, annotation_value FROM cds_annotations 
        WHERE UPPER(ddl_name) IN ({ph}) 
        AND UPPER(annotation_name) LIKE '%DATAEXTRACTION.ENABLED%'
        AND LOWER(annotation_value) = 'true'
    """, [v.upper() for v in direct_candidates])
    ext_annot = [r["ddl_name"] for r in c.fetchall()]
    print(f"Found in cds_annotations (dataExtraction.enabled = true): {len(ext_annot)}")
    for v in ext_annot[:10]:
        print(f"   -> {v}")

conn.close()
