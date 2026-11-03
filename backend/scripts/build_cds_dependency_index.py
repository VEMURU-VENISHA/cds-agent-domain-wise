"""
Safe local CDS dependency-index builder.

Run from the backend directory:
    py -3.12 scripts\build_cds_dependency_index.py

This script:
- backs up the SQLite database before changing its schema;
- preserves legacy cds_sources / cds_dependencies tables by renaming them;
- creates the schema expected by TableDiscoveryService;
- copies CDS source text from cds_views into cds_sources;
- parses basic FROM, JOIN, ASSOCIATION TO and UNION FROM references;
- labels parsed edges PARSED_PENDING_VERIFICATION (never VERIFIED);
- is designed to be re-runnable.

It does not call SAP, alter SAP objects, or overwrite the database with a seed.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "data" / "sap_cds.db"
KNOWN_TABLES = {
    "MARA", "MAKT", "MARC", "MARD", "MBEW", "MVKE", "MLGN", "MLGT",
    "VBAK", "VBAP", "VBKD", "VBRK", "VBRP", "LIKP", "LIPS", "VBPA",
    "VBEP", "ACDOCA", "BKPF", "BSEG", "BSIS", "BSAS", "BSIK", "BSAK",
    "BSID", "BSAD", "KNA1", "KNVV", "KNB1", "LFA1", "LFB1", "LFM1",
    "BUT000", "BUT020", "EKKO", "EKPO", "EKET", "EKKN", "MATDOC",
    "MKPF", "MSEG", "T001", "T001K", "T001W", "T005", "T005T",
    "CSKS", "CRHD", "AFKO", "AFPO", "AUFK", "COEP", "SKA1", "SKB1",
    "PRPS",
}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1].lower() for row in conn.execute(f'PRAGMA table_info("{table}")')}

def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

def unique_archive_name(conn: sqlite3.Connection, base: str) -> str:
    name = base
    suffix = 1
    while table_exists(conn, name):
        name = f"{base}_{suffix}"
        suffix += 1
    return name

def backup_database() -> Path:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DB_PATH.with_name(f"{DB_PATH.stem}_before_dependency_index_{stamp}{DB_PATH.suffix}")
    shutil.copy2(DB_PATH, backup)
    # Include any committed WAL content in the backup using SQLite's backup API.
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup

def create_schema(conn: sqlite3.Connection) -> None:
    # Archive incompatible legacy tables instead of dropping or destructively altering them.
    source_cols = table_columns(conn, "cds_sources") if table_exists(conn, "cds_sources") else set()
    if source_cols and "ddl_name" not in source_cols:
        archive = unique_archive_name(conn, "cds_sources_legacy")
        conn.execute(f'ALTER TABLE cds_sources RENAME TO "{archive}"')
        print(f"Preserved old cds_sources as {archive}")

    dep_cols = table_columns(conn, "cds_dependencies") if table_exists(conn, "cds_dependencies") else set()
    required_dep = {"source_ddl_name", "target_object_name", "dependency_type"}
    if dep_cols and not required_dep.issubset(dep_cols):
        archive = unique_archive_name(conn, "cds_dependencies_legacy")
        conn.execute(f'ALTER TABLE cds_dependencies RENAME TO "{archive}"')
        print(f"Preserved old cds_dependencies as {archive}")

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cds_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ddl_name TEXT NOT NULL UNIQUE,
        source_code TEXT,
        source_hash TEXT,
        source_status TEXT DEFAULT 'LOCAL_SOURCE',
        retrieved_at TEXT,
        last_checked_at TEXT
    );

    CREATE TABLE IF NOT EXISTS cds_dependencies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_ddl_name TEXT NOT NULL,
        target_object_name TEXT NOT NULL,
        target_object_type TEXT DEFAULT 'UNKNOWN',
        dependency_type TEXT NOT NULL,
        source_evidence TEXT,
        verification_status TEXT DEFAULT 'PARSED_PENDING_VERIFICATION',
        source_hash TEXT,
        parent_cds TEXT,
        child_cds TEXT,
        UNIQUE(source_ddl_name, target_object_name, dependency_type, source_hash)
    );

    CREATE TABLE IF NOT EXISTS cds_annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ddl_name TEXT NOT NULL,
        annotation_name TEXT NOT NULL,
        annotation_value TEXT,
        source_evidence TEXT,
        verification_status TEXT DEFAULT 'PARSED_PENDING_VERIFICATION',
        source_hash TEXT,
        UNIQUE(ddl_name, annotation_name)
    );

    CREATE TABLE IF NOT EXISTS table_discovery_runs (
        run_id TEXT PRIMARY KEY,
        matching_mode TEXT NOT NULL,
        execution_status TEXT NOT NULL,
        created_at TEXT,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS table_discovery_inputs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        validation_status TEXT,
        FOREIGN KEY(run_id) REFERENCES table_discovery_runs(run_id)
    );

    CREATE TABLE IF NOT EXISTS table_discovery_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        ddl_name TEXT NOT NULL,
        extraction_status TEXT,
        cdc_status TEXT,
        verification_status TEXT,
        dependency_classification TEXT,
        dependency_depth INTEGER,
        direct_from_source INTEGER,
        sql_view_name TEXT,
        requested_table TEXT,
        complete_dependency_path TEXT,
        UNIQUE(run_id, ddl_name),
        FOREIGN KEY(run_id) REFERENCES table_discovery_runs(run_id)
    );

    CREATE TABLE IF NOT EXISTS table_discovery_paths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        ddl_name TEXT NOT NULL,
        dependency_path TEXT,
        dependency_type TEXT,
        dependency_depth INTEGER,
        source_evidence TEXT,
        dependency_classification TEXT,
        direct_from_source INTEGER,
        FOREIGN KEY(run_id) REFERENCES table_discovery_runs(run_id)
    );

    CREATE TABLE IF NOT EXISTS sap_extraction_catalog (
        view_name TEXT PRIMARY KEY,
        description TEXT,
        is_sap_released INTEGER,
        delta_cdc_supported INTEGER,
        last_synced TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_cds_sources_ddl ON cds_sources(ddl_name);
    CREATE INDEX IF NOT EXISTS idx_cds_dependencies_source ON cds_dependencies(source_ddl_name);
    CREATE INDEX IF NOT EXISTS idx_cds_dependencies_target ON cds_dependencies(target_object_name);
    CREATE INDEX IF NOT EXISTS idx_cds_dependencies_type ON cds_dependencies(dependency_type);
    CREATE INDEX IF NOT EXISTS idx_cds_annotations_name ON cds_annotations(ddl_name);
    CREATE INDEX IF NOT EXISTS idx_discovery_results_run ON table_discovery_results(run_id);
    CREATE INDEX IF NOT EXISTS idx_discovery_paths_run ON table_discovery_paths(run_id);
    """)

