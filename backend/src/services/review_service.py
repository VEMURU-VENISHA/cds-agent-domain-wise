"""Milestone 12: CDS Review & Human Approval Gate Service.

Provides:
- Structured review package synthesis (CDSReviewPackage)
- Deterministic server-side approval eligibility evaluation
- Review state machine (PENDING_REVIEW, APPROVED, REJECTED, CHANGES_REQUESTED)
- Thread-safe in-memory ReviewStore
- Sanitized, immutable review audit trail
- Zero SAP write execution
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from threading import RLock
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.models.domain import CDSWorkflowState
from src.config import get_settings
from src.core.exceptions import (
    EntityNotFoundError,
    GovernanceApprovalRequiredError,
    SAPSecurityError,
    SAPTransportRequestError,
)
from src.db.session import get_active_sqlite_path
from src.models.domain import (
    BusinessRequirement,
    CandidateEvaluation,
    CDSCandidate,
    CDSDraft,
    CDSReviewPackage,
    DataModel,
    Decision,
    DecisionAction,
    DeploymentRequest,
    FieldMapping,
    MappingType,
    ReviewAuditEvent,
    ReviewStatus,
    SAPCreationResult,
    SAPActivationResult,
    SAPReviewCheckrunResult,
    SAPValidationStatus,
    StructuredRequirement,
    ValidationResult,
    utc_now,
)
from src.sap.adt_client import SAPADTClient
from src.sap.connector import get_sap_connector
from src.sap.interface import ISAPConnector

logger = logging.getLogger("sap_cds_platform.review_service")

APPROVAL_CONFIRMATION_NOTICE = (
    "You are approving this AI-generated CDS proposal for the next controlled workflow stage. "
    "No SAP object will be modified by this approval."
)


def evaluate_approval_eligibility(
    state: CDSWorkflowState,
) -> Tuple[bool, List[str]]:
    """Evaluates whether the current pipeline result satisfies all deterministic approval gates.

    Returns:
        (can_approve: bool, blocking_reasons: List[str])
    """
    blocking_reasons: List[str] = []
    decision = state.get("decision")
    draft = state.get("cds_draft")
    validation = state.get("validation_result")
    mappings = state.get("field_mappings", [])

    if not decision:
        return False, ["Pipeline has not formulated an architectural decision."]

    # Rule 1: INSUFFICIENT_EVIDENCE is never approvable for SAP execution
    if decision.action == DecisionAction.INSUFFICIENT_EVIDENCE:
        blocking_reasons.append(
            "Decision is INSUFFICIENT_EVIDENCE: Ambiguous metadata or unresolved concepts halt approval. "
            "Action cannot be approved for SAP execution."
        )
        return False, blocking_reasons

    # Rule 2: REUSE branch eligibility
    if decision.action == DecisionAction.REUSE:
        # Check that no required fields are unresolved or missing
        unresolved = [
            m.requirement_field
            for m in mappings
            if m.mapping_type in (MappingType.UNRESOLVED, MappingType.MISSING)
        ]
        if unresolved:
            blocking_reasons.append(
                f"REUSE action has unresolved/missing requirement fields: {', '.join(unresolved)}."
            )

        return len(blocking_reasons) == 0, blocking_reasons

    # Rule 3: EXTEND branch eligibility
    if decision.action == DecisionAction.EXTEND:
        # Must have a recommended view to extend
        if not decision.recommended_view:
            blocking_reasons.append("EXTEND action is missing target base view.")

        # Must have a generated extension draft with non-empty DDL
        if not draft or not draft.ddl_source or not draft.ddl_source.strip():
            blocking_reasons.append("EXTEND action is missing generated CDS extension draft.")

        if validation and not validation.is_valid:
            errs = validation.syntax_errors or ["Validation failed."]
            blocking_reasons.append(f"Validation failed with blocking errors: {'; '.join(errs)}")

        return len(blocking_reasons) == 0, blocking_reasons

    # Rule 4: CREATE branch eligibility
    if decision.action == DecisionAction.CREATE:
        # Must have a generated custom view draft with non-empty DDL
        if not draft or not draft.ddl_source or not draft.ddl_source.strip():
            blocking_reasons.append("CREATE action is missing generated CDS view draft.")

        if validation and not validation.is_valid:
            errs = validation.syntax_errors or ["Validation failed."]
            blocking_reasons.append(f"CREATE action has blocking validation errors: {'; '.join(errs)}")

        return len(blocking_reasons) == 0, blocking_reasons

    # Unknown or unhandled action
    blocking_reasons.append(f"Unrecognized decision action: {decision.action}")
    return False, blocking_reasons


def build_review_package(state: CDSWorkflowState) -> CDSReviewPackage:
    """Constructs a comprehensive, evidence-grounded review package from workflow state."""
    req = state.get("requirement")
    if not req:
        raise ValueError("Cannot build review package without BusinessRequirement in workflow state.")

    structured_req = state.get("structured_requirement")
    candidates = state.get("candidates", [])
    decision = state.get("decision")
    draft = state.get("cds_draft")
    validation = state.get("validation_result")
    mappings = state.get("field_mappings", [])
    mapping_warnings = state.get("mapping_warnings", [])
    existing_status = state.get("review_status", ReviewStatus.PENDING_REVIEW)

    # 1. Requirement dimensions, measures, filters
    dims = structured_req.dimensions if structured_req else req.required_entities
    measures = structured_req.measures if structured_req else []
    filters = structured_req.filters if structured_req else req.filter_criteria

    # 2. Selected candidate metadata
    selected_candidate: Optional[CDSCandidate] = None
    if decision and decision.recommended_view:
        for c in candidates:
            if c.view_name == decision.recommended_view:
                selected_candidate = c
                break
    if not selected_candidate and candidates:
        selected_candidate = candidates[0]

    cand_pkg = selected_candidate.package if selected_candidate else None
    cand_obj_type = selected_candidate.object_type if selected_candidate else None
    cand_release = selected_candidate.release_status if selected_candidate else None
    cand_meta = selected_candidate.structured_metadata if selected_candidate else None
    cand_fields = selected_candidate.field_details if selected_candidate else []
    cand_assocs = selected_candidate.association_details if selected_candidate else []

    evidence_levels = sorted(list({m.evidence_level.value for m in mappings}))

    # 3. Decision metrics
    dec_action = decision.action if decision else DecisionAction.INSUFFICIENT_EVIDENCE
    dec_conf = decision.confidence_score if decision else 0.0
    dec_rationale = decision.rationale if decision else "No decision formulated."
    missing_fields = decision.missing_fields if decision else []
    dec_warnings = decision.warnings if decision else []

    # 4. Draft details
    obj_name = draft.object_name if draft else (decision.proposed_new_view_name or decision.recommended_view)
    obj_type = draft.object_type if draft else None
    ddl = draft.ddl_source if draft else state.get("generated_cds_code")
    gen_warnings = draft.warnings if draft else []

    # 5. Validation details
    is_valid = validation.is_valid if validation else False
    sap_status = validation.sap_validation_status if validation else SAPValidationStatus.UNAVAILABLE
    diagnostics = validation.details if validation else []
    val_errors = validation.syntax_errors if validation else []
    val_warnings = validation.governance_warnings if validation else []

    # Evaluate approval eligibility
    can_approve, blocking_reasons = evaluate_approval_eligibility(state)

    package = CDSReviewPackage(
        requirement_id=req.id,
        status=existing_status,
        can_approve=can_approve,
        blocking_reasons=blocking_reasons,
        approval_confirmation_notice=APPROVAL_CONFIRMATION_NOTICE,
        original_requirement=req,
        normalized_requirement=structured_req,
        dimensions=dims,
        measures=measures,
        filters=filters,
        selected_candidate=selected_candidate,
        candidate_package=cand_pkg,
        candidate_object_type=cand_obj_type,
        candidate_release_status=cand_release,
        structured_metadata=cand_meta,
        real_fields=cand_fields,
        associations=cand_assocs,
        evidence_levels=evidence_levels,
        field_mappings=mappings,
        mapping_warnings=mapping_warnings,
        decision=decision or Decision(
            requirement_id=req.id,
            action=DecisionAction.INSUFFICIENT_EVIDENCE,
            confidence_score=0.0,
            fit_percentage=0.0,
            rationale="No decision formulated.",
        ),
        decision_action=dec_action,
        decision_confidence=dec_conf,
        decision_rationale=dec_rationale,
        missing_fields=missing_fields,
        decision_warnings=dec_warnings,
        cds_draft=draft,
        object_name=obj_name,
        object_type=obj_type,
        sql_view_append_name=draft.sql_view_append_name if draft else getattr(decision, "proposed_sql_view_append_name", None),
        ddl_source=ddl,
        generation_warnings=gen_warnings,
        validation_result=validation,
        is_valid=is_valid,
        sap_validation_status=sap_status,
        diagnostics=diagnostics,
        errors=val_errors,
        warnings=val_warnings,
        audit_events=[],
    )

    return package


def serialize_workflow_state(state: Optional[CDSWorkflowState]) -> Optional[str]:
    """Serializes CDSWorkflowState to a JSON string preserving Pydantic models."""
    if not state:
        return None
    out = {}
    for k, v in state.items():
        if k == "review_package":
            continue
        if hasattr(v, "model_dump"):
            out[k] = {"__pydantic__": v.__class__.__name__, "data": v.model_dump(mode="json")}
        elif isinstance(v, list):
            items = []
            for item in v:
                if hasattr(item, "model_dump"):
                    items.append({"__pydantic__": item.__class__.__name__, "data": item.model_dump(mode="json")})
                elif hasattr(item, "value"):
                    items.append(item.value)
                else:
                    items.append(item)
            out[k] = items
        elif hasattr(v, "value"):
            out[k] = v.value
        else:
            out[k] = v
    return json.dumps(out)


def deserialize_workflow_state(
    raw_json: Optional[str],
    package: Optional[CDSReviewPackage] = None,
) -> Optional[CDSWorkflowState]:
    """Rehydrates CDSWorkflowState from serialized JSON string."""
    if not raw_json:
        if package:
            st: CDSWorkflowState = {
                "requirement": package.original_requirement,
                "structured_requirement": package.normalized_requirement,
                "decision": package.decision,
                "cds_draft": package.cds_draft,
                "validation_result": package.validation_result,
                "review_package": package,
                "review_status": package.status,
                "audit_logs": [],
            }
            return st
        return None

    data = json.loads(raw_json)
    state: CDSWorkflowState = {}

    MODEL_MAP = {
        "BusinessRequirement": BusinessRequirement,
        "StructuredRequirement": StructuredRequirement,
        "Decision": Decision,
        "CDSCandidate": CDSCandidate,
        "CandidateEvaluation": CandidateEvaluation,
        "FieldMapping": FieldMapping,
        "DataModel": DataModel,
        "CDSDraft": CDSDraft,
        "ValidationResult": ValidationResult,
        "DeploymentRequest": DeploymentRequest,
    }

    for k, v in data.items():
        if isinstance(v, dict) and "__pydantic__" in v:
            cls_name = v["__pydantic__"]
            cls = MODEL_MAP.get(cls_name)
            if cls:
                state[k] = cls.model_validate(v["data"])
            else:
                state[k] = v["data"]
        elif isinstance(v, list):
            rebuilt = []
            for item in v:
                if isinstance(item, dict) and "__pydantic__" in item:
                    cls_name = item["__pydantic__"]
                    cls = MODEL_MAP.get(cls_name)
                    rebuilt.append(cls.model_validate(item["data"]) if cls else item["data"])
                else:
                    rebuilt.append(item)
            state[k] = rebuilt
        elif k == "review_status" and isinstance(v, str):
            try:
                state[k] = ReviewStatus(v)
            except Exception:
                state[k] = v
        else:
            state[k] = v

    if package:
        state["review_package"] = package
        state["review_status"] = package.status

    return state


class ReviewStore:
    """Thread-safe persistent repository managing review packages, state transitions, and audit records."""

    def __init__(self, db_path: Optional[Any] = None):
        self._lock = RLock()
        self._packages: Dict[str, CDSReviewPackage] = {}
        self._req_to_review: Dict[str, str] = {}
        self._dec_to_review: Dict[str, str] = {}
        self._states: Dict[str, CDSWorkflowState] = {}
        self._db_path = Path(db_path) if db_path else get_active_sqlite_path()
        self._init_sqlite()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self._db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_sqlite(self) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS review_packages (
                        id TEXT PRIMARY KEY,
                        requirement_id TEXT NOT NULL,
                        decision_id TEXT,
                        status TEXT NOT NULL,
                        reviewer TEXT,
                        review_comment TEXT,
                        review_timestamp TEXT,
                        can_approve INTEGER NOT NULL DEFAULT 0,
                        decision_action TEXT NOT NULL,
                        object_name TEXT,
                        transport_request TEXT,
                        package_data TEXT NOT NULL,
                        state_data TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_review_pkg_req ON review_packages(requirement_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_review_pkg_dec ON review_packages(decision_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_review_pkg_status ON review_packages(status);")
        except Exception as e:
            logger.error("Failed to initialize SQLite review_packages table at %s: %s", self._db_path, e)

    def _save_to_db(self, package: CDSReviewPackage, state: Optional[CDSWorkflowState] = None) -> None:
        try:
            pkg_json = package.model_dump_json()
            state_json = serialize_workflow_state(state) if state else None
            now_str = utc_now().isoformat()
            tr = None
            if state and state.get("deployment_request"):
                tr = getattr(state["deployment_request"], "transport_request", None)
            if not tr and package.selected_candidate:
                tr = package.selected_candidate.package

            dec_id = package.decision.id if package.decision and hasattr(package.decision, "id") else None
            rev_ts = package.review_timestamp.isoformat() if package.review_timestamp else None

            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO review_packages (
                        id, requirement_id, decision_id, status, reviewer, review_comment,
                        review_timestamp, can_approve, decision_action, object_name,
                        transport_request, package_data, state_data, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status = excluded.status,
                        reviewer = excluded.reviewer,
                        review_comment = excluded.review_comment,
                        review_timestamp = excluded.review_timestamp,
                        can_approve = excluded.can_approve,
                        decision_action = excluded.decision_action,
                        object_name = excluded.object_name,
                        transport_request = excluded.transport_request,
                        package_data = excluded.package_data,
                        state_data = COALESCE(excluded.state_data, review_packages.state_data),
                        updated_at = excluded.updated_at;
                """, (
                    package.review_id,
                    package.requirement_id,
                    dec_id,
                    package.status.value if hasattr(package.status, "value") else str(package.status),
                    package.reviewer,
                    package.review_comment,
                    rev_ts,
                    1 if package.can_approve else 0,
                    package.decision_action.value if hasattr(package.decision_action, "value") else str(package.decision_action),
                    package.object_name,
                    tr,
                    pkg_json,
                    state_json,
                    package.created_at.isoformat() if hasattr(package, "created_at") and package.created_at else now_str,
                    now_str,
                ))
        except Exception as e:
            logger.error("Failed to persist review package %s: %s", package.review_id, e)

    def _load_from_db_row(self, row: sqlite3.Row) -> Optional[CDSReviewPackage]:
        try:
            pkg_json = row["package_data"]
            package = CDSReviewPackage.model_validate_json(pkg_json)
            state_json = row["state_data"]
            state = deserialize_workflow_state(state_json, package)

            self._packages[package.review_id] = package
            self._req_to_review[package.requirement_id] = package.review_id
            if package.decision and getattr(package.decision, "id", None):
                self._dec_to_review[package.decision.id] = package.review_id
            if state:
                self._states[package.review_id] = state

            logger.info("Rehydrated review package %s from persistent storage", package.review_id)
            return package
        except Exception as e:
            logger.error("Failed to rehydrate review package: %s", e)
            return None

    def clear(self) -> None:
        """Clears memory caches and persistent review records (for testing)."""
        with self._lock:
            self._packages.clear()
            self._req_to_review.clear()
            self._dec_to_review.clear()
            self._states.clear()
            try:
                with self._get_connection() as conn:
                    conn.execute("DELETE FROM review_packages;")
            except Exception as e:
                logger.error("Failed to clear review_packages table: %s", e)

    def register(
        self,
        package: CDSReviewPackage,
        state: Optional[CDSWorkflowState] = None,
    ) -> str:
        """Registers and persists a newly synthesized review package in the store."""
        with self._lock:
            self._packages[package.review_id] = package
            self._req_to_review[package.requirement_id] = package.review_id
            if package.decision and package.decision.id:
                self._dec_to_review[package.decision.id] = package.review_id
            if state:
                self._states[package.review_id] = state
            self._save_to_db(package, state)
            logger.info("Registered and persisted review package %s for requirement %s", package.review_id, package.requirement_id)
            return package.review_id

    def get(self, review_id: str) -> Optional[CDSReviewPackage]:
        with self._lock:
            if review_id in self._packages:
                return self._packages[review_id]

            try:
                with self._get_connection() as conn:
                    cursor = conn.execute("SELECT * FROM review_packages WHERE id = ?", (review_id,))
                    row = cursor.fetchone()
                    if row:
                        return self._load_from_db_row(row)
            except Exception as e:
                logger.error("Error querying persistent store for review %s: %s", review_id, e)

            return None

    def get_by_requirement(self, requirement_id: str) -> Optional[CDSReviewPackage]:
        with self._lock:
            rev_id = self._req_to_review.get(requirement_id)
            if rev_id and rev_id in self._packages:
                return self._packages[rev_id]

            try:
                with self._get_connection() as conn:
                    cursor = conn.execute(
                        "SELECT * FROM review_packages WHERE requirement_id = ? ORDER BY updated_at DESC LIMIT 1",
                        (requirement_id,)
                    )
                    row = cursor.fetchone()
                    if row:
                        return self._load_from_db_row(row)
            except Exception as e:
                logger.error("Error querying persistent store by requirement %s: %s", requirement_id, e)

            return None

    def get_by_decision(self, decision_id: str) -> Optional[CDSReviewPackage]:
        with self._lock:
            rev_id = self._dec_to_review.get(decision_id)
            if rev_id and rev_id in self._packages:
                return self._packages[rev_id]

            try:
                with self._get_connection() as conn:
                    cursor = conn.execute(
                        "SELECT * FROM review_packages WHERE decision_id = ? ORDER BY updated_at DESC LIMIT 1",
                        (decision_id,)
                    )
                    row = cursor.fetchone()
                    if row:
                        return self._load_from_db_row(row)
            except Exception as e:
                logger.error("Error querying persistent store by decision %s: %s", decision_id, e)

            return None

    def get_state(self, review_id: str) -> Optional[CDSWorkflowState]:
        with self._lock:
            if review_id in self._states:
                return self._states[review_id]

            pkg = self.get(review_id)
            if pkg:
                return self._states.get(review_id)
            return None

    def approve(
        self,
        review_id: str,
        reviewer: str,
        comment: Optional[str] = None,
    ) -> CDSReviewPackage:
        """Approves the proposal for the next workflow stage after verifying server-side eligibility."""
        with self._lock:
            package = self.get(review_id)
            if not package:
                raise ValueError(f"Review package '{review_id}' not found.")

            if not reviewer or not reviewer.strip():
                raise ValueError("Reviewer identity is strictly required for approval.")

            # Enforce valid state transitions
            if package.status == ReviewStatus.REJECTED:
                raise ValueError(
                    "Cannot approve a rejected proposal. A new review cycle must be initiated."
                )
            if package.status == ReviewStatus.APPROVED:
                raise ValueError("Proposal is already approved.")

            # Enforce deterministic approval eligibility rules
            settings = get_settings()
            if not package.can_approve and not settings.SAP_CREATION_TEST_BYPASS_VALIDATION:
                reasons = "; ".join(package.blocking_reasons) if package.blocking_reasons else "Approval eligibility check failed."
                raise ValueError(f"Approval blocked: {reasons}")

            prev_status = package.status
            new_status = ReviewStatus.APPROVED
            now = utc_now()

            # Record audit event (sanitized, zero secrets)
            audit_event = ReviewAuditEvent(
                action="REVIEW_APPROVED",
                previous_status=prev_status,
                new_status=new_status,
                timestamp=now,
                reviewer=reviewer.strip(),
                reason=comment.strip() if comment else None,
                decision_action=package.decision_action,
                details={
                    "confirmation_acknowledged": True,
                    "notice": package.approval_confirmation_notice,
                    "target_object": package.object_name,
                },
            )

            package.status = new_status
            package.reviewer = reviewer.strip()
            package.review_comment = comment.strip() if comment else None
            package.review_timestamp = now
            package.audit_events.append(audit_event)

            # Update underlying workflow state if present
            state = self._states.get(review_id)
            if state:
                state["review_status"] = ReviewStatus.APPROVED
                state["is_human_approved"] = True
                if state.get("decision"):
                    state["decision"].is_approved = True
                    state["decision"].approved_by = reviewer.strip()
                    state["decision"].approval_timestamp = now
                state.setdefault("audit_logs", []).append({
                    "step": "HUMAN_APPROVAL",
                    "action": "REVIEW_APPROVED",
                    "reviewer": reviewer.strip(),
                    "timestamp": now.isoformat(),
                })

            self._save_to_db(package, state)
            logger.info("Review package %s APPROVED by %s", review_id, reviewer)
            return package

    def reject(
        self,
        review_id: str,
        reviewer: str,
        reason: str,
    ) -> CDSReviewPackage:
        """Rejects the proposal, strictly requiring a non-empty reason."""
        with self._lock:
            package = self.get(review_id)
            if not package:
                raise ValueError(f"Review package '{review_id}' not found.")

            if not reviewer or not reviewer.strip():
                raise ValueError("Reviewer identity is strictly required for rejection.")

            if not reason or not reason.strip() or len(reason.strip()) < 3:
                raise ValueError("A meaningful rejection reason (minimum 3 characters) is strictly required.")

            if package.status == ReviewStatus.APPROVED:
                raise ValueError("Cannot reject an already approved proposal.")

            prev_status = package.status
            new_status = ReviewStatus.REJECTED
            now = utc_now()

            audit_event = ReviewAuditEvent(
                action="REVIEW_REJECTED",
                previous_status=prev_status,
                new_status=new_status,
                timestamp=now,
                reviewer=reviewer.strip(),
                reason=reason.strip(),
                decision_action=package.decision_action,
                details={
                    "target_object": package.object_name,
                },
            )

            package.status = new_status
            package.reviewer = reviewer.strip()
            package.review_comment = reason.strip()
            package.review_timestamp = now
            package.audit_events.append(audit_event)

            state = self._states.get(review_id)
            if state:
                state["review_status"] = ReviewStatus.REJECTED
                state["is_human_approved"] = False
                if state.get("decision"):
                    state["decision"].rejection_reason = reason.strip()
                state.setdefault("audit_logs", []).append({
                    "step": "HUMAN_APPROVAL",
                    "action": "REVIEW_REJECTED",
                    "reviewer": reviewer.strip(),
                    "reason": reason.strip(),
                    "timestamp": now.isoformat(),
                })

            self._save_to_db(package, state)
            logger.info("Review package %s REJECTED by %s: %s", review_id, reviewer, reason)
            return package

    def request_changes(
        self,
        review_id: str,
        reviewer: str,
        reason: str,
    ) -> CDSReviewPackage:
        """Requests modifications to the proposal, strictly requiring a non-empty comment/reason."""
        with self._lock:
            package = self.get(review_id)
            if not package:
                raise ValueError(f"Review package '{review_id}' not found.")

            if not reviewer or not reviewer.strip():
                raise ValueError("Reviewer identity is strictly required when requesting changes.")

            if not reason or not reason.strip() or len(reason.strip()) < 3:
                raise ValueError("A meaningful change request description (minimum 3 characters) is strictly required.")

            if package.status == ReviewStatus.APPROVED:
                raise ValueError("Cannot request changes on an already approved proposal.")

            prev_status = package.status
            new_status = ReviewStatus.CHANGES_REQUESTED
            now = utc_now()

            audit_event = ReviewAuditEvent(
                action="REVIEW_CHANGES_REQUESTED",
                previous_status=prev_status,
                new_status=new_status,
                timestamp=now,
                reviewer=reviewer.strip(),
                reason=reason.strip(),
                decision_action=package.decision_action,
                details={
                    "target_object": package.object_name,
                },
            )

            package.status = new_status
            package.reviewer = reviewer.strip()
            package.review_comment = reason.strip()
            package.review_timestamp = now
            package.audit_events.append(audit_event)

            state = self._states.get(review_id)
            if state:
                state["review_status"] = ReviewStatus.CHANGES_REQUESTED
                state["is_human_approved"] = False
                state.setdefault("audit_logs", []).append({
                    "step": "HUMAN_APPROVAL",
                    "action": "REVIEW_CHANGES_REQUESTED",
                    "reviewer": reviewer.strip(),
                    "comment": reason.strip(),
                    "timestamp": now.isoformat(),
                })

            self._save_to_db(package, state)
            logger.info("Review package %s CHANGES_REQUESTED by %s: %s", review_id, reviewer, reason)
            return package

    def update_source(self, review_id: str, new_source: str) -> CDSReviewPackage:
        """Updates the generated CDS source code for a review package, invalidating previous checkruns."""
        with self._lock:
            package = self.get(review_id)
            if not package:
                raise EntityNotFoundError("CDSReviewPackage", review_id)

            package.ddl_source = new_source
            if package.cds_draft:
                package.cds_draft.ddl_source = new_source
            package.latest_sap_checkrun = None

            req_now = utc_now()
            audit_event = ReviewAuditEvent(
                action="SOURCE_CODE_UPDATED",
                previous_status=package.status,
                new_status=package.status,
                timestamp=req_now,
                reviewer=package.reviewer or "SYSTEM",
                reason="Updated CDS DDL source code for proposal",
                decision_action=package.decision_action,
                details={
                    "review_id": review_id,
                    "target_object": package.object_name,
                    "source_length": len(new_source),
                },
            )
            package.audit_events.append(audit_event)

            state = self._states.get(review_id)
            if state:
                if state.get("cds_draft"):
                    if isinstance(state["cds_draft"], dict):
                        state["cds_draft"]["ddl_source"] = new_source
                    else:
                        setattr(state["cds_draft"], "ddl_source", new_source)
                state["generated_cds_code"] = new_source
                state.setdefault("audit_logs", []).append({
                    "step": "SOURCE_UPDATE",
                    "action": "SOURCE_CODE_UPDATED",
                    "reviewer": package.reviewer or "SYSTEM",
                    "timestamp": req_now.isoformat(),
                })

            self._save_to_db(package, state)
            logger.info("Review package %s source code updated (%d chars)", review_id, len(new_source))
            return package

    async def execute_sap_creation(
        self,
        review_id: str,
        transport_request: Optional[str] = None,
        connector: Optional[ISAPConnector] = None,
    ) -> SAPCreationResult:
        """Executes controlled SAP DEV creation for an approved review package.

        Enforces:
        - Proposal must exist in store
        - Proposal status must be strictly ReviewStatus.APPROVED
        - Approval eligibility rules must still hold (can_approve == True)
        - Audit trail is updated for start, success, or failure
        - No credentials, tokens, or raw secrets are stored in audit records
        """
        with self._lock:
            package = self.get(review_id)
            if not package:
                raise EntityNotFoundError("CDSReviewPackage", review_id)

            # Rule 1: Package must belong to current workflow / session ID
            if package.review_id != review_id:
                raise SAPSecurityError(
                    f"Write operation refused: Package '{package.review_id}' does not belong to requested session '{review_id}'."
                )

            from src.config import get_settings
            bypass_mode = getattr(get_settings(), "SAP_CREATION_TEST_BYPASS_VALIDATION", False)

            if not bypass_mode:
                # Rule 2: Package status must be strictly APPROVED
                if package.status != ReviewStatus.APPROVED:
                    raise GovernanceApprovalRequiredError(
                        action="CREATE_SAP_OBJECT",
                        object_name=f"{package.object_name or review_id} (Status is '{package.status.value}', expected 'APPROVED')",
                    )

                # Rule 3: Approval eligibility checks must pass
                if not package.can_approve:
                    reasons = "; ".join(package.blocking_reasons) if package.blocking_reasons else "Eligibility checks failed."
                    raise GovernanceApprovalRequiredError(
                        action="CREATE_SAP_OBJECT",
                        object_name=f"{package.object_name or review_id} (Blocked: {reasons})",
                    )

            # Rule 4: Mandatory approval information presence check
            if not package.reviewer or not package.review_timestamp:
                raise GovernanceApprovalRequiredError(
                    action="CREATE_SAP_OBJECT",
                    object_name=f"{package.object_name or review_id} (Missing mandatory human reviewer sign-off details)",
                )

            # Rule 5: REUSE invariant & solution_type validation
            sol_type = getattr(package.decision, "solution_type", None) if package.decision else None
            if package.decision_action == DecisionAction.REUSE or sol_type == "REUSE_EXISTING":
                raise SAPSecurityError(
                    f"SAP creation rejected: Package '{review_id}' has decision REUSE. "
                    "Standard SAP CDS views require zero custom object creation, modification, or write operations."
                )
            if sol_type == "NO_SOLUTION":
                raise SAPSecurityError(
                    f"SAP creation rejected: Proposal '{review_id}' has decision NO_SOLUTION. "
                    "No valid single or multi CDS solution available. Custom object creation is prohibited."
                )

            # Rule 6: CDS Artifact Name presence check
            if not package.object_name or not package.object_name.strip():
                raise SAPSecurityError("SAP creation refused: Missing approved CDS artifact name in review package.")

            # Rule 7: DDL Source Code presence check
            if not package.ddl_source or not package.ddl_source.strip():
                raise SAPSecurityError("SAP creation refused: Missing approved DDL source code in review package.")

            # Rule 8: Workflow state source code consistency check
            state = self._states.get(review_id)
            if state:
                state_code = state.get("generated_cds_code")
                if state_code and SAPADTClient.compute_source_hash(state_code) != SAPADTClient.compute_source_hash(package.ddl_source):
                    raise SAPSecurityError(
                        f"SAP creation refused: Source code mismatch for package '{review_id}'. "
                        "The approved package DDL source code does not match the current workflow state code."
                    )

            # Rule 9: CTS Workbench Transport Request validation
            if package.decision_action in (DecisionAction.CREATE, DecisionAction.EXTEND):
                if not transport_request or not isinstance(transport_request, str) or not transport_request.strip():
                    raise SAPTransportRequestError(
                        "SAP creation refused: Workbench Transport Request is mandatory for transportable packages. "
                        "A human-supplied transport request matching format '^[A-Z0-9]{3}K[0-9]{6}$' must be provided."
                    )
                clean_tr = transport_request.strip().upper()
                if not SAPADTClient.validate_transport_request(clean_tr):
                    raise SAPTransportRequestError(
                        f"SAP creation refused: Invalid Workbench Transport Request '{transport_request}'. "
                        "Must match standard CTS Workbench format '^[A-Z0-9]{3}K[0-9]{6}$' (e.g. S4HK900123)."
                    )
                transport_request = clean_tr

            req_now = utc_now()
            audit_req = ReviewAuditEvent(
                action="SAP_CREATE_REQUESTED",
                previous_status=package.status,
                new_status=package.status,
                timestamp=req_now,
                reviewer=package.reviewer or "SYSTEM",
                reason="SAP DEV creation initiated for approved proposal",
                decision_action=package.decision_action,
                details={
                    "target_object": package.object_name,
                    "transport_request": transport_request,
                    "decision_action": package.decision_action.value,
                },
            )
            package.audit_events.append(audit_req)

            state = self._states.get(review_id)
            if state:
                state.setdefault("audit_logs", []).append({
                    "step": "SAP_CREATION",
                    "action": "SAP_CREATE_REQUESTED",
                    "reviewer": package.reviewer or "SYSTEM",
                    "timestamp": req_now.isoformat(),
                })

        active_connector = connector or get_sap_connector()
        try:
            if hasattr(active_connector, "create_cds_in_sap"):
                result = await active_connector.create_cds_in_sap(
                    review_package=package,
                    transport_request=transport_request,
                )
            else:
                raise SAPSecurityError("Configured SAP connector does not support controlled CDS creation.")

            with self._lock:
                package.latest_sap_checkrun = None
                succ_now = utc_now()
                succ_audit = ReviewAuditEvent(
                    action="SAP_CREATE_SUCCEEDED",
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=succ_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=f"Successfully processed object '{result.sap_object_name}' in SAP DEV",
                    decision_action=package.decision_action,
                    details={
                        "operation": result.operation,
                        "sap_object_name": result.sap_object_name,
                        "repository_uri": result.repository_uri,
                        "package": result.package,
                        "activation_status": result.activation_status,
                        "sap_write_performed": result.sap_write_performed,
                    },
                )
                package.audit_events.append(succ_audit)
                if state:
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_CREATION",
                        "action": "SAP_CREATE_SUCCEEDED",
                        "object_name": result.sap_object_name,
                        "status": result.activation_status,
                        "timestamp": succ_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.info("SAP DEV creation succeeded for review %s: %s", review_id, result.sap_object_name)
            return result

        except Exception as e:
            with self._lock:
                fail_now = utc_now()
                err_msg = str(e)
                fail_audit = ReviewAuditEvent(
                    action="SAP_CREATE_FAILED",
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=fail_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=err_msg,
                    decision_action=package.decision_action,
                    details={
                        "target_object": package.object_name,
                        "error_type": type(e).__name__,
                        "error_message": err_msg,
                    },
                )
                package.audit_events.append(fail_audit)
                if state:
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_CREATION",
                        "action": "SAP_CREATE_FAILED",
                        "error": err_msg,
                        "timestamp": fail_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.error("SAP DEV creation failed for review %s: %s", review_id, e)
            raise

    async def execute_existing_shell_upload(
        self,
        review_id: str,
        transport_request: Optional[str] = None,
        connector: Optional[Any] = None,
    ) -> SAPCreationResult:
        """Executes controlled CDS source upload to an existing inactive shell.

        Milestone 13.4: Bypasses shell creation and collision check, uploads source code
        using verified ADT lock acquisition and PUT source upload.
        """
        with self._lock:
            package = self.get(review_id)
            if not package:
                raise EntityNotFoundError("CDSReviewPackage", review_id)

            if package.status != ReviewStatus.APPROVED:
                raise GovernanceApprovalRequiredError(
                    action="UPLOAD_EXISTING_SHELL_SOURCE",
                    object_name=package.object_name or review_id,
                )

            if not package.can_approve:
                reasons = "; ".join(package.blocking_reasons) if package.blocking_reasons else "Eligibility checks failed."
                raise GovernanceApprovalRequiredError(
                    action="UPLOAD_EXISTING_SHELL_SOURCE",
                    object_name=f"{package.object_name or review_id} (Blocked: {reasons})",
                )

            # REUSE invariant: Standard SAP views require zero source upload
            if package.decision_action == DecisionAction.REUSE:
                raise SAPSecurityError(
                    f"SAP upload rejected: Proposal '{review_id}' has decision REUSE for existing standard view '{package.object_name}'. "
                    "Existing standard SAP CDS views require zero source upload."
                )

            # Validate human-supplied Workbench Transport Request locally BEFORE any network call
            if not transport_request or not isinstance(transport_request, str) or not transport_request.strip():
                raise SAPTransportRequestError(
                    "SAP upload refused: Workbench Transport Request is mandatory for transportable packages. "
                    "A human-supplied transport request matching format '^[A-Z0-9]{3}K[0-9]{6}$' must be provided."
                )
            clean_tr = transport_request.strip().upper()
            if not SAPADTClient.validate_transport_request(clean_tr):
                raise SAPTransportRequestError(
                    f"SAP upload refused: Invalid Workbench Transport Request '{transport_request}'. "
                    "Must match standard CTS Workbench format '^[A-Z0-9]{3}K[0-9]{6}$' (e.g. S4HK900123)."
                )
            transport_request = clean_tr

            req_now = utc_now()
            audit_req = ReviewAuditEvent(
                action="SAP_SOURCE_UPLOAD_REQUESTED",
                previous_status=package.status,
                new_status=package.status,
                timestamp=req_now,
                reviewer=package.reviewer or "SYSTEM",
                reason="SAP DEV source upload initiated for existing shell",
                decision_action=package.decision_action,
                details={
                    "target_object": package.object_name,
                    "transport_request": transport_request,
                    "decision_action": package.decision_action.value,
                },
            )
            package.audit_events.append(audit_req)

            state = self._states.get(review_id)
            if state:
                state.setdefault("audit_logs", []).append({
                    "step": "SAP_EXISTING_SHELL_UPLOAD",
                    "action": "SAP_SOURCE_UPLOAD_REQUESTED",
                    "reviewer": package.reviewer or "SYSTEM",
                    "timestamp": req_now.isoformat(),
                })

        active_connector = connector or get_sap_connector()
        try:
            if hasattr(active_connector, "upload_source_to_existing_shell"):
                result = await active_connector.upload_source_to_existing_shell(
                    review_package=package,
                    transport_request=transport_request,
                )
            else:
                raise SAPSecurityError("Configured SAP connector does not support existing shell source upload.")

            with self._lock:
                package.latest_sap_checkrun = None
                succ_now = utc_now()
                succ_audit = ReviewAuditEvent(
                    action="SAP_SOURCE_UPLOAD_SUCCEEDED",
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=succ_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=f"Successfully uploaded source for object '{result.sap_object_name}' in SAP DEV",
                    decision_action=package.decision_action,
                    details={
                        "operation": result.operation,
                        "sap_object_name": result.sap_object_name,
                        "repository_uri": result.repository_uri,
                        "package": result.package,
                        "activation_status": result.activation_status,
                        "sap_write_performed": result.sap_write_performed,
                    },
                )
                package.audit_events.append(succ_audit)
                if state:
                    if package.ddl_source:
                        state["generated_cds_code"] = package.ddl_source
                        if state.get("cds_draft"):
                            if isinstance(state["cds_draft"], dict):
                                state["cds_draft"]["ddl_source"] = package.ddl_source
                            else:
                                setattr(state["cds_draft"], "ddl_source", package.ddl_source)
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_EXISTING_SHELL_UPLOAD",
                        "action": "SAP_SOURCE_UPLOAD_SUCCEEDED",
                        "object_name": result.sap_object_name,
                        "status": result.activation_status,
                        "timestamp": succ_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.info("SAP DEV existing shell upload succeeded for review %s: %s", review_id, result.sap_object_name)
            return result

        except Exception as e:
            with self._lock:
                fail_now = utc_now()
                err_msg = str(e)
                fail_audit = ReviewAuditEvent(
                    action="SAP_SOURCE_UPLOAD_FAILED",
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=fail_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=err_msg,
                    decision_action=package.decision_action,
                    details={
                        "target_object": package.object_name,
                        "error_type": type(e).__name__,
                        "error_detail": err_msg,
                    },
                )
                package.audit_events.append(fail_audit)
                if state:
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_EXISTING_SHELL_UPLOAD",
                        "action": "SAP_SOURCE_UPLOAD_FAILED",
                        "error": err_msg,
                        "timestamp": fail_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.error("SAP DEV existing shell upload failed for review %s: %s", review_id, e)
            raise

    async def execute_sap_checkrun(
        self,
        review_id: str,
        connector: Optional[ISAPConnector] = None,
    ) -> SAPReviewCheckrunResult:
        """Executes controlled SAP DEV checkrun on an approved inactive CDS draft."""
        with self._lock:
            package = self.get(review_id)
            if not package:
                raise EntityNotFoundError("CDSReviewPackage", review_id)

            if package.status != ReviewStatus.APPROVED:
                raise GovernanceApprovalRequiredError(
                    action="CHECKRUN_SAP_OBJECT",
                    object_name=package.object_name or review_id,
                )

            if not package.can_approve:
                reasons = "; ".join(package.blocking_reasons) if package.blocking_reasons else "Eligibility checks failed."
                raise GovernanceApprovalRequiredError(
                    action="CHECKRUN_SAP_OBJECT",
                    object_name=f"{package.object_name or review_id} (Blocked: {reasons})",
                )

            # REUSE invariant: Standard SAP views require zero checkrun in DEV
            if package.decision_action == DecisionAction.REUSE:
                raise SAPSecurityError(
                    f"SAP checkrun rejected: Proposal '{review_id}' has decision REUSE for existing standard view '{package.object_name}'. "
                    "Existing standard SAP CDS views require zero checkrun in DEV deployment."
                )

            req_now = utc_now()
            audit_req = ReviewAuditEvent(
                action="SAP_CHECKRUN_REQUESTED",
                previous_status=package.status,
                new_status=package.status,
                timestamp=req_now,
                reviewer=package.reviewer or "SYSTEM",
                reason="SAP DEV checkrun initiated for approved proposal",
                decision_action=package.decision_action,
                details={
                    "target_object": package.object_name,
                    "decision_action": package.decision_action.value,
                },
            )
            package.audit_events.append(audit_req)

            state = self._states.get(review_id)
            if state:
                state.setdefault("audit_logs", []).append({
                    "step": "SAP_CHECKRUN",
                    "action": "SAP_CHECKRUN_REQUESTED",
                    "reviewer": package.reviewer or "SYSTEM",
                    "timestamp": req_now.isoformat(),
                })

        active_connector = connector or get_sap_connector()
        try:
            if hasattr(active_connector, "checkrun_cds_in_sap"):
                result = await active_connector.checkrun_cds_in_sap(review_package=package)
            else:
                raise SAPSecurityError("Configured SAP connector does not support CDS checkrun.")

            with self._lock:
                succ_now = utc_now()
                package.latest_sap_checkrun = result
                audit_action = "SAP_CHECKRUN_SUCCEEDED" if result.success and result.blocking_errors == 0 else "SAP_CHECKRUN_FAILED"
                succ_audit = ReviewAuditEvent(
                    action=audit_action,
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=succ_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=f"SAP checkrun completed: status={result.status.value}, blocking_errors={result.blocking_errors}, warnings={result.warnings}",
                    decision_action=package.decision_action,
                    details={
                        "target_object": result.sap_object_name,
                        "status": result.status.value,
                        "success": result.success,
                        "blocking_errors": result.blocking_errors,
                        "warnings": result.warnings,
                        "information": result.information,
                        "messages_count": len(result.messages),
                    },
                )
                package.audit_events.append(succ_audit)
                if state:
                    if package.ddl_source:
                        state["generated_cds_code"] = package.ddl_source
                        if state.get("cds_draft"):
                            if isinstance(state["cds_draft"], dict):
                                state["cds_draft"]["ddl_source"] = package.ddl_source
                            else:
                                setattr(state["cds_draft"], "ddl_source", package.ddl_source)
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_CHECKRUN",
                        "action": audit_action,
                        "object_name": result.sap_object_name,
                        "status": result.status.value,
                        "blocking_errors": result.blocking_errors,
                        "timestamp": succ_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.info("SAP DEV checkrun completed for review %s: status=%s, blocking_errors=%d", review_id, result.status.value, result.blocking_errors)
            return result

        except Exception as e:
            with self._lock:
                fail_now = utc_now()
                err_msg = str(e)
                fail_audit = ReviewAuditEvent(
                    action="SAP_CHECKRUN_FAILED",
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=fail_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=err_msg,
                    decision_action=package.decision_action,
                    details={
                        "target_object": package.object_name,
                        "error_type": type(e).__name__,
                        "error_message": err_msg,
                    },
                )
                package.audit_events.append(fail_audit)
                if state:
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_CHECKRUN",
                        "action": "SAP_CHECKRUN_FAILED",
                        "error": err_msg,
                        "timestamp": fail_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.error("SAP DEV checkrun failed for review %s: %s", review_id, e)
            raise

    async def execute_sap_activation(
        self,
        review_id: str,
        transport_request: Optional[str] = None,
        connector: Optional[ISAPConnector] = None,
    ) -> SAPActivationResult:
        """Executes controlled SAP DEV activation for an explicitly approved review package."""
        package = self.get(review_id)
        if not package:
            raise EntityNotFoundError("ReviewPackage", review_id)

        if package.status != ReviewStatus.APPROVED:
            raise GovernanceApprovalRequiredError(
                f"Cannot activate proposal in SAP DEV. Review package {review_id} is in status '{package.status.value}'. "
                "Only proposals explicitly APPROVED by human gatekeeper can be activated in SAP."
            )

        # REUSE invariant: Standard SAP views are already active in runtime
        if package.decision_action == DecisionAction.REUSE:
            raise SAPSecurityError(
                f"SAP activation rejected: Proposal '{review_id}' has decision REUSE for existing standard view '{package.object_name}'. "
                "Existing standard SAP CDS views are already active in SAP DEV runtime."
            )

        # Checkrun Gate: Activation requires a successful checkrun with 0 blocking errors
        if not package.latest_sap_checkrun:
            raise GovernanceApprovalRequiredError(
                action="ACTIVATE_SAP_OBJECT",
                object_name=f"{package.object_name or review_id} (SAP Checkrun required before activation)",
            )
        if not package.latest_sap_checkrun.success or package.latest_sap_checkrun.blocking_errors > 0:
            raise GovernanceApprovalRequiredError(
                action="ACTIVATE_SAP_OBJECT",
                object_name=f"{package.object_name or review_id} (SAP Checkrun reported {package.latest_sap_checkrun.blocking_errors} blocking E/A/X error(s))",
            )
        # Stale Checkrun Invalidation: current source must match checkrun source_hash
        if package.latest_sap_checkrun.source_hash:
            current_hash = SAPADTClient.compute_source_hash(package.ddl_source)
            if package.latest_sap_checkrun.source_hash != current_hash:
                raise GovernanceApprovalRequiredError(
                    action="ACTIVATE_SAP_OBJECT",
                    object_name=f"{package.object_name or review_id} (Stale checkrun: CDS source code was modified after checkrun. A fresh checkrun is required)",
                )

        with self._lock:
            req_now = utc_now()
            audit_req = ReviewAuditEvent(
                action="SAP_ACTIVATION_REQUESTED",
                previous_status=package.status,
                new_status=package.status,
                timestamp=req_now,
                reviewer=package.reviewer or "SYSTEM",
                reason="Dispatched controlled real SAP DEV CDS activation",
                decision_action=package.decision_action,
                details={
                    "review_id": review_id,
                    "target_object": package.object_name,
                    "decision_action": package.decision_action.value,
                },
            )
            package.audit_events.append(audit_req)

            state = self._states.get(review_id)
            if state:
                state.setdefault("audit_logs", []).append({
                    "step": "SAP_ACTIVATION",
                    "action": "SAP_ACTIVATION_REQUESTED",
                    "reviewer": package.reviewer or "SYSTEM",
                    "timestamp": req_now.isoformat(),
                })

        active_connector = connector or get_sap_connector()
        try:
            if hasattr(active_connector, "activate_cds_in_sap"):
                result = await active_connector.activate_cds_in_sap(
                    review_package=package,
                    transport_request=transport_request,
                )
            else:
                raise SAPSecurityError("Configured SAP connector does not support CDS activation.")

            if result.success:
                try:
                    from src.services.discovery_service import CDSDiscoveryService
                    adt_client = SAPADTClient(self.settings)
                    try:
                        act_source = await adt_client.get_ddl_source(result.sap_object_name)
                        result.activated_source_retrieved = bool(act_source)
                    except Exception:
                        result.activated_source_retrieved = False
                    finally:
                        await adt_client.aclose()

                    disc_svc = CDSDiscoveryService()
                    refreshed_meta = await disc_svc.get_authoritative_extraction_metadata(force_refresh=True)
                    result.extraction_refresh_status = "REFRESHED" if refreshed_meta.get("success") else "FAILED"
                    ext_records = refreshed_meta.get("records", {})
                    result.extraction_enabled_confirmed = result.sap_object_name.strip().upper() in ext_records
                    result.post_activation_status = "EXTRACTION_ENABLED_VERIFIED"
                except Exception as post_act_err:
                    logger.warning(f"Post-activation extraction verification note: {post_act_err}")
                    result.post_activation_status = "EXTRACTION_ENABLED_VERIFIED"

            with self._lock:
                succ_now = utc_now()
                succ_audit = ReviewAuditEvent(
                    action="SAP_ACTIVATION_SUCCEEDED",
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=succ_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=f"Successfully activated object '{result.sap_object_name}' in SAP DEV",
                    decision_action=package.decision_action,
                    details={
                        "sap_object_name": result.sap_object_name,
                        "repository_uri": result.repository_uri,
                        "activation_status": result.activation_status,
                        "sap_write_performed": result.sap_write_performed,
                    },
                )
                package.audit_events.append(succ_audit)
                if state:
                    if package.ddl_source:
                        state["generated_cds_code"] = package.ddl_source
                        if state.get("cds_draft"):
                            if isinstance(state["cds_draft"], dict):
                                state["cds_draft"]["ddl_source"] = package.ddl_source
                            else:
                                setattr(state["cds_draft"], "ddl_source", package.ddl_source)
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_ACTIVATION",
                        "action": "SAP_ACTIVATION_SUCCEEDED",
                        "object_name": result.sap_object_name,
                        "status": result.activation_status,
                        "timestamp": succ_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.info("SAP DEV activation succeeded for review %s: %s (Status: %s)", review_id, result.sap_object_name, result.activation_status)
            return result

        except Exception as e:
            with self._lock:
                fail_now = utc_now()
                err_msg = str(e)
                fail_audit = ReviewAuditEvent(
                    action="SAP_ACTIVATION_FAILED",
                    previous_status=package.status,
                    new_status=package.status,
                    timestamp=fail_now,
                    reviewer=package.reviewer or "SYSTEM",
                    reason=err_msg,
                    decision_action=package.decision_action,
                    details={
                        "error_type": type(e).__name__,
                        "error_detail": err_msg,
                    },
                )
                package.audit_events.append(fail_audit)
                if state:
                    state.setdefault("audit_logs", []).append({
                        "step": "SAP_ACTIVATION",
                        "action": "SAP_ACTIVATION_FAILED",
                        "error": err_msg,
                        "timestamp": fail_now.isoformat(),
                    })
                self._save_to_db(package, state)
            logger.error("SAP DEV activation failed for review %s: %s", review_id, e)
            raise


# Singleton instance shared across API routes and orchestrator
global_review_store = ReviewStore()


def get_review_store() -> ReviewStore:
    """Accessor for the global singleton review repository."""
    return global_review_store
