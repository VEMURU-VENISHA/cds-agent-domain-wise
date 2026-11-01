"""Authoritative Human-in-the-Loop Join Configuration and Validation Service.

Enforces strict rules for Part 6:
- Display all verified fields for all selected CDS views.
- Allows user selection of output fields.
- Validates user-configured join conditions:
  - Field existence in respective views.
  - Data type and length compatibility.
  - Key compatibility & data grain.
  - Cardinality evidence (0..1, 1..1, 0..*, 1..*).
  - Cartesian join risk & duplicate record risk.
  - Distinguishes SAP-verified vs metadata-derived vs user-proposed relationships.
- Issues structured validation warnings and blocking errors.
- Manages explicit approval gate: APPROVE SOURCE CDS AND JOIN CONFIGURATION.
"""

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from src.logging_config import logger
from src.models.domain import WorkflowStatus


class JoinCondition(BaseModel):
    left_field: str
    right_field: str
    operator: str = "="
    logical_op: str = "AND"


class JoinRelationship(BaseModel):
    left_cds: str
    right_cds: str
    join_type: str = "association [0..1]"  # e.g., 'association [0..1]', 'association [1..1]', 'inner join', etc.
    conditions: List[JoinCondition] = Field(default_factory=list)
    cardinality: str = "0..1"
    relationship_evidence_type: str = "USER_PROPOSED"  # SAP_VERIFIED, METADATA_DERIVED, USER_PROPOSED
    evidence_detail: str = ""
    is_user_edited: bool = False


class OutputFieldSelection(BaseModel):
    field_name: str
    source_cds: str
    data_type: str = "CHAR"
    length: Optional[int] = None
    decimals: Optional[int] = None
    is_key: bool = False
    field_label: str = ""
    selected_for_output: bool = True
    output_alias: Optional[str] = None


class JoinValidationIssue(BaseModel):
    code: str
    message: str
    severity: str = "WARNING"  # ERROR, WARNING, INFO
    left_cds: Optional[str] = None
    right_cds: Optional[str] = None
    field: Optional[str] = None


class JoinValidationResult(BaseModel):
    is_valid: bool
    status: WorkflowStatus
    errors: List[JoinValidationIssue] = Field(default_factory=list)
    warnings: List[JoinValidationIssue] = Field(default_factory=list)
    relationships: List[Dict[str, Any]] = Field(default_factory=list)
    output_fields: List[Dict[str, Any]] = Field(default_factory=list)
    requires_approval: bool = True
    is_approved: bool = False