def copy_legacy_dependencies(conn: sqlite3.Connection) -> int:
    # Import only if an archived legacy table exists and new index is empty.
    new_count = conn.execute("SELECT COUNT(*) FROM cds_dependencies").fetchone()[0]
    if new_count:
        return 0
    archives = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cds_dependencies_legacy%'"
        )
    ]
    imported = 0
    for archive in archives:
        cols = table_columns(conn, archive)
        if not {"parent_cds", "child_cds", "dependency_type"}.issubset(cols):
            continue
        rows = conn.execute(
            f'SELECT parent_cds, child_cds, dependency_type, '
            f'{ "source_evidence" if "source_evidence" in cols else "NULL" } '
            f'FROM "{archive}"'
        ).fetchall()
        for parent, child, old_type, evidence in rows:
            if not parent or not child:
                continue
            old_type = (old_type or "").upper()
            mapped = {
                "SELECT_FROM": "SQL_SOURCE_DEPENDENCY",
                "JOIN": "JOIN_DEPENDENCY",
                "ASSOCIATION": "ASSOCIATION_DEPENDENCY",
                "UNION": "UNION_DEPENDENCY",
            }.get(old_type, old_type or "LEGACY_DEPENDENCY")
            conn.execute("""
                INSERT OR IGNORE INTO cds_dependencies
                (source_ddl_name, target_object_name, target_object_type,
                 dependency_type, source_evidence, verification_status,
                 parent_cds, child_cds)
                VALUES (?, ?, 'UNKNOWN', ?, ?, 'LEGACY_IMPORTED', ?, ?)
            """, (str(parent).upper(), str(child).upper(), mapped, evidence, parent, child))
            imported += conn.execute("SELECT changes()").fetchone()[0]
    return imported

def strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"(?m)^\s*\*.*?$", " ", source)
    source = re.sub(r"(?m)--.*?$", " ", source)
    return source

def classify_target(conn: sqlite3.Connection, target: str) -> str:
    name = target.upper()
    if name in KNOWN_TABLES:
        return "TRANSPARENT_TABLE"
    if conn.execute("SELECT 1 FROM cds_sources WHERE UPPER(ddl_name)=? LIMIT 1", (name,)).fetchone():
        return "CDS_VIEW"
    view_name_col = "cds_name" if "cds_name" in table_columns(conn, "cds_views") else "ddl_name"
    if conn.execute(
        f'SELECT 1 FROM cds_views WHERE UPPER("{view_name_col}")=? LIMIT 1', (name,)
    ).fetchone():
        return "CDS_VIEW"
    return "UNKNOWN"

def parse_edges(source: str) -> list[tuple[str, str]]:
    text = strip_comments(source)
    found: list[tuple[str, str]] = []
    # Main SELECT source and JOIN targets. Keep the evidence as a short source fragment.
    patterns = [
        (r"\bAS\s+SELECT\s+FROM\s+([/A-Za-z_][A-Za-z0-9_/$]*)", "SQL_SOURCE_DEPENDENCY"),
        (r"\bJOIN\s+([/A-Za-z_][A-Za-z0-9_/$]*)", "JOIN_DEPENDENCY"),
        (r"\bASSOCIATION\s+TO\s+([/A-Za-z_][A-Za-z0-9_/$]*)", "ASSOCIATION_DEPENDENCY"),
        (r"\bUNION(?:\s+DISTINCT|\s+ALL)?\s+SELECT\s+FROM\s+([/A-Za-z_][A-Za-z0-9_/$]*)", "UNION_DEPENDENCY"),
    ]
    for pattern, dep_type in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            target = match.group(1).strip().rstrip(".")
            if target:
                start = max(0, match.start() - 70)
                end = min(len(text), match.end() + 100)
                evidence = re.sub(r"\s+", " ", text[start:end]).strip()
                found.append((target, dep_type + "\t" + evidence))
    # De-duplicate identical references from the same source text.
    seen = set()
    unique = []
    for item in found:
        key = (item[0].upper(), item[1].split("\t", 1)[0])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique

def main() -> int:
    print(f"Backend: {BACKEND_DIR}")
    print(f"Database: {DB_PATH}")
    backup = backup_database()
    print(f"Backup created: {backup}")

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        create_schema(conn)
        imported = copy_legacy_dependencies(conn)

        view_cols = table_columns(conn, "cds_views")
        # This database's actual schema uses cds_name as the CDS object key.
        # Support both cds_name (current database) and ddl_name (alternate schemas).
        view_name_col = "cds_name" if "cds_name" in view_cols else (
            "ddl_name" if "ddl_name" in view_cols else None
        )
        if not view_name_col or "source_code" not in view_cols:
            raise RuntimeError(
                "Could not identify the CDS name column and source_code in cds_views. "
                f"Available columns: {sorted(view_cols)}"
            )

        rows = conn.execute(f"""
            SELECT "{view_name_col}" AS ddl_name, source_code
            FROM cds_views
            WHERE source_code IS NOT NULL AND TRIM(source_code) <> ''
        """).fetchall()

        source_inserted = 0
        for row in rows:
            ddl_name = str(row["ddl_name"]).strip()
            source = str(row["source_code"])
            if not ddl_name:
                continue
            digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()
            conn.execute("""
                INSERT INTO cds_sources
                (ddl_name, source_code, source_hash, source_status, retrieved_at, last_checked_at)
                VALUES (?, ?, ?, 'LOCAL_SOURCE', ?, ?)
                ON CONFLICT(ddl_name) DO UPDATE SET
                    source_code=excluded.source_code,
                    source_hash=excluded.source_hash,
                    source_status='LOCAL_SOURCE',
                    last_checked_at=excluded.last_checked_at
                WHERE cds_sources.source_hash IS NULL
                   OR cds_sources.source_hash <> excluded.source_hash
            """, (ddl_name, source, digest, utc_now(), utc_now()))
            source_inserted += conn.execute("SELECT changes()").fetchone()[0]

        # Parse every available source in the local source index.
        sources = conn.execute("""
            SELECT ddl_name, source_code, source_hash
            FROM cds_sources
            WHERE source_code IS NOT NULL AND TRIM(source_code) <> ''
        """).fetchall()

        edges_added = 0
        annotations_added = 0
        for row in sources:
            ddl_name = str(row["ddl_name"]).strip()
            source = str(row["source_code"])
            digest = row["source_hash"] or hashlib.sha256(source.encode()).hexdigest()

            for target, packed in parse_edges(source):
                dep_type, evidence = packed.split("\t", 1)
                target_type = classify_target(conn, target)
                conn.execute("""
                    INSERT OR IGNORE INTO cds_dependencies
                    (source_ddl_name, target_object_name, target_object_type,
                     dependency_type, source_evidence, verification_status,
                     source_hash, parent_cds, child_cds)
                    VALUES (?, ?, ?, ?, ?, 'PARSED_PENDING_VERIFICATION', ?, ?, ?)
                """, (ddl_name.upper(), target.upper(), target_type, dep_type,
                      evidence, digest, ddl_name, target))
                edges_added += conn.execute("SELECT changes()").fetchone()[0]

            annotation_patterns = [
                (r"@Analytics\.dataExtraction\.enabled\s*:\s*(true|false)", "@Analytics.dataExtraction.enabled"),
                (r"@Analytics\.dataExtraction\.delta\.changeDataCapture\.automatic\s*:\s*(true|false)", "@Analytics.dataExtraction.delta.changeDataCapture.automatic"),
                (r"@AbapCatalog\.sqlViewName\s*:\s*'([^']+)'", "@AbapCatalog.sqlViewName"),
            ]
            for pattern, annotation_name in annotation_patterns:
                for match in re.finditer(pattern, strip_comments(source), flags=re.I):
                    value = match.group(1)
                    evidence = match.group(0)
                    conn.execute("""
                        INSERT INTO cds_annotations
                        (ddl_name, annotation_name, annotation_value, source_evidence,
                         verification_status, source_hash)
                        VALUES (?, ?, ?, ?, 'PARSED_PENDING_VERIFICATION', ?)
                        ON CONFLICT(ddl_name, annotation_name) DO UPDATE SET
                            annotation_value=excluded.annotation_value,
                            source_evidence=excluded.source_evidence,
                            verification_status='PARSED_PENDING_VERIFICATION',
                            source_hash=excluded.source_hash
                    """, (ddl_name, annotation_name, value, evidence, digest))
                    annotations_added += conn.execute("SELECT changes()").fetchone()[0]

        conn.commit()

        print("\nINDEX BUILD COMPLETE")
        print(f"CDS source rows processed: {len(rows)}")
        print(f"CDS source rows inserted/updated: {source_inserted}")
        print(f"Legacy dependency rows imported: {imported}")
        print(f"New dependency edges inserted: {edges_added}")
        print(f"Annotation rows inserted/updated: {annotations_added}")
        print("Verification status: PARSED_PENDING_VERIFICATION (not authoritative verification)")
        for table in ("cds_views", "cds_sources", "cds_dependencies", "cds_annotations"):
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            print(f"{table}: {count:,}")
        print("\nNo SAP calls or SAP write operations were performed.")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("The database backup was created before schema changes.", file=sys.stderr)
        raise
