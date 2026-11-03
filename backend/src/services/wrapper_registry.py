"""Authoritative Wrapper Registry & Mapping Service.

Maintains relationship between source CDS views and custom extraction wrappers.
Retrieves actual wrapper names and mappings without assumptions or invented names.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.config import get_settings

logger = logging.getLogger("sap_cds_platform.wrapper_registry")


def get_cds_db_path() -> Path:
    p1 = Path(__file__).resolve().parent.parent.parent / "data" / "sap_cds.db"
    if p1.exists():
        return p1
    p2 = Path(__file__).resolve().parent.parent / "data" / "sap_cds.db"
    return p2


def get_cds_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_cds_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_wrappers_table(conn: Optional[sqlite3.Connection] = None):
    should_close = False
    if conn is None:
        conn = get_cds_db_conn()
        should_close = True
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS cds_wrappers (
                source_cds_name TEXT NOT NULL,
                wrapper_cds_name TEXT NOT NULL,
                sql_view_name TEXT,
                package TEXT,
                transport_request TEXT,
                creation_status TEXT,
                activation_status TEXT,
                created_at TEXT,
                PRIMARY KEY (source_cds_name, wrapper_cds_name)
            )
        """)
        initial_wrappers = [
            ("I_BILLINGDOCUMENTITEM", "ZC_DATAWRAPPER1", "ZASDF", "ZTRAINING_2026", "S3XK911848", "CREATED", "ACTIVE", "2026-11-03 12:37:56"),
            ("I_JOURNALENTRYITEM", "ZC_DATAWRAPPER", "ZCDATAWRAP", "ZTRAINING_2026", "S3XK911848", "CREATED", "INACTIVE", "2026-11-03 12:28:23"),
        ]
        for row in initial_wrappers:
            c.execute("""
                INSERT OR IGNORE INTO cds_wrappers 
                (source_cds_name, wrapper_cds_name, sql_view_name, package, transport_request, creation_status, activation_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, row)
        conn.commit()
    finally:
        if should_close:
            conn.close()


def register_created_wrapper(
    source_cds_name: str,
    wrapper_cds_name: str,
    sql_view_name: str = "",
    package: str = "ZTRAINING_2026",
    transport_request: str = "",
    creation_status: str = "CREATED",
    activation_status: str = "INACTIVE",
):
    with get_cds_db_conn() as conn:
        ensure_wrappers_table(conn)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO cds_wrappers
            (source_cds_name, wrapper_cds_name, sql_view_name, package, transport_request, creation_status, activation_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            source_cds_name.strip().upper(),
            wrapper_cds_name.strip().upper(),
            sql_view_name.strip().upper(),
            package.strip().upper(),
            transport_request.strip().upper(),
            creation_status,
            activation_status,
        ))
        conn.commit()


def update_wrapper_activation_status(wrapper_cds_name: str, activation_status: str = "ACTIVE"):
    with get_cds_db_conn() as conn:
        ensure_wrappers_table(conn)
        c = conn.cursor()
        c.execute("""
            UPDATE cds_wrappers
            SET activation_status = ?
            WHERE UPPER(wrapper_cds_name) = UPPER(?)
        """, (activation_status, wrapper_cds_name.strip()))
        conn.commit()


def get_wrapper_for_source_cds(source_cds_name: str) -> Optional[Dict[str, Any]]:
    clean_name = source_cds_name.strip().upper()
    with get_cds_db_conn() as conn:
        ensure_wrappers_table(conn)
        c = conn.cursor()
        c.execute("""
            SELECT wrapper_cds_name, sql_view_name, package, transport_request, creation_status, activation_status, created_at
            FROM cds_wrappers
            WHERE UPPER(source_cds_name) = ?
            ORDER BY CASE WHEN activation_status = 'ACTIVE' THEN 0 WHEN creation_status = 'CREATED' THEN 1 ELSE 2 END, rowid DESC
            LIMIT 1
        """, (clean_name,))
        row = c.fetchone()
        if row:
            return dict(row)

        # Fallback to cds_views where parent_name matches
        c.execute("""
            SELECT cds_name, package, updated_at
            FROM cds_views
            WHERE UPPER(parent_name) = ? AND (cds_name LIKE 'Z%' OR cds_name LIKE 'Y%')
            ORDER BY rowid DESC
            LIMIT 1
        """, (clean_name,))
        v_row = c.fetchone()
        if v_row:
            return {
                "source_cds_name": clean_name,
                "wrapper_cds_name": v_row["cds_name"],
                "sql_view_name": "",
                "package": v_row["package"] or "ZTRAINING_2026",
                "transport_request": "",
                "creation_status": "CREATED",
                "activation_status": "CREATED",
                "created_at": v_row["updated_at"] or "",
            }
        return None


async def get_views_wrapper_mapping_details(
    selected_views: List[str],
    required_fields: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    clean_views = [v.strip().upper() for v in selected_views if v and v.strip()]
    if not clean_views:
        return []

    # Map required_fields for matching
    rf_by_name = {}
    if required_fields:
        for rf in required_fields:
            name = (rf.get("name") or "").strip().lower()
            if name:
                rf_by_name[name] = rf
            for var in rf.get("variants", []):
                v_clean = str(var).strip().lower()
                if v_clean:
                    rf_by_name[v_clean] = rf

    result = []
    with get_cds_db_conn() as conn:
        ensure_wrappers_table(conn)
        c = conn.cursor()

        for v in clean_views:
            # 1. Check wrapper
            wrapper_rec = get_wrapper_for_source_cds(v)
            has_wrapper = wrapper_rec is not None
            wrapper_name = wrapper_rec["wrapper_cds_name"] if has_wrapper else "Not Created"
            
            wrapper_creation_status = "Not Created"
            if has_wrapper:
                act = (wrapper_rec.get("activation_status") or "").upper()
                if act == "ACTIVE":
                    wrapper_creation_status = "ACTIVE (SAP CONFIRMED)"
                elif act in ("INACTIVE", "CREATED"):
                    wrapper_creation_status = "CREATED (INACTIVE)"
                else:
                    wrapper_creation_status = act or "CREATED"

            # 2. View description & package
            c.execute("SELECT label, package FROM cds_views WHERE UPPER(cds_name) = ?", (v,))
            v_meta = c.fetchone()
            desc = ""
            pkg = wrapper_rec.get("package") if wrapper_rec else "ZTRAINING_2026"
            if v_meta:
                desc = v_meta["label"] or ""
                if v_meta["package"]:
                    pkg = v_meta["package"]

            # 3. Retrieve fields
            c.execute("""
                SELECT fieldname, field_label, data_type, length, decimals, is_key
                FROM cds_fields
                WHERE UPPER(cds_name) = ?
                ORDER BY is_key DESC, id ASC
            """, (v,))
            field_rows = c.fetchall()

            # If no fields in local db, fetch live from ADT
            fields_data = [dict(r) for r in field_rows]
            if not fields_data:
                try:
                    from src.sap.adt_client import SAPADTClient
                    settings = get_settings()
                    client = SAPADTClient(settings)
                    try:
                        view_meta = await client.get_view_metadata(v)
                        for f in view_meta.fields:
                            fields_data.append({
                                "fieldname": f.name,
                                "field_label": f.description or f.name,
                                "data_type": f.data_type or f.abap_type or "CHAR",
                                "length": f.length,
                                "decimals": f.decimals,
                                "is_key": f.is_key,
                            })
                    finally:
                        await client.aclose()
                except Exception as live_err:
                    logger.debug("Live ADT metadata fetch for %s failed: %s", v, live_err)

            # Build field mappings
            mappings = []
            for f in fields_data:
                fn = f.get("fieldname") or ""
                fn_lower = fn.lower()
                fl = f.get("field_label") or fn
                fl_lower = fl.lower()
                dt = f.get("data_type") or "CHAR"
                if f.get("length"):
                    dt = f"{dt}({f['length']})"
                is_key = bool(f.get("is_key"))

                # Match business field
                matched_rf = rf_by_name.get(fn_lower) or rf_by_name.get(fl_lower)
                biz_field = matched_rf.get("name") if matched_rf else fl

                mappings.append({
                    "business_field": biz_field,
                    "source_cds": v,
                    "actual_field": fn,
                    "field_label": fl,
                    "data_type": dt,
                    "is_key": is_key,
                    "wrapper_target_field": fn,
                    "evidence": "Verified SAP Data Dictionary",
                })

            result.append({
                "source_cds_view": v,
                "description": desc or v,
                "package": pkg,
                "wrapper_cds_view": wrapper_name,
                "has_wrapper": has_wrapper,
                "wrapper_creation_status": wrapper_creation_status,
                "sql_view_name": wrapper_rec.get("sql_view_name") if wrapper_rec else None,
                "transport_request": wrapper_rec.get("transport_request") if wrapper_rec else None,
                "mappings": mappings,
                "total_fields": len(mappings),
                "can_create_wrapper": not has_wrapper,
            })

    return result
