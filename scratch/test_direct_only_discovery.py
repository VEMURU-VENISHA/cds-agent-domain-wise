import sqlite3
import re
from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "backend" / "data" / "sap_cds.db"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
c = conn.cursor()

def test_direct_discovery(tables: List[str], matching_mode: str = "ANY", standard_only: bool = True):
    print(f"\n=======================================================")
    print(f"DISCOVERY TEST: tables={tables}, mode={matching_mode}, standard_only={standard_only}")
    print(f"=======================================================")
    
    valid_tables = [t.upper() for t in tables]
    
    # 1. Query direct candidates from index
    direct_candidates = set()
    for tbl in valid_tables:
        c.execute("SELECT cds_name FROM cds_views WHERE UPPER(parent_name) = ?", (tbl,))
        for r in c.fetchall():
            direct_candidates.add(r[0])
            
        c.execute("SELECT cds_name FROM cds_joins WHERE UPPER(right_entity) = ?", (tbl,))
        for r in c.fetchall():
            direct_candidates.add(r[0])
            
        c.execute("""
            SELECT source_ddl_name FROM cds_dependencies 
            WHERE UPPER(target_object_name) = ? 
            AND dependency_type IN ('SQL_SOURCE_DEPENDENCY', 'JOIN_DEPENDENCY')
        """, (tbl,))
        for r in c.fetchall():
            direct_candidates.add(r[0])
            
        c.execute("""
            SELECT ddl_name FROM cds_sources 
            WHERE (UPPER(source_code) LIKE ? OR UPPER(source_code) LIKE ?)
        """, (f"%FROM%{tbl}%", f"%JOIN%{tbl}%"))
        for r in c.fetchall():
            direct_candidates.add(r[0])

    print(f"Direct candidate views from index: {len(direct_candidates)}")
    
    recommendations = []
    
    for vname in direct_candidates:
        # Standard filter
        if standard_only and vname.upper().startswith(("Z", "Y", "/Z", "/Y")):
            continue
            
        # Get source code
        row = c.execute("SELECT source_code FROM cds_sources WHERE UPPER(ddl_name) = ?", (vname.upper(),)).fetchone()
        if not row or not row["source_code"]:
            row = c.execute("SELECT source_code FROM cds_views WHERE UPPER(cds_name) = ?", (vname.upper(),)).fetchone()
            
        if not row or not row["source_code"]:
            continue
            
        s_code = row["source_code"]
        
        # Clean comments
        clean = re.sub(r"/\*[\s\S]*?\*/", "", s_code)
        clean = re.sub(r"//.*$", "", clean, flags=re.MULTILINE)
        clean = re.sub(r"--.*$", "", clean, flags=re.MULTILINE)
        
        # Parse FROM
        m_from = re.findall(r"\bas\s+select\s+from\s+([A-Za-z0-9_\/]+)", clean, re.IGNORECASE)
        from_targets = [x.strip().upper() for x in m_from if x.upper() not in ("SELECT", "FROM")]
        
        # Parse JOIN
        m_join = re.findall(r"\bjoin\s+([A-Za-z0-9_\/]+)", clean, re.IGNORECASE)
        join_targets = [x.strip().upper() for x in m_join]
        
        direct_sources = set(from_targets + join_targets)
        matched_tables = [tbl for tbl in valid_tables if tbl in direct_sources]
        
        if not matched_tables:
            continue
            
        if matching_mode == "ALL":
            if len(matched_tables) != len(valid_tables):
                continue
                
        # Extraction check
        m_dot = re.search(r"@Analytics\.dataExtraction\.enabled\s*:\s*true\b", clean, re.IGNORECASE)
        m_block = re.search(r"@Analytics\s*:\s*\{[^\}]*\bdataExtraction\.enabled\s*:\s*true\b", clean, re.IGNORECASE)
        m_nested = re.search(r"@Analytics\s*:\s*\{[\s\S]*?dataExtraction\s*:\s*\{[\s\S]*?\benabled\s*:\s*true\b", clean, re.IGNORECASE)
        is_ext = bool(m_dot or m_block or m_nested)
        
        if not is_ext:
            continue
            
        # CDC check
        cdc_match = re.search(r"@Analytics(?:\.dataExtraction\.delta\.changeDataCapture|\s*:\s*\{[\s\S]*?changeDataCapture)", clean, re.IGNORECASE)
        is_cdc = bool(cdc_match)
        
        # SQL view name
        sql_view_match = re.search(r"@AbapCatalog\.sqlViewName\s*:\s*['\"]?([A-Za-z0-9_]+)['\"]?", clean, re.IGNORECASE)
        sql_view_name = sql_view_match.group(1).strip() if sql_view_match else "None (View Entity)"
        
        recommendations.append({
            "cds_name": vname,
            "sql_view_name": sql_view_name,
            "direct_from_source": from_targets[0] if from_targets else "None",
            "direct_join_sources": join_targets,
            "matched_input_tables": matched_tables,
            "dependency_classification": "DIRECT_TABLE_SOURCE",
            "cdc_status": "CDC_SUPPORTED" if is_cdc else "CDC_NOT_SUPPORTED",
            "source_verification_status": "VERIFIED_FROM_DDL_SOURCE"
        })
        
    print(f"Final Direct Recommendations: {len(recommendations)}")
    if not recommendations:
        print("  -> Message: No extraction-enabled CDS views directly reference the selected transparent database tables.")
    for r in recommendations:
        print(f"  - {r['cds_name']} | Classification: {r['dependency_classification']} | FROM: {r['direct_from_source']} | JOIN: {r['direct_join_sources']} | Matched: {r['matched_input_tables']} | SQL View: {r['sql_view_name']} | CDC: {r['cdc_status']}")

test_direct_discovery(["ACDOCA", "MATDOC"], matching_mode="ALL", standard_only=True)
test_direct_discovery(["ACDOCA", "MATDOC"], matching_mode="ANY", standard_only=True)
test_direct_discovery(["MARA"], matching_mode="ANY", standard_only=True)
test_direct_discovery(["ACDOCA", "MATDOC"], matching_mode="ANY", standard_only=False)