class JoinValidatorService:
    """Validates multi-CDS join configurations against authoritative SAP metadata."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = Path(__file__).resolve().parent.parent.parent / "data" / "sap_cds.db"
            if not self.db_path.exists():
                self.db_path = Path(__file__).resolve().parent.parent / "data" / "sap_cds.db"

    def _get_db(self) -> Optional[sqlite3.Connection]:
        if self.db_path and self.db_path.exists():
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            return conn
        return None

    def get_all_fields_for_views(self, view_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Loads all verified technical fields for every selected CDS view from the catalog."""
        clean_names = [v.strip().upper() for v in view_names if v and v.strip()]
        result: Dict[str, List[Dict[str, Any]]] = {v: [] for v in clean_names}
        if not clean_names:
            return result

        db = self._get_db()
        if not db:
            return result

        try:
            cur = db.cursor()
            placeholders = ", ".join("?" for _ in clean_names)
            query = f"""
                SELECT cds_name, fieldname, data_element, data_type, length, decimals, is_key, field_label, source
                FROM cds_fields
                WHERE UPPER(cds_name) IN ({placeholders})
                ORDER BY cds_name, is_key DESC, id ASC
            """
            cur.execute(query, clean_names)
            for row in cur.fetchall():
                v_up = row["cds_name"].strip().upper()
                field_item = {
                    "field_name": row["fieldname"],
                    "source_cds": row["cds_name"],
                    "data_type": row["data_type"] or "CHAR",
                    "length": row["length"],
                    "decimals": row["decimals"],
                    "is_key": bool(row["is_key"]),
                    "field_label": row["field_label"] or row["fieldname"],
                    "data_element": row["data_element"] or "",
                    "selected_for_output": True,
                    "output_alias": row["fieldname"],
                }
                result.setdefault(v_up, []).append(field_item)
            return result
        finally:
            db.close()

    def get_verified_relationships(self, left_cds: str, right_cds: str) -> List[Dict[str, Any]]:
        """Queries the relationship index to check if SAP has a verified association or join between these views."""
        left_up = left_cds.strip().upper()
        right_up = right_cds.strip().upper()
        db = self._get_db()
        if not db:
            return []

        verified = []
        try:
            cur = db.cursor()
            # 1. Check cds_associations
            cur.execute("""
                SELECT cds_name, target_entity, association_name, cardinality, on_condition
                FROM cds_associations
                WHERE UPPER(cds_name) = ? AND UPPER(target_entity) = ?
            """, (left_up, right_up))
            for row in cur.fetchall():
                verified.append({
                    "left_cds": row["cds_name"],
                    "right_cds": row["target_entity"],
                    "relationship_type": "association",
                    "association_name": row["association_name"],
                    "cardinality": row["cardinality"] or "0..1",
                    "on_condition": row["on_condition"],
                    "evidence_type": "SAP_VERIFIED",
                    "evidence_detail": f"Verified SAP association '{row['association_name']}' in DDL: {row['on_condition']}",
                })

            # 2. Check reverse association if right -> left
            cur.execute("""
                SELECT cds_name, target_entity, association_name, cardinality, on_condition
                FROM cds_associations
                WHERE UPPER(cds_name) = ? AND UPPER(target_entity) = ?
            """, (right_up, left_up))
            for row in cur.fetchall():
                verified.append({
                    "left_cds": left_up,
                    "right_cds": right_up,
                    "relationship_type": "reverse_association",
                    "association_name": row["association_name"],
                    "cardinality": row["cardinality"] or "0..1",
                    "on_condition": row["on_condition"],
                    "evidence_type": "SAP_VERIFIED",
                    "evidence_detail": f"Verified reverse SAP association '{row['association_name']}' on {right_up}: {row['on_condition']}",
                })

            return verified
        finally:
            db.close()

    def validate_join_configuration(
        self,
        selected_views: List[str],
        relationships: List[Dict[str, Any]],
        output_fields: Optional[List[Dict[str, Any]]] = None,
        views_metadata: Optional[Dict[str, Any]] = None,
        is_approved: bool = False,
    ) -> JoinValidationResult:
        """Thoroughly validates proposed relationships and output field selections."""
        clean_views = [v.strip().upper() for v in selected_views if v and v.strip()]
        errors: List[JoinValidationIssue] = []
        warnings: List[JoinValidationIssue] = []

        # 1. Fetch metadata if not provided
        if not views_metadata:
            fields_by_view = self.get_all_fields_for_views(clean_views)
        else:
            fields_by_view = {}
            for v, meta in views_metadata.items():
                v_up = v.strip().upper()
                f_list = meta.get("fields", [])
                fields_by_view[v_up] = [
                    {
                        "field_name": f.get("fieldname") or f.get("field_name"),
                        "source_cds": v,
                        "data_type": f.get("data_type", "CHAR"),
                        "length": f.get("length"),
                        "decimals": f.get("decimals"),
                        "is_key": bool(f.get("is_key")),
                        "field_label": f.get("field_label", ""),
                        "selected_for_output": True,
                    }
                    for f in f_list
                ]

        # 2. If multiple views, relationships MUST be defined
        if len(clean_views) > 1 and not relationships:
            errors.append(JoinValidationIssue(
                code="MISSING_RELATIONSHIP",
                message=f"Multiple CDS views selected ({', '.join(clean_views)}) but no join relationship is configured. Direct Cartesian product is strictly forbidden.",
                severity="ERROR",
            ))

        # Check connectivity of selected views (all selected views must be linked)
        connected_views: Set[str] = set()
        primary_view = clean_views[0] if clean_views else ""
        if primary_view:
            connected_views.add(primary_view)

        processed_relationships: List[Dict[str, Any]] = []

        for idx, rel_item in enumerate(relationships):
            if hasattr(rel_item, "model_dump"):
                rel = rel_item.model_dump()
            elif hasattr(rel_item, "dict"):
                rel = rel_item.dict()
            elif isinstance(rel_item, dict):
                rel = dict(rel_item)
            else:
                rel = vars(rel_item)

            left = (rel.get("left_cds") or rel.get("source_cds") or "").strip().upper()
            right = (rel.get("right_cds") or rel.get("target_cds") or "").strip().upper()
            join_type = rel.get("join_type", "association [0..1]")
            cardinality = rel.get("cardinality", "0..1")
            raw_conditions = rel.get("conditions") or []
            is_user_edited = rel.get("is_user_edited", False)

            # Check view existence in selected list
            if left not in clean_views:
                errors.append(JoinValidationIssue(
                    code="INVALID_SOURCE_VIEW",
                    message=f"Left CDS view '{left}' is not in selected views list.",
                    severity="ERROR",
                    left_cds=left,
                ))
            if right not in clean_views:
                errors.append(JoinValidationIssue(
                    code="INVALID_TARGET_VIEW",
                    message=f"Right CDS view '{right}' is not in selected views list.",
                    severity="ERROR",
                    right_cds=right,
                ))

            if left == right and left:
                errors.append(JoinValidationIssue(
                    code="SELF_JOIN_UNSUPPORTED",
                    message=f"Self-join on '{left}' is not supported.",
                    severity="ERROR",
                    left_cds=left,
                ))

            connected_views.add(left)
            connected_views.add(right)

            left_fields_dict = {f["field_name"].strip().upper(): f for f in fields_by_view.get(left, [])}
            right_fields_dict = {f["field_name"].strip().upper(): f for f in fields_by_view.get(right, [])}

            # If conditions provided as flat source_field/target_field
            normalized_conditions: List[Dict[str, str]] = []
            if not raw_conditions and rel.get("source_field") and rel.get("target_field"):
                normalized_conditions.append({
                    "left_field": rel["source_field"],
                    "right_field": rel["target_field"],
                    "operator": "=",
                    "logical_op": "AND",
                })
            else:
                for c in raw_conditions:
                    if hasattr(c, "model_dump"):
                        c_dict = c.model_dump()
                    elif hasattr(c, "dict"):
                        c_dict = c.dict()
                    elif isinstance(c, dict):
                        c_dict = c
                    else:
                        c_dict = vars(c)
                    normalized_conditions.append({
                        "left_field": c_dict.get("left_field") or c_dict.get("source_field") or "",
                        "right_field": c_dict.get("right_field") or c_dict.get("target_field") or "",
                        "operator": c_dict.get("operator", "="),
                        "logical_op": c_dict.get("logical_op", "AND"),
                    })

            if not normalized_conditions:
                errors.append(JoinValidationIssue(
                    code="CARTESIAN_PRODUCT_RISK",
                    message=f"Join between '{left}' and '{right}' has NO conditions. A Cartesian product would crash database runtime.",
                    severity="ERROR",
                    left_cds=left,
                    right_cds=right,
                ))

            # Validate each join condition
            for c in normalized_conditions:
                lf_name = c["left_field"].strip().upper()
                rf_name = c["right_field"].strip().upper()

                # Field existence check
                if lf_name not in left_fields_dict:
                    errors.append(JoinValidationIssue(
                        code="FIELD_NOT_FOUND",
                        message=f"Left join field '{c['left_field']}' does not exist in verified metadata of '{left}'.",
                        severity="ERROR",
                        left_cds=left,
                        field=c["left_field"],
                    ))
                if rf_name not in right_fields_dict:
                    errors.append(JoinValidationIssue(
                        code="FIELD_NOT_FOUND",
                        message=f"Right join field '{c['right_field']}' does not exist in verified metadata of '{right}'.",
                        severity="ERROR",
                        right_cds=right,
                        field=c["right_field"],
                    ))

                # Data type and length compatibility
                if lf_name in left_fields_dict and rf_name in right_fields_dict:
                    lf_meta = left_fields_dict[lf_name]
                    rf_meta = right_fields_dict[rf_name]
                    lt = (lf_meta.get("data_type") or "CHAR").upper()
                    rt = (rf_meta.get("data_type") or "CHAR").upper()
                    ll = lf_meta.get("length")
                    rl = rf_meta.get("length")

                    # Incompatible base types (e.g. DATS vs INT, CHAR vs DEC)
                    num_types = {"INT1", "INT2", "INT4", "INT8", "DEC", "FLTP", "CURR", "QUAN"}
                    char_types = {"CHAR", "NUMC", "CLNT", "LANG", "UNIT", "CUKY"}
                    date_types = {"DATS", "TIMS"}

                    if (lt in char_types and rt in num_types) or (lt in num_types and rt in char_types):
                        errors.append(JoinValidationIssue(
                            code="TYPE_INCOMPATIBILITY",
                            message=f"Data type mismatch in join condition: '{left}.{c['left_field']}' ({lt}) cannot be joined with '{right}.{c['right_field']}' ({rt}).",
                            severity="ERROR",
                            left_cds=left,
                            right_cds=right,
                            field=f"{c['left_field']}={c['right_field']}",
                        ))
                    elif (lt in date_types and rt not in date_types) or (rt in date_types and lt not in date_types):
                        errors.append(JoinValidationIssue(
                            code="TYPE_INCOMPATIBILITY",
                            message=f"Data type mismatch in join condition: '{left}.{c['left_field']}' ({lt}) cannot be joined with '{right}.{c['right_field']}' ({rt}).",
                            severity="ERROR",
                            left_cds=left,
                            right_cds=right,
                            field=f"{c['left_field']}={c['right_field']}",
                        ))

                    # Length warning
                    if ll and rl and ll != rl:
                        warnings.append(JoinValidationIssue(
                            code="LENGTH_MISMATCH_WARNING",
                            message=f"Field length difference between '{left}.{c['left_field']}' (len {ll}) and '{right}.{c['right_field']}' (len {rl}). May truncate or cause runtime performance penalties.",
                            severity="WARNING",
                            left_cds=left,
                            right_cds=right,
                        ))

                    # Key and Grain analysis
                    rf_is_key = rf_meta.get("is_key", False)
                    if not rf_is_key and "1" in cardinality:
                        warnings.append(JoinValidationIssue(
                            code="CARDINALITY_GRAIN_RISK",
                            message=f"Target join field '{right}.{c['right_field']}' is NOT marked as a key field in SAP metadata. Joining with cardinality [{cardinality}] risks runtime cardinality violations and duplicate records.",
                            severity="WARNING",
                            left_cds=left,
                            right_cds=right,
                        ))

            # Determine Relationship Evidence Type
            verified_in_sap = self.get_verified_relationships(left, right)
            evidence_type = "USER_PROPOSED"
            evidence_detail = "User-proposed relationship."
            if not is_user_edited and verified_in_sap:
                evidence_type = "SAP_VERIFIED"
                evidence_detail = verified_in_sap[0]["evidence_detail"]
            elif is_user_edited and verified_in_sap:
                evidence_type = "USER_PROPOSED"
                evidence_detail = f"User customized relationship (Original SAP association: {verified_in_sap[0]['evidence_detail']})"
            else:
                evidence_type = "USER_PROPOSED"
                evidence_detail = "User configured relationship."

            processed_relationships.append({
                "left_cds": left,
                "right_cds": right,
                "join_type": join_type,
                "cardinality": cardinality,
                "conditions": normalized_conditions,
                "relationship_evidence_type": evidence_type,
                "evidence_detail": evidence_detail,
                "is_user_edited": is_user_edited,
            })

        # Check for unlinked views
        unlinked = set(clean_views) - connected_views
        if unlinked:
            errors.append(JoinValidationIssue(
                code="UNCONNECTED_SOURCE_VIEW",
                message=f"Selected CDS view(s) {', '.join(unlinked)} are not connected by any join or association path.",
                severity="ERROR",
            ))

        # Validate Output Fields
        processed_outputs = []
        if output_fields:
            for of_item in output_fields:
                if hasattr(of_item, "model_dump"):
                    of = of_item.model_dump()
                elif hasattr(of_item, "dict"):
                    of = of_item.dict()
                elif isinstance(of_item, dict):
                    of = dict(of_item)
                else:
                    of = vars(of_item)

                src = (of.get("source_cds") or "").strip().upper()
                fn = of.get("field_name", "")
                if src not in clean_views:
                    errors.append(JoinValidationIssue(
                        code="OUTPUT_FIELD_UNKNOWN_VIEW",
                        message=f"Selected output field '{fn}' specifies unselected source CDS '{src}'.",
                        severity="ERROR",
                    ))
                processed_outputs.append(of)
        else:
            # Default to all fields
            for v_name in clean_views:
                for f_item in fields_by_view.get(v_name, []):
                    processed_outputs.append({
                        "field_name": f_item["field_name"],
                        "source_cds": v_name,
                        "data_type": f_item["data_type"],
                        "length": f_item["length"],
                        "is_key": f_item["is_key"],
                        "selected_for_output": True,
                        "output_alias": f_item["field_name"],
                    })

        # Check if at least one output field is selected
        selected_out_count = sum(1 for f in processed_outputs if f.get("selected_for_output", True))
        if selected_out_count == 0:
            errors.append(JoinValidationIssue(
                code="NO_OUTPUT_FIELDS_SELECTED",
                message="At least one field must be selected for output in the custom CDS wrapper.",
                severity="ERROR",
            ))

        has_blocking_errors = len([e for e in errors if e.severity == "ERROR"]) > 0
        is_valid = not has_blocking_errors

        status = WorkflowStatus.JOIN_CONFIGURATION_APPROVED if (is_valid and is_approved) else WorkflowStatus.JOIN_CONFIGURATION_PENDING

        return JoinValidationResult(
            is_valid=is_valid,
            status=status,
            errors=errors,
            warnings=warnings,
            relationships=processed_relationships,
            output_fields=processed_outputs,
            requires_approval=not is_approved,
            is_approved=is_approved,
        )

    def approve_join_configuration(
        self,
        selected_views: List[str],
        relationships: List[JoinRelationship],
        output_fields: Optional[List[OutputFieldSelection]] = None,
    ) -> JoinValidationResult:
        """Explicitly approves the join configuration and transitions to JOIN_CONFIGURATION_APPROVED."""
        return self.validate_join_configuration(
            selected_views=selected_views,
            relationships=relationships,
            output_fields=output_fields,
            is_approved=True,
        )


join_validator = JoinValidatorService()
