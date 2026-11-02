"""Table-to-CDS Extraction Discovery Service.

Discovers, analyzes, and verifies CDS views directly and indirectly dependent on one
or multiple transparent SAP database tables (e.g. MARA, MARC, MARD, VBAK, VBAP, ACDOCA, BKPF).

Capabilities:
1. Input normalization and transparent table dictionary validation.
2. Recursive dependency graph traversal (FROM, JOIN, ASSOCIATION, UNION) with cycle prevention.
3. Actual CDS DDL source code retrieval and storage in SQLite (cds_sources).
4. Strict extraction verification: Positively verified @Analytics.dataExtraction.enabled: true
   and authoritative cross-check against SAP I_DataExtractionEnabledView.
5. Separate Change Data Capture (CDC) status verification.
6. Multi-table matching modes:
   - MODE A: ANY TABLE (at least one input table)
   - MODE B: ALL TABLES (must cover all input tables)
7. Strict deduplication: Exactly one recommendation row per unique CDS DDL name.
8. Complete discovery run and path persistence in SQLite (table_discovery_* tables).
"""

import hashlib
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from uuid import uuid4

from src.config import Settings, get_settings
from src.logging_config import logger
from src.sap.adt_client import SAPADTClient


class TableDiscoveryService:
    """Service for Table-to-CDS extraction discovery, source retrieval, and SQLite storage."""

    # Authoritative transparent SAP database tables catalogue with standard business descriptions
    KNOWN_TRANSPARENT_TABLES: Dict[str, str] = {
        "MARA": "General Material Data",
        "MAKT": "Material Descriptions",
        "MARC": "Plant Data for Material",
        "MARD": "Storage Location Data for Material",
        "MBEW": "Material Valuation",
        "MVKE": "Sales Data for Material",
        "MLGN": "Material Data for Warehouse Number",
        "MLGT": "Material Data for Storage Type",
        "VBAK": "Sales Document Header Data",
        "VBAP": "Sales Document Item Data",
        "VBKD": "Sales District & Customer Business Data",
        "VBRK": "Billing Document Header",
        "VBRP": "Billing Document Item",
        "LIKP": "SD Document: Delivery Header Data",
        "LIPS": "SD Document: Delivery Item Data",
        "VBPA": "Sales Document: Partner",
        "VBEP": "Sales Document: Schedule Line Data",
        "ACDOCA": "Universal Journal Entry Line Items",
        "BKPF": "Accounting Document Header",
        "BSEG": "Accounting Document Segment",
        "BSIS": "Accounting: Secondary Index for G/L Accounts",
        "BSAS": "Accounting: Secondary Index for G/L Accounts (Cleared Items)",
        "BSIK": "Accounting: Secondary Index for Vendors",
        "BSAK": "Accounting: Secondary Index for Vendors (Cleared Items)",
        "BSID": "Accounting: Secondary Index for Customers",
        "BSAD": "Accounting: Secondary Index for Customers (Cleared Items)",
        "KNA1": "General Data in Customer Master",
        "KNVV": "Customer Master Sales Data",
        "KNB1": "Customer Master (Company Code)",
        "LFA1": "Vendor Master (General Section)",
        "LFB1": "Vendor Master (Company Code)",
        "LFM1": "Purchasing Organization Data (Vendor)",
        "BUT000": "BP: General Data I",
        "BUT020": "BP: Addresses",
        "EKKO": "Purchasing Document Header",
        "EKPO": "Purchasing Document Item",
        "EKET": "Purchasing Document: Delivery Schedules",
        "EKKN": "Account Assignment in Purchasing Document",
        "MATDOC": "Material Documents (Goods Movements)",
        "MKPF": "Material Document Header",
        "MSEG": "Material Document Segment",
        "T001": "Company Codes",
        "T001K": "Valuation Areas",
        "T001W": "Plants / Branches",
        "T005": "Countries",
        "T005T": "Country Names",
        "CSKS": "Cost Center Master Record",
        "CRHD": "Work Center Header",
        "AFKO": "Order Header Data PP Orders",
        "AFPO": "Order Item PP Orders",
        "AUFK": "Order Master Data",
        "COEP": "CO Object: Line Items (by Period)",
        "SKA1": "G/L Account Master (Chart of Accounts)",
        "SKB1": "G/L Account Master (Company Code)",
        "PRPS": "WBS (Work Breakdown Structure) Element Master Data",
    }

    def __init__(self, db_path: Optional[str] = None, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._sap_client: Optional[SAPADTClient] = None
        if db_path:
            self.db_path = db_path
        else:
            base_dir = Path(__file__).resolve().parent.parent.parent
            self.db_path = str(base_dir / "data" / "sap_cds.db")

        self._ensure_schema()

    async def aclose(self) -> None:
        """Closes the underlying SAP ADT client to avoid resource leaks."""
        if hasattr(self, "_sap_client") and self._sap_client is not None:
            await self._sap_client.aclose()
            self._sap_client = None

    async def __aenter__(self) -> "TableDiscoveryService":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    def _get_db(self) -> sqlite3.Connection:
        """Opens a connection to the SQLite database with row factory."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        """Creates or safely migrates all required SQLite tables."""
        with self._get_db() as conn:
            c = conn.cursor()

            # 1. cds_sources
            c.execute("""
                CREATE TABLE IF NOT EXISTS cds_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ddl_name TEXT UNIQUE NOT NULL,
                    source_code TEXT,
                    source_hash TEXT,
                    source_status TEXT,
                    retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_sources_ddl ON cds_sources(ddl_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_sources_upper ON cds_sources(UPPER(ddl_name))")

            # 2. cds_dependencies
            c.execute("""
                CREATE TABLE IF NOT EXISTS cds_dependencies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_ddl_name TEXT,
                    target_object_name TEXT,
                    target_object_type TEXT,
                    dependency_type TEXT,
                    source_evidence TEXT,
                    verification_status TEXT,
                    source_hash TEXT,
                    parent_cds TEXT,
                    child_cds TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_dep_source ON cds_dependencies(source_ddl_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_dep_target ON cds_dependencies(target_object_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_dep_target_upper ON cds_dependencies(UPPER(target_object_name))")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_dep_source_upper ON cds_dependencies(UPPER(source_ddl_name))")

            # 3. cds_annotations
            c.execute("""
                CREATE TABLE IF NOT EXISTS cds_annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ddl_name TEXT NOT NULL,
                    annotation_name TEXT NOT NULL,
                    annotation_value TEXT,
                    source_evidence TEXT,
                    verification_status TEXT,
                    source_hash TEXT,
                    UNIQUE(ddl_name, annotation_name)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_anno_ddl ON cds_annotations(ddl_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_anno_name ON cds_annotations(annotation_name)")

            # 4. table_discovery_runs
            c.execute("""
                CREATE TABLE IF NOT EXISTS table_discovery_runs (
                    run_id TEXT PRIMARY KEY,
                    matching_mode TEXT NOT NULL,
                    execution_status TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)

            # 5. table_discovery_inputs
            c.execute("""
                CREATE TABLE IF NOT EXISTS table_discovery_inputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    validation_status TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES table_discovery_runs(run_id) ON DELETE CASCADE
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_td_inputs_run ON table_discovery_inputs(run_id)")

            # 6. table_discovery_results
            c.execute("""
                CREATE TABLE IF NOT EXISTS table_discovery_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ddl_name TEXT NOT NULL,
                    extraction_status TEXT NOT NULL,
                    cdc_status TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    dependency_classification TEXT,
                    dependency_depth INTEGER,
                    direct_from_source TEXT,
                    sql_view_name TEXT,
                    requested_table TEXT,
                    complete_dependency_path TEXT,
                    UNIQUE(run_id, ddl_name),
                    FOREIGN KEY(run_id) REFERENCES table_discovery_runs(run_id) ON DELETE CASCADE
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_td_results_run ON table_discovery_results(run_id)")

            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_views_parent_upper ON cds_views(UPPER(parent_name))")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cds_joins_right_upper ON cds_joins(UPPER(right_entity))")

            # Add columns to table_discovery_results if table already existed without them
            res_cols = {row[1] for row in c.execute("PRAGMA table_info(table_discovery_results)").fetchall()}
            for col, ctype in [
                ("dependency_classification", "TEXT"),
                ("dependency_depth", "INTEGER"),
                ("direct_from_source", "TEXT"),
                ("direct_join_sources", "TEXT"),
                ("sql_view_name", "TEXT"),
                ("requested_table", "TEXT"),
                ("complete_dependency_path", "TEXT"),
            ]:
                if col not in res_cols:
                    try:
                        c.execute(f"ALTER TABLE table_discovery_results ADD COLUMN {col} {ctype}")
                    except Exception:
                        pass

            # 7. table_discovery_paths
            c.execute("""
                CREATE TABLE IF NOT EXISTS table_discovery_paths (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    ddl_name TEXT NOT NULL,
                    dependency_path TEXT NOT NULL,
                    dependency_type TEXT NOT NULL,
                    dependency_depth INTEGER NOT NULL,
                    source_evidence TEXT,
                    dependency_classification TEXT,
                    direct_from_source TEXT,
                    FOREIGN KEY(run_id) REFERENCES table_discovery_runs(run_id) ON DELETE CASCADE
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_td_paths_run ON table_discovery_paths(run_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_td_paths_lookup ON table_discovery_paths(run_id, ddl_name)")

            # Add columns to table_discovery_paths if table already existed without them
            path_cols = {row[1] for row in c.execute("PRAGMA table_info(table_discovery_paths)").fetchall()}
            for col, ctype in [
                ("dependency_classification", "TEXT"),
                ("direct_from_source", "TEXT"),
            ]:
                if col not in path_cols:
                    try:
                        c.execute(f"ALTER TABLE table_discovery_paths ADD COLUMN {col} {ctype}")
                    except Exception:
                        pass

            conn.commit()

    _INDEXED_TABLES: Set[str] = set()

    @staticmethod
    def compute_source_hash(source: Optional[str]) -> str:
        """Computes deterministic SHA-256 hash of normalized CDS source code."""
        if not source:
            return ""
        normalized = source.replace("\r\n", "\n").strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def parse_ddl_edges(
        source_code: str,
    ) -> Tuple[str, List[Tuple[str, str, str]], List[Tuple[str, str, str]]]:
        """Parses direct FROM entity, JOINs, UNIONs (data edges), and ASSOCIATIONs (lazy edges) from DDL source code.

        Returns:
            Tuple of:
            - direct_from: str (the main 'as select from <entity>')
            - data_edges: List of (target_entity, dependency_type, evidence_snippet) [FROM, JOIN, UNION]
            - assoc_edges: List of (target_entity, dependency_type, evidence_snippet) [ASSOCIATION]
        """
        if not source_code:
            return "", [], []

        # Strip comments
        clean = re.sub(r"/\*[\s\S]*?\*/", "", source_code)
        clean = re.sub(r"//.*$", "", clean, flags=re.MULTILINE)
        clean = re.sub(r"--.*$", "", clean, flags=re.MULTILINE)

        from_matches = re.findall(r"\bas\s+select\s+from\s+([A-Za-z0-9_\/]+)", clean, re.IGNORECASE)
        direct_from = from_matches[0].strip() if from_matches else ""

        data_edges: List[Tuple[str, str, str]] = []
        for f in from_matches:
            f_clean = f.strip()
            if f_clean.upper() not in ("SELECT", "FROM"):
                data_edges.append((f_clean, "SQL_SOURCE_DEPENDENCY", f"as select from {f_clean}"))

        # JOINs
        join_matches = re.finditer(
            r"\b(?:inner|left\s+outer|right\s+outer|cross)?\s*join\s+([A-Za-z0-9_\/]+)\b[\s\S]*?\bon\s+([^\n\r;]+)",
            clean,
            re.IGNORECASE,
        )
        for m in join_matches:
            target = m.group(1).strip()
            on_cl = m.group(2).strip()[:60]
            data_edges.append((target, "JOIN_DEPENDENCY", f"join {target} on {on_cl}"))

        # UNIONs
        union_matches = re.findall(r"\bunion\s+(?:all\s+)?select\s+from\s+([A-Za-z0-9_\/]+)", clean, re.IGNORECASE)
        for u in union_matches:
            data_edges.append((u.strip(), "UNION_DEPENDENCY", f"union select from {u.strip()}"))

        # ASSOCIATIONs
        assoc_edges: List[Tuple[str, str, str]] = []
        assoc_matches = re.finditer(
            r"\bassociation\s*(?:\[[^\]]*\])?\s*to\s+([A-Za-z0-9_\/]+)\s+as\s+([A-Za-z0-9_\/]+)",
            clean,
            re.IGNORECASE,
        )
        for m in assoc_matches:
            target = m.group(1).strip()
            alias = m.group(2).strip()
            assoc_edges.append((target, "ASSOCIATION_DEPENDENCY", f"association to {target} as {alias}"))

        return direct_from, data_edges, assoc_edges

    def _ensure_table_dependencies_indexed(self, tables: List[str]) -> None:
        """Ensures that dependencies for transparent tables and extraction catalog views
        are fully indexed into cds_dependencies with cycle-safe graph edges.
        Uses high-performance batch insertion.
        """
        to_index = [t.upper() for t in tables if t.upper() not in TableDiscoveryService._INDEXED_TABLES]
        if not to_index:
            return

        with self._get_db() as conn:
            c = conn.cursor()

            # 1. Ensure extraction views are indexed into cds_dependencies
            c.execute("SELECT COUNT(*) FROM cds_dependencies")
            current_dep_count = c.fetchone()[0]
            if current_dep_count < 70000:
                rows = c.execute("""
                    SELECT ddl_name, source_code, source_hash 
                    FROM cds_sources 
                    WHERE UPPER(ddl_name) IN (SELECT UPPER(view_name) FROM sap_extraction_catalog)
                """).fetchall()
                ext_edges = []
                for ddl_name, src, shash in rows:
                    if not src:
                        continue
                    direct_from, data_edges, assoc_edges = self.parse_ddl_edges(src)
                    for tgt, etype, ev in (data_edges + assoc_edges):
                        ext_edges.append((
                            ddl_name, tgt,
                            "TABLE" if tgt.upper() in self.KNOWN_TRANSPARENT_TABLES else "CDS_VIEW",
                            etype, ev, "VERIFIED", shash or "", ddl_name, tgt
                        ))
                if ext_edges:
                    c.execute("BEGIN TRANSACTION")
                    c.executemany("""
                        INSERT INTO cds_dependencies 
                        (source_ddl_name, target_object_name, target_object_type, dependency_type, source_evidence, verification_status, source_hash, parent_cds, child_cds)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, ext_edges)
                    conn.commit()

            # 2. Index views referencing the requested tables (Level 1)
            for tbl in to_index:
                c.execute(
                    "SELECT ddl_name, source_code, source_hash FROM cds_sources WHERE UPPER(source_code) LIKE ?",
                    (f"%{tbl}%",),
                )
                rows = c.fetchall()
                tbl_edges = []
                for r in rows:
                    ddl_name = r["ddl_name"]
                    src = r["source_code"]
                    shash = r["source_hash"] or ""
                    if not src:
                        continue
                    direct_from, data_edges, assoc_edges = self.parse_ddl_edges(src)
                    for tgt, etype, ev in (data_edges + assoc_edges):
                        tbl_edges.append((
                            ddl_name, tgt,
                            "TABLE" if tgt.upper() in self.KNOWN_TRANSPARENT_TABLES else "CDS_VIEW",
                            etype, ev, "VERIFIED", shash, ddl_name, tgt
                        ))

                if tbl_edges:
                    c.execute("BEGIN TRANSACTION")
                    c.executemany("""
                        INSERT INTO cds_dependencies 
                        (source_ddl_name, target_object_name, target_object_type, dependency_type, source_evidence, verification_status, source_hash, parent_cds, child_cds)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, tbl_edges)
                    conn.commit()

                TableDiscoveryService._INDEXED_TABLES.add(tbl)

    @staticmethod
    def is_standard_cds(cds_name: str, package: Optional[str] = None) -> bool:
        """Determines if a CDS view is a genuine standard SAP-delivered view.

        Rules:
        - In SAP ABAP and S/4HANA, custom / customer-defined objects reside in the Z* or Y* namespace.
        - Any view beginning with 'Z' or 'Y' (or customer namespace '/Z', '/Y') is custom.
        - Standard SAP CDS views are delivered under SAP standard namespaces (e.g. 'I_', 'C_', 'R_', 'E_', 'P_')
          and belong to SAP standard packages (never starting with 'Z', 'Y', or '$TMP').
        """
        if not cds_name:
            return False
        name_up = cds_name.strip().upper()
        if name_up.startswith(("Z", "Y", "/Z", "/Y")):
            return False
        if package:
            pkg_up = package.strip().upper()
            if pkg_up.startswith(("Z", "Y", "$TMP", "/Z", "/Y")):
                return False
        return True

    def normalize_table_inputs(
        self, raw_input: Union[str, List[str]]
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """Normalizes and validates table inputs against transparent tables metadata.

        Supports:
        - Comma-separated: "MARA, MARC, MARD"
        - Space-separated: "MARA MARC MARD"
        - Newline-separated: "MARA\\nMARC\\nMARD"
        - Semicolons / 'and': "MARA and MARC; MARD"
        - Lists of strings: ["MARA", "MARC"]

        Returns:
            Tuple of (valid_tables, invalid_tables)
            invalid_tables contains {"table_name": str, "reason": str}
        """
        if isinstance(raw_input, (list, tuple, set)):
            text = " ".join(str(item) for item in raw_input)
        else:
            text = str(raw_input or "")

        # Replace 'and' with space and split on delimiters
        clean_text = re.sub(r"\band\b", " ", text, flags=re.IGNORECASE)
        tokens = [t.strip().upper() for t in re.split(r"[,;\s\n\r]+", clean_text) if t.strip()]

        # Deduplicate while preserving order
        unique_tokens: List[str] = []
        seen = set()
        for tok in tokens:
            if tok not in seen:
                seen.add(tok)
                unique_tokens.append(tok)

        valid_tables: List[str] = []
        invalid_tables: List[Dict[str, str]] = []

        with self._get_db() as conn:
            c = conn.cursor()
            for tbl in unique_tokens:
                # 1. Check known transparent tables map
                if tbl in self.KNOWN_TRANSPARENT_TABLES:
                    valid_tables.append(tbl)
                    continue

                # 2. Check if table appears as transparent parent or join target in database index
                c.execute(
                    "SELECT 1 FROM cds_views WHERE UPPER(parent_name) = ? LIMIT 1",
                    (tbl,),
                )
                if c.fetchone():
                    valid_tables.append(tbl)
                    continue

                c.execute(
                    "SELECT 1 FROM cds_joins WHERE UPPER(right_entity) = ? LIMIT 1",
                    (tbl,),
                )
                if c.fetchone():
                    valid_tables.append(tbl)
                    continue

                c.execute(
                    "SELECT 1 FROM cds_associations WHERE UPPER(target_entity) = ? LIMIT 1",
                    (tbl,),
                )
                if c.fetchone():
                    valid_tables.append(tbl)
                    continue

                # Invalid table
                invalid_tables.append({
                    "table_name": tbl,
                    "reason": f"'{tbl}' was not recognized as a valid transparent database table in the SAP dictionary catalog.",
                })

        return valid_tables, invalid_tables

    async def get_or_store_cds_source(
        self,
        cds_name: str,
        fetch_live: bool = True,
        sap_client: Optional[SAPADTClient] = None,
    ) -> Dict[str, Any]:
        """Retrieves complete CDS DDL source code, stores in cds_sources, and returns metadata."""
        clean_name = cds_name.strip()
        name_up = clean_name.upper()

        with self._get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT source_code, source_hash, source_status FROM cds_sources WHERE UPPER(ddl_name) = ?",
                (name_up,),
            )
            row = c.fetchone()
            if row and row["source_code"] and len(row["source_code"]) > 0:
                s_code = row["source_code"]
                s_hash = row["source_hash"] or self.compute_source_hash(s_code)
                return {
                    "ddl_name": clean_name,
                    "source_code": s_code,
                    "source_hash": s_hash,
                    "source_status": row["source_status"] or "CACHED_CATALOG",
                }

            # Check cds_views catalog
            c.execute(
                "SELECT source_code FROM cds_views WHERE UPPER(cds_name) = ?",
                (name_up,),
            )
            v_row = c.fetchone()
            if v_row and v_row["source_code"]:
                s_code = v_row["source_code"]
                s_hash = self.compute_source_hash(s_code)
                c.execute(
                    """
                    INSERT OR REPLACE INTO cds_sources 
                    (ddl_name, source_code, source_hash, source_status, retrieved_at, last_checked_at)
                    VALUES (?, ?, ?, 'CACHED_CATALOG', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (clean_name, s_code, s_hash),
                )
                conn.commit()
                return {
                    "ddl_name": clean_name,
                    "source_code": s_code,
                    "source_hash": s_hash,
                    "source_status": "CACHED_CATALOG",
                }

        # Attempt Live ADT retrieval if enabled
        if fetch_live and self.settings.SAP_BASE_URL:
            client = sap_client or self._sap_client
            own_client = False
            if client is None:
                client = SAPADTClient(self.settings)
                self._sap_client = client

            try:
                live_ddl = await client.get_ddl_source(clean_name)
                if live_ddl and live_ddl.strip():
                    s_hash = self.compute_source_hash(live_ddl)
                    with self._get_db() as conn:
                        c = conn.cursor()
                        c.execute(
                            """
                            INSERT OR REPLACE INTO cds_sources 
                            (ddl_name, source_code, source_hash, source_status, retrieved_at, last_checked_at)
                            VALUES (?, ?, ?, 'VERIFIED_LIVE_ADT', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            """,
                            (clean_name, live_ddl, s_hash),
                        )
                        conn.commit()
                    return {
                        "ddl_name": clean_name,
                        "source_code": live_ddl,
                        "source_hash": s_hash,
                        "source_status": "VERIFIED_LIVE_ADT",
                    }
            except Exception as e:
                logger.warning("Live ADT source retrieval for %s failed: %s", clean_name, e)

        return {
            "ddl_name": clean_name,
            "source_code": "",
            "source_hash": "",
            "source_status": "MISSING_SOURCE",
        }

    def parse_and_store_dependencies(
        self,
        cds_name: str,
        source_code: str,
        source_hash: str,
    ) -> List[Dict[str, Any]]:
        """Parses DDL source code into structured dependencies and annotations and persists them."""
        clean_name = cds_name.strip()
        if not source_code:
            return []

        # Strip comments
        clean_ddl = re.sub(r"/\*[\s\S]*?\*/", "", source_code)
        clean_ddl = re.sub(r"//.*$", "", clean_ddl, flags=re.MULTILINE)
        clean_ddl = re.sub(r"--.*$", "", clean_ddl, flags=re.MULTILINE)

        extracted_deps: List[Dict[str, Any]] = []

        # 1. SQL_SOURCE_DEPENDENCY: as select from <entity>
        from_matches = re.finditer(
            r"\bas\s+select\s+from\s+([A-Za-z0-9_\/]+)",
            clean_ddl,
            re.IGNORECASE,
        )
        for m in from_matches:
            target = m.group(1).strip()
            if target.upper() not in ("SELECT", "FROM"):
                extracted_deps.append({
                    "source_ddl_name": clean_name,
                    "target_object_name": target,
                    "target_object_type": "TABLE" if target in self.KNOWN_TRANSPARENT_TABLES else "CDS_VIEW",
                    "dependency_type": "SQL_SOURCE_DEPENDENCY",
                    "source_evidence": f"as select from {target}",
                    "verification_status": "VERIFIED",
                    "source_hash": source_hash,
                })

        # 2. JOIN_DEPENDENCY: join <entity>
        join_matches = re.finditer(
            r"\b(?:inner|left\s+outer|right\s+outer|cross)?\s*join\s+([A-Za-z0-9_\/]+)\b[\s\S]*?\bon\s+([^\n\r;]+)",
            clean_ddl,
            re.IGNORECASE,
        )
        for m in join_matches:
            target = m.group(1).strip()
            on_clause = m.group(2).strip()[:100]
            extracted_deps.append({
                "source_ddl_name": clean_name,
                "target_object_name": target,
                "target_object_type": "TABLE" if target in self.KNOWN_TRANSPARENT_TABLES else "CDS_VIEW",
                "dependency_type": "JOIN_DEPENDENCY",
                "source_evidence": f"join {target} on {on_clause}",
                "verification_status": "VERIFIED",
                "source_hash": source_hash,
            })

        # 3. ASSOCIATION_DEPENDENCY: association to <entity>
        assoc_matches = re.finditer(
            r"\bassociation\s*(?:\[[^\]]*\])?\s*to\s+([A-Za-z0-9_\/]+)\s+as\s+([A-Za-z0-9_\/]+)",
            clean_ddl,
            re.IGNORECASE,
        )
        for m in assoc_matches:
            target = m.group(1).strip()
            alias = m.group(2).strip()
            extracted_deps.append({
                "source_ddl_name": clean_name,
                "target_object_name": target,
                "target_object_type": "TABLE" if target in self.KNOWN_TRANSPARENT_TABLES else "CDS_VIEW",
                "dependency_type": "ASSOCIATION_DEPENDENCY",
                "source_evidence": f"association to {target} as {alias}",
                "verification_status": "VERIFIED",
                "source_hash": source_hash,
            })

        # 4. UNION_DEPENDENCY: union [all] select from <entity>
        union_matches = re.finditer(
            r"\bunion\s+(?:all\s+)?select\s+from\s+([A-Za-z0-9_\/]+)",
            clean_ddl,
            re.IGNORECASE,
        )
        for m in union_matches:
            target = m.group(1).strip()
            extracted_deps.append({
                "source_ddl_name": clean_name,
                "target_object_name": target,
                "target_object_type": "TABLE" if target in self.KNOWN_TRANSPARENT_TABLES else "CDS_VIEW",
                "dependency_type": "UNION_DEPENDENCY",
                "source_evidence": f"union select from {target}",
                "verification_status": "VERIFIED",
                "source_hash": source_hash,
            })

        # Persist extracted dependencies & annotations into database
        with self._get_db() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            c = conn.cursor()
            for dep in extracted_deps:
                c.execute(
                    """
                    INSERT INTO cds_dependencies 
                    (source_ddl_name, target_object_name, target_object_type, dependency_type, source_evidence, verification_status, source_hash, parent_cds, child_cds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dep["source_ddl_name"],
                        dep["target_object_name"],
                        dep["target_object_type"],
                        dep["dependency_type"],
                        dep["source_evidence"],
                        dep["verification_status"],
                        dep["source_hash"],
                        dep["source_ddl_name"],
                        dep["target_object_name"],
                    ),
                )

            # Extract & persist extraction annotations
            ext_match = re.search(r"@Analytics\.dataExtraction\.enabled\s*:\s*(true|false)", clean_ddl, re.IGNORECASE)
            if ext_match:
                c.execute(
                    """
                    INSERT OR REPLACE INTO cds_annotations 
                    (ddl_name, annotation_name, annotation_value, source_evidence, verification_status, source_hash)
                    VALUES (?, 'Analytics.dataExtraction.enabled', ?, ?, 'VERIFIED', ?)
                    """,
                    (clean_name, ext_match.group(1).lower(), ext_match.group(0), source_hash),
                )

            cdc_match = re.search(r"@Analytics\.dataExtraction\.delta\.changeDataCapture[^\n\r]*", clean_ddl, re.IGNORECASE)
            if cdc_match:
                c.execute(
                    """
                    INSERT OR REPLACE INTO cds_annotations 
                    (ddl_name, annotation_name, annotation_value, source_evidence, verification_status, source_hash)
                    VALUES (?, 'Analytics.dataExtraction.delta.changeDataCapture', 'true', ?, 'VERIFIED', ?)
                    """,
                    (clean_name, cdc_match.group(0), source_hash),
                )

            sql_view_match = re.search(r"@AbapCatalog\.sqlViewName\s*:\s*['\"]?([A-Za-z0-9_]+)['\"]?", clean_ddl, re.IGNORECASE)
            if sql_view_match:
                c.execute(
                    """
                    INSERT OR REPLACE INTO cds_annotations 
                    (ddl_name, annotation_name, annotation_value, source_evidence, verification_status, source_hash)
                    VALUES (?, 'AbapCatalog.sqlViewName', ?, ?, 'VERIFIED', ?)
                    """,
                    (clean_name, sql_view_match.group(1), sql_view_match.group(0), source_hash),
                )

            conn.commit()

        return extracted_deps

    def build_table_dependency_graph(
        self,
        table_name: str,
        max_depth: int = 4,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Traverses the recursive dependency graph starting from a transparent table using BFS.

        Traverses:
        - cds_views (AS SELECT FROM)
        - cds_dependencies (verified parsed dependencies: FROM, JOIN, ASSOCIATION, UNION)
        - cds_joins (both left and right entities)
        - cds_associations (target entities)
        """
        clean_tbl = table_name.strip().upper()
        view_paths: Dict[str, List[Dict[str, Any]]] = {}
        visited: Set[str] = set()

        # Queue contains: (current_entity, path_list, current_depth, dep_type, evidence)
        queue: List[Tuple[str, List[str], int, str, str]] = [
            (clean_tbl, [clean_tbl], 0, "TABLE", f"SAP Transparent Table {clean_tbl}")
        ]

        with self._get_db() as conn:
            c = conn.cursor()

            while queue:
                curr_entity, path, depth, dep_type, ev = queue.pop(0)
                if depth >= max_depth:
                    continue

                curr_up = curr_entity.upper()
                path_upper = {p.upper() for p in path}

                # Step 1: Query direct children from cds_views (AS SELECT FROM)
                c.execute(
                    """
                    SELECT cds_name, label, domain, source_type, package, vdm_view_type, is_parameterized 
                    FROM cds_views 
                    WHERE UPPER(parent_name) = ?
                    LIMIT 200
                    """,
                    (curr_up,),
                )
                for r in c.fetchall():
                    v_name = r["cds_name"]
                    v_up = v_name.upper()
                    if v_up in path_upper:
                        continue  # cycle prevention

                    new_path = path + [v_name]
                    new_depth = depth + 1
                    evidence = f"as select from {curr_entity}"

                    path_info = {
                        "path": new_path,
                        "path_str": " -> ".join(new_path),
                        "depth": new_depth,
                        "type": "SQL_SOURCE_DEPENDENCY",
                        "evidence": evidence,
                        "target_entity": curr_entity,
                    }
                    view_paths.setdefault(v_up, []).append(path_info)

                    if v_up not in visited:
                        visited.add(v_up)
                        queue.append((v_name, new_path, new_depth, "SQL_SOURCE_DEPENDENCY", evidence))

                # Step 2: Query parsed dependencies from cds_dependencies
                c.execute(
                    """
                    SELECT source_ddl_name, dependency_type, source_evidence 
                    FROM cds_dependencies 
                    WHERE UPPER(target_object_name) = ?
                    LIMIT 200
                    """,
                    (curr_up,),
                )
                for r in c.fetchall():
                    v_name = r["source_ddl_name"]
                    if not v_name:
                        continue
                    v_up = v_name.upper()
                    if v_up in path_upper:
                        continue

                    new_path = path + [v_name]
                    new_depth = depth + 1
                    d_type = r["dependency_type"] or "JOIN_DEPENDENCY"
                    evidence = r["source_evidence"] or f"references {curr_entity}"

                    path_info = {
                        "path": new_path,
                        "path_str": " -> ".join(new_path),
                        "depth": new_depth,
                        "type": d_type,
                        "evidence": evidence,
                        "target_entity": curr_entity,
                    }
                    view_paths.setdefault(v_up, []).append(path_info)

                    if v_up not in visited:
                        visited.add(v_up)
                        queue.append((v_name, new_path, new_depth, d_type, evidence))

                # Step 3: Query joins from cds_joins (both right and left entities)
                c.execute(
                    """
                    SELECT cds_name, left_entity, right_entity, join_type, on_condition_raw 
                    FROM cds_joins 
                    WHERE UPPER(right_entity) = ? OR UPPER(left_entity) = ?
                    LIMIT 100
                    """,
                    (curr_up, curr_up),
                )
                for r in c.fetchall():
                    v_name = r["cds_name"] or r["left_entity"]
                    if not v_name:
                        continue
                    v_up = v_name.upper()
                    if v_up in path_upper:
                        continue  # cycle prevention

                    new_path = path + [v_name]
                    new_depth = depth + 1
                    join_type = r["join_type"] or "JOIN"
                    evidence = f"{join_type} {curr_entity} on {r['on_condition_raw'] or ''}"

                    path_info = {
                        "path": new_path,
                        "path_str": " -> ".join(new_path),
                        "depth": new_depth,
                        "type": "JOIN_DEPENDENCY",
                        "evidence": evidence[:120],
                        "target_entity": curr_entity,
                    }
                    view_paths.setdefault(v_up, []).append(path_info)

                    if v_up not in visited:
                        visited.add(v_up)
                        queue.append((v_name, new_path, new_depth, "JOIN_DEPENDENCY", evidence))

                # Step 4: Query associations from cds_associations
                c.execute(
                    """
                    SELECT cds_name, target_entity, association_name, on_condition 
                    FROM cds_associations 
                    WHERE UPPER(target_entity) = ?
                    LIMIT 100
                    """,
                    (curr_up,),
                )
                for r in c.fetchall():
                    v_name = r["cds_name"]
                    if not v_name:
                        continue
                    v_up = v_name.upper()
                    if v_up in path_upper:
                        continue  # cycle prevention

                    new_path = path + [v_name]
                    new_depth = depth + 1
                    evidence = f"association to {curr_entity} as {r['association_name'] or ''}"

                    path_info = {
                        "path": new_path,
                        "path_str": " -> ".join(new_path),
                        "depth": new_depth,
                        "type": "ASSOCIATION_DEPENDENCY",
                        "evidence": evidence,
                        "target_entity": curr_entity,
                    }
                    view_paths.setdefault(v_up, []).append(path_info)

                    if v_up not in visited:
                        visited.add(v_up)
                        queue.append((v_name, new_path, new_depth, "ASSOCIATION_DEPENDENCY", evidence))

        return view_paths

    def verify_extraction_and_cdc(
        self,
        cds_name: str,
        source_code: Optional[str],
        authoritative_records: Dict[str, Any],
    ) -> Tuple[str, str, Optional[str], Dict[str, Any]]:
        """Verifies extraction and CDC status strictly and separately.

        Supports all 3 SAP annotation syntax variants:
        1. Dot notation: @Analytics.dataExtraction.enabled: true
        2. Flat block notation: @Analytics: { ... dataExtraction.enabled: true ... }
        3. Nested block notation: @Analytics: { ... dataExtraction: { ... enabled: true ... } ... }

        Returns:
            Tuple of:
            - extraction_status: EXTRACTION_ENABLED_VERIFIED, EXTRACTION_NOT_ENABLED, EXTRACTION_STATUS_UNVERIFIED
            - cdc_status: CDC_SUPPORTED, CDC_NOT_SUPPORTED
            - sql_view_name: Optional[str]
            - details_dict: evidence details
        """
        name_up = cds_name.strip().upper()
        rec = authoritative_records.get(name_up)

        is_ext_enabled = False
        ext_evidence = []
        is_cdc_supported = False
        cdc_evidence = []
        sql_view_name = None

        # 1. Check authoritative SAP metadata (I_DataExtractionEnabledView)
        if rec:
            is_ext_enabled = True
            ext_evidence.append(
                f"Authoritative SAP View I_DataExtractionEnabledView (Released: {rec.get('is_sap_released', False)})"
            )
            if rec.get("delta_cdc_supported"):
                is_cdc_supported = True
                cdc_evidence.append("Delta CDC confirmed in SAP I_DataExtractionEnabledView")

        # 2. Check actual DDL source code
        if source_code:
            # 1. Dot notation: @Analytics.dataExtraction.enabled: true
            m_dot = re.search(r"@Analytics\.dataExtraction\.enabled\s*:\s*true\b", source_code, re.IGNORECASE)
            # 2. Flat block: @Analytics: { ... dataExtraction.enabled: true ... }
            m_block = re.search(r"@Analytics\s*:\s*\{[^\}]*\bdataExtraction\.enabled\s*:\s*true\b", source_code, re.IGNORECASE)
            # 3. Nested block: @Analytics: { ... dataExtraction\s*:\s*\{[\s\S]*?\benabled\s*:\s*true\b", source_code, re.IGNORECASE)
            m_nested = re.search(r"@Analytics\s*:\s*\{[\s\S]*?dataExtraction\s*:\s*\{[\s\S]*?\benabled\s*:\s*true\b", source_code, re.IGNORECASE)

            if m_dot:
                is_ext_enabled = True
                ext_evidence.append(f"Positively verified '{m_dot.group(0).strip()}' in DDL source")
            elif m_block:
                is_ext_enabled = True
                ext_evidence.append("Positively verified '@Analytics: { dataExtraction.enabled: true }' in DDL source")
            elif m_nested:
                is_ext_enabled = True
                ext_evidence.append("Positively verified '@Analytics: { dataExtraction: { enabled: true } }' in DDL source")

            # Check CDC in source (including table mapping if declared)
            cdc_mapping = re.search(
                r"@Analytics(?:\.dataExtraction\.delta\.changeDataCapture\.mapping|\s*:\s*\{[\s\S]*?changeDataCapture\s*:\s*\{[\s\S]*?mapping)\s*:\s*\[[\s\S]*?table\s*:\s*['\"]?([A-Za-z0-9_]+)['\"]?",
                source_code,
                re.IGNORECASE,
            )
            cdc_general = re.search(
                r"@Analytics(?:\.dataExtraction\.delta\.changeDataCapture|\s*:\s*\{[\s\S]*?changeDataCapture)",
                source_code,
                re.IGNORECASE,
            )
            if cdc_mapping:
                is_cdc_supported = True
                mapped_tbl = cdc_mapping.group(1).strip()
                cdc_evidence.append(f"Change Data Capture declared in DDL with table mapping '{mapped_tbl}'")
            elif cdc_general:
                is_cdc_supported = True
                cdc_evidence.append("Change Data Capture declared in DDL source (@Analytics.dataExtraction.delta.changeDataCapture)")

            # Check SQL View Name
            sql_view_match = re.search(
                r"@AbapCatalog\.sqlViewName\s*:\s*['\"]?([A-Za-z0-9_]+)['\"]?",
                source_code,
                re.IGNORECASE,
            )
            if sql_view_match:
                sql_view_name = sql_view_match.group(1).strip()

        # Classify Extraction
        if is_ext_enabled:
            extraction_status = "EXTRACTION_ENABLED_VERIFIED"
        elif source_code and len(source_code) > 0:
            extraction_status = "EXTRACTION_NOT_ENABLED"
        else:
            extraction_status = "EXTRACTION_STATUS_UNVERIFIED"

        # Classify CDC
        cdc_status = "CDC_SUPPORTED" if is_cdc_supported else "CDC_NOT_SUPPORTED"

        details = {
            "is_extraction_enabled": is_ext_enabled,
            "extraction_evidence": " | ".join(ext_evidence) if ext_evidence else "Not extraction-enabled in SAP or DDL",
            "is_cdc_supported": is_cdc_supported,
            "cdc_evidence": " | ".join(cdc_evidence) if cdc_evidence else "Change Data Capture not supported",
            "sql_view_name": sql_view_name or "None (View Entity)",
        }

        return extraction_status, cdc_status, sql_view_name, details

    def verify_transitive_table_paths(
        self,
        cds_name: str,
        target_table: str,
        max_depth: int = 5,
        source_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Performs cycle-safe BFS traversal backward from candidate CDS view to target database table
        using verified DDL source code.

        Classifies dependency as:
        - DIRECT_TABLE_DEPENDENCY: CDS directly selects from/joins the transparent table (depth 1).
        - TRANSITIVE_CDS_DEPENDENCY: CDS depends on intermediate CDS views that reach the table (depth >= 2).
        - UNVERIFIED_DEPENDENCY: Cannot be verified back to target table (must be excluded from recommendations).
        """
        clean_name = cds_name.strip()
        name_up = clean_name.upper()
        target_up = target_table.strip().upper()

        src = source_code
        if not src:
            with self._get_db() as conn:
                c = conn.cursor()
                row = c.execute("SELECT source_code FROM cds_sources WHERE UPPER(ddl_name) = ?", (name_up,)).fetchone()
                if row and row["source_code"]:
                    src = row["source_code"]

        if not src:
            return {
                "classification": "UNVERIFIED_DEPENDENCY",
                "verified": False,
                "direct_from_source": "",
                "requested_database_table": target_up,
                "min_depth": 0,
                "primary_path": "",
                "primary_type": "UNVERIFIED",
                "paths": [],
                "source_verification_status": "MISSING_SOURCE",
            }

        start_direct_from, start_data, start_assoc = self.parse_ddl_edges(src)

        # BFS backward from cds_name to target_table using pure data flow edges (FROM, JOIN, UNION)
        # Queue item: (current_entity, path_so_far, depth, primary_type, evidence)
        queue: List[Tuple[str, List[str], int, str, str]] = [
            (clean_name, [clean_name], 0, "SQL_SOURCE_DEPENDENCY", "")
        ]
        visited: Set[str] = set()
        verified_paths: List[Dict[str, Any]] = []

        local_src_cache: Dict[str, Optional[str]] = {name_up: src}

        with self._get_db() as conn:
            c = conn.cursor()

            while queue:
                curr, path, depth, dtype, ev = queue.pop(0)
                if depth >= max_depth:
                    continue

                curr_up = curr.upper()
                if curr_up in visited:
                    continue
                visited.add(curr_up)

                curr_src = local_src_cache.get(curr_up)
                if curr_src is None:
                    row = c.execute("SELECT source_code FROM cds_sources WHERE UPPER(ddl_name) = ?", (curr_up,)).fetchone()
                    curr_src = row["source_code"] if row and row["source_code"] else None
                    local_src_cache[curr_up] = curr_src

                if not curr_src:
                    continue

                _, data_edges, _ = self.parse_ddl_edges(curr_src)

                for tgt, edge_type, edge_ev in data_edges:
                    tgt_up = tgt.upper()
                    if tgt_up == target_up:
                        # Target table reached!
                        full_path = [target_up] + list(reversed(path))
                        verified_paths.append({
                            "table_name": target_up,
                            "path": full_path,
                            "path_str": " -> ".join(full_path),
                            "depth": depth + 1,
                            "dependency_type": edge_type if depth == 0 else dtype,
                            "evidence": edge_ev,
                            "is_direct_source_chain": (dtype == "SQL_SOURCE_DEPENDENCY" and edge_type == "SQL_SOURCE_DEPENDENCY"),
                        })
                    else:
                        if tgt_up not in visited and tgt_up not in [p.upper() for p in path]:
                            next_dtype = edge_type if depth == 0 else dtype
                            queue.append((tgt, path + [tgt], depth + 1, next_dtype, edge_ev))

        # Phase 2: If no pure data chain reached table, check direct associations from start view
        if not verified_paths:
            for tgt, edge_type, edge_ev in start_assoc:
                if tgt.upper() == target_up:
                    full_path = [target_up, clean_name]
                    verified_paths.append({
                        "table_name": target_up,
                        "path": full_path,
                        "path_str": " -> ".join(full_path),
                        "depth": 1,
                        "dependency_type": edge_type,
                        "evidence": edge_ev,
                        "is_direct_source_chain": False,
                    })

        if not verified_paths:
            return {
                "classification": "UNVERIFIED_DEPENDENCY",
                "verified": False,
                "direct_from_source": start_direct_from,
                "requested_database_table": target_up,
                "min_depth": 0,
                "primary_path": "",
                "primary_type": "UNVERIFIED",
                "paths": [],
                "source_verification_status": "VERIFIED_FROM_DDL_SOURCE",
            }

        # Sort verified paths: pure SQL FROM/JOIN chains first, then shorter depth
        verified_paths.sort(key=lambda p: (not p.get("is_direct_source_chain", False), p["depth"], p["path_str"]))

        primary_path_obj = verified_paths[0]
        min_depth = min(p["depth"] for p in verified_paths)
        classification = "DIRECT_TABLE_DEPENDENCY" if min_depth == 1 else "TRANSITIVE_CDS_DEPENDENCY"

        return {
            "classification": classification,
            "verified": True,
            "direct_from_source": start_direct_from,
            "requested_database_table": target_up,
            "min_depth": min_depth,
            "primary_path": primary_path_obj["path_str"],
            "primary_type": primary_path_obj["dependency_type"],
            "paths": verified_paths,
            "source_verification_status": "VERIFIED_FROM_DDL_SOURCE",
        }

    _CLASS_EXTRACTION_RECORDS: Optional[Dict[str, Any]] = None
    _CLASS_EXTRACTION_TIMESTAMP: float = 0.0

    async def get_authoritative_extraction_metadata(
        self, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """Retrieves authoritative extraction metadata, leveraging sap_extraction_catalog in SQLite."""
        now = time.time()
        if not force_refresh and TableDiscoveryService._CLASS_EXTRACTION_RECORDS is not None:
            if (now - TableDiscoveryService._CLASS_EXTRACTION_TIMESTAMP) < 3600:
                return TableDiscoveryService._CLASS_EXTRACTION_RECORDS

        # Check SQLite sap_extraction_catalog
        with self._get_db() as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS sap_extraction_catalog (
                    view_name TEXT PRIMARY KEY,
                    description TEXT,
                    is_sap_released INTEGER,
                    delta_cdc_supported INTEGER,
                    last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("SELECT view_name, description, is_sap_released, delta_cdc_supported FROM sap_extraction_catalog")
            rows = c.fetchall()
            if rows and not force_refresh:
                records = {
                    r["view_name"].upper(): {
                        "view_name": r["view_name"],
                        "description": r["description"],
                        "is_sap_released": bool(r["is_sap_released"]),
                        "delta_cdc_supported": bool(r["delta_cdc_supported"]),
                        "extraction_status": "VERIFIED_ENABLED",
                    }
                    for r in rows
                }
                meta_res = {
                    "success": True,
                    "is_complete": True,
                    "records_count": len(records),
                    "records": records,
                    "source": "sap_extraction_catalog (SQLite)",
                }
                TableDiscoveryService._CLASS_EXTRACTION_RECORDS = meta_res
                TableDiscoveryService._CLASS_EXTRACTION_TIMESTAMP = now
                return meta_res

        # If empty or force_refresh requested, fetch from live ADT
        from src.services.discovery_service import CDSDiscoveryService
        discovery_svc = CDSDiscoveryService()
        meta_res = await discovery_svc.get_authoritative_extraction_metadata(force_refresh=force_refresh)
        records = meta_res.get("records", {})
        if records:
            with self._get_db() as conn:
                c = conn.cursor()
                for vname, rec in records.items():
                    c.execute("""
                        INSERT OR REPLACE INTO sap_extraction_catalog 
                        (view_name, description, is_sap_released, delta_cdc_supported, last_synced)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (
                        vname,
                        rec.get("description", ""),
                        1 if rec.get("is_sap_released") else 0,
                        1 if rec.get("delta_cdc_supported") else 0,
                    ))
                conn.commit()

        TableDiscoveryService._CLASS_EXTRACTION_RECORDS = meta_res
        TableDiscoveryService._CLASS_EXTRACTION_TIMESTAMP = now
        return meta_res

    async def run_discovery(
        self,
        tables_input: Union[str, List[str]],
        matching_mode: str = "ANY",
        max_depth: int = 4,
        force_refresh_ext: bool = False,
        standard_only: bool = True,
    ) -> Dict[str, Any]:
        """Executes complete Table-to-CDS extraction discovery across single or multiple tables.

        Matching Modes:
        - "ANY" (or "ANY_TABLE"): Recommends extraction-enabled CDS views dependent on at least ONE input table.
        - "ALL" (or "ALL_TABLES"): Recommends extraction-enabled CDS views dependent on ALL input tables.

        Strict Rules:
        - Deduplicate by CDS DDL name (exactly one row per unique DDL name).
        - Primary recommendation contains ONLY EXTRACTION_ENABLED_VERIFIED views.
        - CDC status verified separately.
        - Full persistence to SQLite table_discovery_* tables.
        """
        start_time = time.time()
        run_id = f"td_run_{uuid4().hex[:12]}"
        mode_clean = "ALL_TABLES" if matching_mode.upper() in ("ALL", "ALL_TABLES", "MODE_B") else "ANY_TABLE"

        # 1. Normalize and validate inputs
        valid_tables, invalid_tables = self.normalize_table_inputs(tables_input)

        if not valid_tables:
            elapsed_ms = round((time.time() - start_time) * 1000, 1)
            return {
                "run_id": run_id,
                "table_name": ", ".join([t["table_name"] for t in invalid_tables]) if invalid_tables else "",
                "matching_mode": mode_clean,
                "input_tables": [t["table_name"] for t in invalid_tables],
                "valid_tables": [],
                "invalid_tables": invalid_tables,
                "recommendations": [],
                "summary_metrics": {
                    "input_tables_count": len(invalid_tables),
                    "valid_tables_count": 0,
                    "invalid_tables_count": len(invalid_tables),
                    "total_discovered_candidates": 0,
                    "unique_extraction_recommended": 0,
                    "duplicates_removed": 0,
                    "unverified_excluded": 0,
                    "execution_time_ms": elapsed_ms,
                },
                "total_matches": 0,
                "sources": [],
                "joins": [],
                "associations": [],
                "combined_views": [],
            }

        # 2. Retrieve authoritative extraction metadata (cached / ADT)
        ext_meta = await self.get_authoritative_extraction_metadata(force_refresh=force_refresh_ext)
        ext_records = ext_meta.get("records", {})

        # 3. DIRECT DATABASE TABLE DISCOVERY ONLY
        # Strictly find candidate views that directly reference the requested table in FROM or JOIN.
        # No recursive BFS, no multi-hop graph traversal, no indirect associations.
        direct_candidate_names: Set[str] = set()
        non_standard_excluded = 0
        excluded_count = 0

        with self._get_db() as conn:
            c = conn.cursor()
            for tbl in valid_tables:
                tbl_up = tbl.upper()
                # 1. Main FROM source in cds_views
                c.execute("SELECT cds_name FROM cds_views WHERE UPPER(parent_name) = ?", (tbl_up,))
                for r in c.fetchall():
                    direct_candidate_names.add(r[0])

                # 2. Direct JOIN in cds_joins
                c.execute("SELECT cds_name FROM cds_joins WHERE UPPER(right_entity) = ?", (tbl_up,))
                for r in c.fetchall():
                    direct_candidate_names.add(r[0])

                # 3. Direct parsed dependencies in cds_dependencies (FROM or JOIN only)
                c.execute("""
                    SELECT source_ddl_name FROM cds_dependencies 
                    WHERE UPPER(target_object_name) = ? 
                      AND dependency_type IN ('SQL_SOURCE_DEPENDENCY', 'JOIN_DEPENDENCY')
                """, (tbl_up,))
                for r in c.fetchall():
                    direct_candidate_names.add(r[0])

        total_discovered = len(direct_candidate_names)

        # Filter standard if requested
        if standard_only:
            filtered_candidates = set()
            for v in direct_candidate_names:
                if self.is_standard_cds(v):
                    filtered_candidates.add(v)
                else:
                    non_standard_excluded += 1
            direct_candidate_names = filtered_candidates

        # 4. Batch retrieve source code and metadata for candidates
        sources_map: Dict[str, Dict[str, Any]] = {}
        meta_map: Dict[str, Dict[str, Any]] = {}

        if direct_candidate_names:
            c_list = list(direct_candidate_names)
            with self._get_db() as conn:
                c = conn.cursor()
                for i in range(0, len(c_list), 500):
                    chunk = c_list[i : i + 500]
                    ph = ",".join(["?"] * len(chunk))

                    # Sources from cds_sources
                    c.execute(
                        f"SELECT ddl_name, source_code, source_hash, source_status FROM cds_sources WHERE UPPER(ddl_name) IN ({ph})",
                        [v.upper() for v in chunk],
                    )
                    for r in c.fetchall():
                        sources_map[r["ddl_name"].upper()] = dict(r)

                    # Fallback sources from cds_views
                    missing_chunk = [v for v in chunk if v.upper() not in sources_map or not sources_map[v.upper()].get("source_code")]
                    if missing_chunk:
                        m_ph = ",".join(["?"] * len(missing_chunk))
                        c.execute(
                            f"SELECT cds_name as ddl_name, source_code, '' as source_hash, 'CACHED_CATALOG' as source_status FROM cds_views WHERE UPPER(cds_name) IN ({m_ph})",
                            [v.upper() for v in missing_chunk],
                        )
                        for r in c.fetchall():
                            if r["source_code"]:
                                sources_map[r["ddl_name"].upper()] = dict(r)

                    # Metadata from cds_views
                    c.execute(
                        f"SELECT cds_name, label, domain, package FROM cds_views WHERE UPPER(cds_name) IN ({ph})",
                        [v.upper() for v in chunk],
                    )
                    for r in c.fetchall():
                        meta_map[r["cds_name"].upper()] = dict(r)

        # 5. Parse actual DDL source of each candidate & verify direct table + extraction
        final_recommendations: List[Dict[str, Any]] = []

        for vname in sorted(list(direct_candidate_names)):
            v_up = vname.upper()
            s_info = sources_map.get(v_up, {})
            s_code = s_info.get("source_code", "")

            if not s_code:
                excluded_count += 1
                continue

            v_meta = meta_map.get(v_up, {})
            label = v_meta.get("label") or vname
            domain = v_meta.get("domain") or "Core"
            package = v_meta.get("package") or "SAP_STANDARD"
            is_std = self.is_standard_cds(vname, package=package)

            if standard_only and not is_std:
                non_standard_excluded += 1
                continue

            # Strip comments
            clean_ddl = re.sub(r"/\*[\s\S]*?\*/", "", s_code)
            clean_ddl = re.sub(r"//.*$", "", clean_ddl, flags=re.MULTILINE)
            clean_ddl = re.sub(r"--.*$", "", clean_ddl, flags=re.MULTILINE)

            # Parse direct FROM clause
            from_matches = re.finditer(r"\bas\s+select\s+from\s+([A-Za-z0-9_\/]+)", clean_ddl, re.IGNORECASE)
            from_targets = [m.group(1).strip().upper() for m in from_matches if m.group(1).strip().upper() not in ("SELECT", "FROM")]
            main_from = from_targets[0] if from_targets else "None"

            # Parse direct JOIN clauses
            join_matches = re.finditer(r"\bjoin\s+([A-Za-z0-9_\/]+)", clean_ddl, re.IGNORECASE)
            join_targets = [m.group(1).strip().upper() for m in join_matches]

            # Direct database table sources in this view's own DDL
            direct_sources_in_ddl = set(from_targets + join_targets)

            # Check which of the requested valid tables appear directly in this view's own DDL
            matched_tables = [tbl for tbl in valid_tables if tbl.upper() in direct_sources_in_ddl]

            if not matched_tables:
                # STRICT RULE: Reject any view whose own DDL does not directly select from or join the table
                excluded_count += 1
                continue

            # Check matching mode
            if mode_clean == "ALL_TABLES":
                if len(matched_tables) != len(valid_tables):
                    # STRICT RULE: Must directly reference EVERY requested table in the same DDL
                    continue
            else:
                if not matched_tables:
                    continue

            # Verify Extraction Enablement strictly from DDL / authoritative metadata
            ext_status, cdc_status, sql_name, details = self.verify_extraction_and_cdc(
                cds_name=vname,
                source_code=s_code,
                authoritative_records=ext_records,
            )

            # STRICT RULE: Require @Analytics.dataExtraction.enabled: true
            if ext_status != "EXTRACTION_ENABLED_VERIFIED":
                excluded_count += 1
                continue

            # Build dependency paths (Direct depth 1 only)
            dep_paths = []
            table_roles = {}
            for tbl in matched_tables:
                tbl_up = tbl.upper()
                if tbl_up in from_targets:
                    table_roles[tbl] = "SELECT FROM"
                    dep_paths.append({
                        "table_name": tbl,
                        "path": [tbl, vname],
                        "path_str": f"{tbl} -> {vname}",
                        "depth": 1,
                        "type": "SQL_SOURCE_DEPENDENCY",
                        "evidence": f"as select from {tbl}",
                    })
                elif tbl_up in join_targets:
                    table_roles[tbl] = "JOIN"
                    dep_paths.append({
                        "table_name": tbl,
                        "path": [tbl, vname],
                        "path_str": f"{tbl} -> {vname}",
                        "depth": 1,
                        "type": "JOIN_DEPENDENCY",
                        "evidence": f"join {tbl}",
                    })

            rec_row = {
                "cds_name": vname,
                "ddl_name": vname,
                "sql_view_name": sql_name or "None (View Entity)",
                "direct_from_source": main_from,
                "direct_join_sources": join_targets,
                "matched_input_tables": matched_tables,
                "requested_table": ", ".join(matched_tables),
                "requested_database_table": ", ".join(matched_tables),
                "dependency_classification": "DIRECT_TABLE_SOURCE",
                "dependency_depth": 1,
                "complete_dependency_path": f"{', '.join(matched_tables)} -> {vname}",
                "dependency_paths": dep_paths,
                "dependency_type": "SQL_SOURCE_DEPENDENCY" if any(tbl.upper() in from_targets for tbl in matched_tables) else "JOIN_DEPENDENCY",
                "dependency_types": sorted(list({p["type"] for p in dep_paths})),
                "extraction_status": ext_status,
                "data_extraction_enabled": True,
                "verification_evidence": details["extraction_evidence"],
                "cdc_status": cdc_status,
                "delta_cdc_supported": (cdc_status == "CDC_SUPPORTED"),
                "cdc_evidence": details["cdc_evidence"],
                "source_verification_status": "VERIFIED_FROM_DDL_SOURCE",
                "has_source": True,
                "source_code_preview": s_code[:300] if s_code else "",
                "label": label,
                "domain": domain,
                "package": package,
                "is_standard": is_std,
                "sap_origin": "SAP_STANDARD" if is_std else "CUSTOM_Z_Y",
                "related_input_tables": matched_tables,
                "table_roles": table_roles,
            }
            final_recommendations.append(rec_row)

        # Sort recommendations by CDS Name
        final_recommendations.sort(key=lambda x: x["cds_name"])

        elapsed_ms = round((time.time() - start_time) * 1000, 1)
        duplicates_removed = sum(len(v["dependency_paths"]) - 1 for v in final_recommendations)

        # 7. Persist run to SQLite tables
        try:
            with self._get_db() as conn:
                c = conn.cursor()
                now_str = datetime.now(timezone.utc).isoformat()

                # Run record
                c.execute(
                    """
                    INSERT INTO table_discovery_runs (run_id, matching_mode, execution_status, created_at, completed_at)
                    VALUES (?, ?, 'COMPLETED', ?, ?)
                    """,
                    (run_id, mode_clean, now_str, now_str),
                )

                # Input records
                for tbl in valid_tables:
                    c.execute(
                        """
                        INSERT INTO table_discovery_inputs (run_id, table_name, validation_status)
                        VALUES (?, ?, 'VALID_TRANSPARENT_TABLE')
                        """,
                        (run_id, tbl),
                    )
                for inv in invalid_tables:
                    c.execute(
                        """
                        INSERT INTO table_discovery_inputs (run_id, table_name, validation_status)
                        VALUES (?, ?, 'INVALID_TABLE')
                        """,
                        (run_id, inv["table_name"]),
                    )

                # Results & Paths records
                for rec in final_recommendations:
                    c.execute(
                        """
                        INSERT OR REPLACE INTO table_discovery_results 
                        (run_id, ddl_name, extraction_status, cdc_status, verification_status, dependency_classification, dependency_depth, direct_from_source, direct_join_sources, sql_view_name, requested_table, complete_dependency_path)
                        VALUES (?, ?, ?, ?, 'VERIFIED', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            rec["ddl_name"],
                            rec["extraction_status"],
                            rec["cdc_status"],
                            rec.get("dependency_classification", "DIRECT_TABLE_SOURCE"),
                            rec.get("dependency_depth", 1),
                            rec.get("direct_from_source", ""),
                            ", ".join(rec.get("direct_join_sources", [])),
                            rec.get("sql_view_name", ""),
                            rec.get("requested_table", valid_tables[0] if valid_tables else ""),
                            rec.get("complete_dependency_path", ""),
                        ),
                    )

                    for p in rec["dependency_paths"]:
                        c.execute(
                            """
                            INSERT INTO table_discovery_paths 
                            (run_id, table_name, ddl_name, dependency_path, dependency_type, dependency_depth, source_evidence, dependency_classification, direct_from_source)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                p.get("table_name", rec.get("requested_table", valid_tables[0] if valid_tables else "")),
                                rec["ddl_name"],
                                p["path_str"],
                                p.get("dependency_type") or p.get("type", "SQL_SOURCE_DEPENDENCY"),
                                p["depth"],
                                p.get("evidence", ""),
                                rec.get("dependency_classification", "DIRECT_TABLE_SOURCE"),
                                rec.get("direct_from_source", ""),
                            ),
                        )

                conn.commit()
        except Exception as e:
            logger.warning("Failed to persist table discovery run %s: %s", run_id, e)

        # 8. Return structured payload (including backward-compatible fields)
        sources_compat: List[Dict[str, Any]] = []
        joins_compat: List[Dict[str, Any]] = []
        associations_compat: List[Dict[str, Any]] = []
        combined_views_compat: List[Dict[str, Any]] = []

        for r in final_recommendations:
            t_roles = r.get("table_roles", {})
            has_from = any("SELECT FROM" in role.upper() or "PRIMARY" in role.upper() for role in t_roles.values()) or r.get("dependency_type") == "SQL_SOURCE_DEPENDENCY"
            has_join = any("JOIN" in role.upper() for role in t_roles.values()) or "JOIN_DEPENDENCY" in r.get("dependency_types", [])
            has_assoc = any("ASSOCIATION" in role.upper() for role in t_roles.values()) or "ASSOCIATION_DEPENDENCY" in r.get("dependency_types", [])
            is_combined = len(r.get("related_input_tables", [])) > 1

            item_base = {
                "cds_name": r["cds_name"],
                "label": r["label"],
                "domain": r["domain"],
                "package": r["package"],
                "is_standard": r.get("is_standard", True),
                "sap_origin": r.get("sap_origin", "SAP_STANDARD"),
                "relationship_type": r["dependency_type"],
                "level": r["dependency_depth"],
                "clause_label": r["dependency_type"].replace("_DEPENDENCY", ""),
                "data_extraction_enabled": r["data_extraction_enabled"],
                "extraction_status": r["extraction_status"],
                "delta_cdc_supported": r["delta_cdc_supported"],
                "cdc_status": r["cdc_status"],
                "sql_view_name": r["sql_view_name"],
                "direct_from_source": r.get("direct_from_source", ""),
                "direct_join_sources": r.get("direct_join_sources", []),
                "matched_input_tables": r.get("matched_input_tables", []),
                "requested_table": r.get("requested_table", ""),
                "requested_database_table": r.get("requested_database_table", ""),
                "dependency_classification": r.get("dependency_classification", "DIRECT_TABLE_SOURCE"),
                "complete_dependency_path": r.get("complete_dependency_path", ""),
                "source_verification_status": r.get("source_verification_status", "VERIFIED_FROM_DDL_SOURCE"),
                "verification_evidence": r.get("verification_evidence", ""),
                "cdc_evidence": r.get("cdc_evidence", ""),
                "related_input_tables": r["related_input_tables"],
                "table_roles": t_roles,
                "dependency_paths": r["dependency_paths"],
                "classification": "BASIC",
                "is_basic": True,
                "custom_wrapper_required": False,
                "wrapper_reason": "",
            }

            if has_from:
                sources_compat.append(item_base)
            if has_join:
                join_item = dict(item_base)
                joined_tbls = [tbl for tbl, role in t_roles.items() if "JOIN" in role.upper()]
                join_item["joined_table"] = joined_tbls[0] if joined_tbls else (r["related_input_tables"][-1] if r["related_input_tables"] else "")
                join_item["join_type"] = "JOIN"
                joins_compat.append(join_item)
            if has_assoc:
                assoc_item = dict(item_base)
                assoc_tbls = [tbl for tbl, role in t_roles.items() if "ASSOCIATION" in role.upper()]
                assoc_item["target_entity"] = assoc_tbls[0] if assoc_tbls else ""
                assoc_item["association_name"] = f"_{assoc_item['target_entity']}"
                associations_compat.append(assoc_item)
            if is_combined:
                combined_views_compat.append(item_base)

        if not sources_compat and final_recommendations:
            sources_compat = [
                {
                    "cds_name": r["cds_name"],
                    "label": r["label"],
                    "domain": r["domain"],
                    "package": r["package"],
                    "is_standard": r.get("is_standard", True),
                    "sap_origin": r.get("sap_origin", "SAP_STANDARD"),
                    "relationship_type": r["dependency_type"],
                    "level": r["dependency_depth"],
                    "clause_label": r["dependency_type"].replace("_DEPENDENCY", ""),
                    "data_extraction_enabled": r["data_extraction_enabled"],
                    "extraction_status": r["extraction_status"],
                    "delta_cdc_supported": r["delta_cdc_supported"],
                    "cdc_status": r["cdc_status"],
                    "sql_view_name": r["sql_view_name"],
                    "direct_from_source": r.get("direct_from_source", ""),
                    "direct_join_sources": r.get("direct_join_sources", []),
                    "matched_input_tables": r.get("matched_input_tables", []),
                    "related_input_tables": r["related_input_tables"],
                    "table_roles": r.get("table_roles", {}),
                    "dependency_paths": r["dependency_paths"],
                    "classification": "BASIC",
                    "is_basic": True,
                }
                for r in final_recommendations
            ]

        return {
            "run_id": run_id,
            "table_name": ", ".join(valid_tables),
            "matching_mode": mode_clean,
            "input_tables": valid_tables + [t["table_name"] for t in invalid_tables],
            "valid_tables": valid_tables,
            "invalid_tables": invalid_tables,
            "recommendations": final_recommendations,
            "message": "No extraction-enabled CDS views directly reference the selected transparent database tables." if not final_recommendations else f"Found {len(final_recommendations)} direct extraction-enabled CDS view(s).",
            "standard_only": standard_only,
            "standard_views_enforced": True,
            "summary_metrics": {
                "input_tables_count": len(valid_tables) + len(invalid_tables),
                "valid_tables_count": len(valid_tables),
                "invalid_tables_count": len(invalid_tables),
                "total_discovered_candidates": total_discovered,
                "unique_extraction_recommended": len(final_recommendations),
                "duplicates_removed": duplicates_removed,
                "unverified_excluded": excluded_count,
                "non_standard_excluded": non_standard_excluded,
                "standard_only": standard_only,
                "execution_time_ms": elapsed_ms,
            },
            # Backward-compatible fields for existing UI components and test suites
            "total_matches": len(final_recommendations),
            "is_multi_table": len(valid_tables) > 1,
            "requested_tables": valid_tables,
            "sources": sources_compat,
            "joins": joins_compat,
            "associations": associations_compat,
            "combined_views": combined_views_compat if len(valid_tables) > 1 else sources_compat,
            "total_combined_matches": len(combined_views_compat) if len(valid_tables) > 1 else len(sources_compat),
            "basic_cds_enforced": True,
            "cds_enabled_enforced": True,
            "classification_filter": "BASIC_ONLY",
            "vdm_view_type": "BASIC",
            "extraction_filtering": {
                "filter_active": True,
                "metadata_complete": ext_meta.get("is_complete", False),
                "total_extraction_records_in_sap": len(ext_records),
                "excluded_total_count": excluded_count,
            },
            "query_time_ms": elapsed_ms,
        }

    def get_run_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves history of past table discovery runs."""
        with self._get_db() as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT r.run_id, r.matching_mode, r.execution_status, r.created_at, r.completed_at,
                       COUNT(DISTINCT res.ddl_name) AS recommendations_count
                FROM table_discovery_runs r
                LEFT JOIN table_discovery_results res ON r.run_id = res.run_id
                GROUP BY r.run_id
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in c.fetchall()]

    def get_run_details(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves details, inputs, and results for a specific discovery run."""
        with self._get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM table_discovery_runs WHERE run_id = ?", (run_id,))
            run_row = c.fetchone()
            if not run_row:
                return None

            c.execute("SELECT table_name, validation_status FROM table_discovery_inputs WHERE run_id = ?", (run_id,))
            inputs = [dict(r) for r in c.fetchall()]

            c.execute("SELECT ddl_name, extraction_status, cdc_status, verification_status FROM table_discovery_results WHERE run_id = ?", (run_id,))
            results = [dict(r) for r in c.fetchall()]

            c.execute(
                """
                SELECT table_name, ddl_name, dependency_path, dependency_type, dependency_depth, source_evidence 
                FROM table_discovery_paths WHERE run_id = ?
                """,
                (run_id,),
            )
            paths = [dict(r) for r in c.fetchall()]

            # Group paths by ddl_name
            paths_by_ddl: Dict[str, List[Dict[str, Any]]] = {}
            for p in paths:
                paths_by_ddl.setdefault(p["ddl_name"], []).append(p)

            for res in results:
                res["paths"] = paths_by_ddl.get(res["ddl_name"], [])

            return {
                "run": dict(run_row),
                "inputs": inputs,
                "results": results,
                "total_recommendations": len(results),
            }
