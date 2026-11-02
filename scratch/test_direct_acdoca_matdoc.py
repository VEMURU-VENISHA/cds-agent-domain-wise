import sqlite3
import re
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "backend" / "data" / "sap_cds.db"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
c = conn.cursor()

def check_direct_table(tbl):
    print(f"\n================ Direct Candidates for {tbl} ================")
    # 1. From cds_views where parent_name = tbl
    c.execute("SELECT cds_name, source_code FROM cds_views WHERE UPPER(parent_name) = ?", (tbl,))
    from_views = {r["cds_name"]: r["source_code"] for r in c.fetchall()}
    
    # 2. From cds_joins where right_entity = tbl
    c.execute("SELECT cds_name FROM cds_joins WHERE UPPER(right_entity) = ?", (tbl,))
    join_views = {r["cds_name"] for r in c.fetchall()}
    
    # 3. From cds_sources where source_code has as select from tbl or join tbl
    c.execute("SELECT ddl_name, source_code FROM cds_sources WHERE UPPER(source_code) LIKE ? OR UPPER(source_code) LIKE ?", (f"%SELECT%FROM%{tbl}%", f"%JOIN%{tbl}%"))
    for r in c.fetchall():
        from_views.setdefault(r["ddl_name"], r["source_code"])
        
    all_names = set(from_views.keys()) | join_views
    print(f"Total candidate views found: {len(all_names)}")
    
    verified_direct = []
    
    for vname in all_names:
        # Get source code
        s_code = from_views.get(vname)
        if not s_code:
            row = c.execute("SELECT source_code FROM cds_sources WHERE UPPER(ddl_name) = ?", (vname.upper(),)).fetchone()
            if row and row["source_code"]:
                s_code = row["source_code"]
        if not s_code:
            row = c.execute("SELECT source_code FROM cds_views WHERE UPPER(cds_name) = ?", (vname.upper(),)).fetchone()
            if row and row["source_code"]:
                s_code = row["source_code"]
                
        if not s_code:
            continue
            
        # Strip comments
        clean = re.sub(r"/\*[\s\S]*?\*/", "", s_code)
        clean = re.sub(r"//.*$", "", clean, flags=re.MULTILINE)
        clean = re.sub(r"--.*$", "", clean, flags=re.MULTILINE)
        
        # Check direct FROM
        m_from = re.findall(r"\bas\s+select\s+from\s+([A-Za-z0-9_\/]+)", clean, re.IGNORECASE)
        # Check direct JOIN
        m_join = re.findall(r"\bjoin\s+([A-Za-z0-9_\/]+)", clean, re.IGNORECASE)
        
        direct_from = [x.upper() for x in m_from]
        direct_join = [x.upper() for x in m_join]
        
        has_direct_table = (tbl.upper() in direct_from) or (tbl.upper() in direct_join)
        if not has_direct_table:
            continue
            
        # Check extraction
        m_dot = re.search(r"@Analytics\.dataExtraction\.enabled\s*:\s*true\b", clean, re.IGNORECASE)
        m_block = re.search(r"@Analytics\s*:\s*\{[^\}]*\bdataExtraction\.enabled\s*:\s*true\b", clean, re.IGNORECASE)
        m_nested = re.search(r"@Analytics\s*:\s*\{[\s\S]*?dataExtraction\s*:\s*\{[\s\S]*?\benabled\s*:\s*true\b", clean, re.IGNORECASE)
        is_ext = bool(m_dot or m_block or m_nested)
        
        # Check CDC
        cdc_match = re.search(r"@Analytics(?:\.dataExtraction\.delta\.changeDataCapture|\s*:\s*\{[\s\S]*?changeDataCapture)", clean, re.IGNORECASE)
        is_cdc = bool(cdc_match)
        
        # Check standard
        is_std = not vname.upper().startswith(("Z", "Y", "/Z", "/Y"))
        
        if is_ext:
            verified_direct.append({
                "name": vname,
                "is_standard": is_std,
                "from": direct_from,
                "join": direct_join,
                "cdc": is_cdc
            })
            
    print(f"Extraction-enabled direct candidates for {tbl}: {len(verified_direct)}")
    for vd in verified_direct:
        print(f"  - {vd['name']} (Standard: {vd['is_standard']}) | FROM: {vd['from']} | JOIN: {vd['join']} | CDC: {vd['cdc']}")

check_direct_table("ACDOCA")
check_direct_table("MATDOC")
check_direct_table("MARA")
