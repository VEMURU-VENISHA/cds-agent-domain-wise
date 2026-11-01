"""Enterprise Domain Models for SAP CDS Intelligence & Automation Platform.

Contains core Pydantic domain models:
- BusinessRequirement
- CDSCandidate
- FieldMapping
- Decision
- DataModel
- ValidationResult
- DeploymentRequest
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict
import uuid
from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ==============================================================================
# 1. Enums
# ==============================================================================

class TargetUseCase(str, Enum):
    ANALYTICAL_REPORT = "ANALYTICAL_REPORT"
    TRANSACTIONAL_APP = "TRANSACTIONAL_APP"
    INTEGRATION_API = "INTEGRATION_API"
    DATA_EXTRACTION = "DATA_EXTRACTION"
    SEARCH_MODEL = "SEARCH_MODEL"


class CDSLayer(str, Enum):
    BASIC = "BASIC"                  # Core interface view directly on tables (I_*)
    COMPOSITE = "COMPOSITE"          # Joins/unions across multiple interface views (I_*)
    CONSUMPTION = "CONSUMPTION"      # High-level consumption / analytical query (C_*)
    EXTENSION = "EXTENSION"          # CDS view entity extension (E_*)


class MappingType(str, Enum):
    DIRECT_MATCH = "DIRECT_MATCH"
    ASSOCIATION_HOP = "ASSOCIATION_HOP"
    CALCULATED = "CALCULATED"
    UNRESOLVED = "UNRESOLVED"
    MISSING = "MISSING"


class EvidenceLevel(str, Enum):
    LEVEL_1_EXACT_FIELD = "LEVEL_1_EXACT_FIELD"
    LEVEL_2_LABEL_MATCH = "LEVEL_2_LABEL_MATCH"
    LEVEL_3_VERIFIED_ASSOCIATION = "LEVEL_3_VERIFIED_ASSOCIATION"
    LEVEL_4_HEURISTIC_SYNONYM = "LEVEL_4_HEURISTIC_SYNONYM"
    LEVEL_5_UNVERIFIED_SUGGESTION = "LEVEL_5_UNVERIFIED_SUGGESTION"
    LEVEL_6_MISSING = "LEVEL_6_MISSING"


class DecisionAction(str, Enum):
    REUSE = "REUSE"                          # Standard SAP CDS view satisfies 100% of concepts
    EXTEND = "EXTEND"                        # Missing concepts verified obtainable via extension
    CREATE = "CREATE"                        # Custom CDS required or 0 suitable views
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # Ambiguous/incomplete metadata halts decision


class CDSConstructType(str, Enum):
    """Authoritative base CDS construct type verified from DDL source or SAP metadata."""

    CLASSIC_VIEW = "CLASSIC_VIEW"  # define view ... (classic CDS view requiring extend view with <name>)
    VIEW_ENTITY = "VIEW_ENTITY"    # define view entity ... (modern CDS view entity requiring extend view entity with)
    UNKNOWN = "UNKNOWN"            # Unverified construct type; must halt with INSUFFICIENT_EVIDENCE



class DeploymentStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DRAFT_CREATED = "DRAFT_CREATED"
    ACTIVATED = "ACTIVATED"
    FAILED = "FAILED"


class CDSClassificationType(str, Enum):
    BASIC = "BASIC"
    COMPOSITE = "COMPOSITE"
    CONSUMPTION = "CONSUMPTION"
    PARAMETERIZED = "PARAMETERIZED"
    UNKNOWN = "UNKNOWN"


class WorkflowStatus(str, Enum):
    EXISTING_EXTRACTION_ENABLED_CDS = "EXISTING_EXTRACTION_ENABLED_CDS"
    BASIC_CDS_DISCOVERY_COMPLETED = "BASIC_CDS_DISCOVERY_COMPLETED"
    SOURCE_SELECTION_PENDING = "SOURCE_SELECTION_PENDING"
    JOIN_CONFIGURATION_PENDING = "JOIN_CONFIGURATION_PENDING"
    JOIN_CONFIGURATION_APPROVED = "JOIN_CONFIGURATION_APPROVED"
    WRAPPER_GENERATED = "WRAPPER_GENERATED"
    STATIC_VALIDATION_PASSED = "STATIC_VALIDATION_PASSED"
    ADT_VALIDATION_PASSED = "ADT_VALIDATION_PASSED"
    SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED = "SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED"
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    ACTIVATED = "ACTIVATED"
    EXTRACTION_ENABLED_VERIFIED = "EXTRACTION_ENABLED_VERIFIED"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    UNVERIFIED = "UNVERIFIED"
    NO_SOLUTION = "NO_SOLUTION"
    CUSTOM_WRAPPER_PENDING_APPROVAL = "CUSTOM_WRAPPER_PENDING_APPROVAL"
    CUSTOM_WRAPPER_EXTRACTION_ENABLED_VERIFIED = "CUSTOM_WRAPPER_EXTRACTION_ENABLED_VERIFIED"


class ReviewStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


# ==============================================================================
# 2. Domain Models
# ==============================================================================

class BusinessRequirement(BaseModel):
    """Encapsulates a user business requirement with semantic extraction."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique requirement ID")
    title: str = Field(..., min_length=3, max_length=200, description="Concise requirement title")
    description: str = Field(..., min_length=10, description="Raw natural language requirement statement")
    business_domain: str = Field(default="Sales & Distribution", description="SAP business functional domain")
    target_use_case: TargetUseCase = Field(
        default=TargetUseCase.ANALYTICAL_REPORT,
        description="Intended architectural use-case",
    )
    required_entities: List[str] = Field(default_factory=list, description="Target business entities (e.g., SalesOrder, Product)")
    required_fields: List[str] = Field(default_factory=list, description="Requested business fields or attributes")
    filter_criteria: List[str] = Field(default_factory=list, description="Requested filter conditions or where clauses")
    granularity: Optional[str] = Field(default=None, description="Data grain or aggregation level (e.g., Item, Header)")
    submitted_by: str = Field(default="system_user", description="Author or requester identifier")
    created_at: datetime = Field(default_factory=utc_now)


class CDSCandidate(BaseModel):
    """Represents a discovered SAP CDS view candidate evaluated for reuse or extension."""

    view_name: str = Field(..., description="Technical CDS view entity name (e.g., I_SalesOrderAnalytics)")
    sql_view_name: Optional[str] = Field(default=None, description="Classic SQL view name if applicable")
    description: str = Field(..., description="Business text or description from SAP data dictionary")
    layer: CDSLayer = Field(default=CDSLayer.BASIC, description="VDM architectural layer")
    release_status: str = Field(default="RELEASED", description="SAP release status (RELEASED, DEPRECATED, INTERNAL)")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Semantic match relevance score (0-1)")
    source_tables: List[str] = Field(default_factory=list, description="Underlying database tables (e.g., VBAK, VBAP)")
    associations: List[str] = Field(default_factory=list, description="Exposed associations to other views")
    available_fields: List[str] = Field(default_factory=list, description="Key and data elements available in the candidate")
    key_fields: List[str] = Field(default_factory=list, description="Primary key fields")
    is_extendable: bool = Field(default=True, description="Whether the CDS view entity supports extension")
    package: Optional[str] = Field(default=None, description="ABAP development package (e.g., VDM_SD_SLS_SO)")
    application_component: Optional[str] = Field(default=None, description="SAP Application Component hierarchy (e.g., SD-SLS, MM-PUR, FI-GL)")
    domain: Optional[str] = Field(default=None, description="Resolved SAP business domain")
    object_type: Optional[str] = Field(default="DDLS/DF", description="ADT object type (e.g., DDLS/DF)")
    source_type: Optional[str] = Field(default=None, description="CDS source type (view_entity, view, projection_view)")
    cds_construct_type: CDSConstructType = Field(
        default=CDSConstructType.UNKNOWN,
        description="Base CDS construct type: CLASSIC_VIEW, VIEW_ENTITY, or UNKNOWN",
    )
    version: Optional[str] = Field(default="active", description="Repository version (active/inactive)")
    structured_metadata: Optional[Any] = Field(default=None, description="Structured Milestone 9 CDSViewMetadata instance")
    field_details: List[Any] = Field(default_factory=list, description="Structured CDSFieldMetadata items")
    association_details: List[Any] = Field(default_factory=list, description="Structured CDSAssociationMetadata items")
    annotation_details: List[Any] = Field(default_factory=list, description="Structured CDSAnnotationMetadata items")
    parameters: List[Dict[str, Any]] = Field(default_factory=list, description="CDS view parameters")


class StructuredRequirement(BaseModel):
    """Fine-grained semantic representation of user requirements."""

    dimensions: List[str] = Field(default_factory=list, description="Business dimensions (e.g. SalesOrder, Customer)")
    measures: List[str] = Field(default_factory=list, description="Business measures (e.g. NetAmount, OrderQuantity)")
    attributes: List[str] = Field(default_factory=list, description="Descriptive attributes (e.g. CreationDate, Status)")
    parameters: List[str] = Field(default_factory=list, description="Input parameters (e.g. P_DisplayCurrency)")
    filters: List[str] = Field(default_factory=list, description="Requested filter conditions")
    all_requested_concepts: List[str] = Field(default_factory=list, description="Consolidated unique list of all requested concepts")


class FieldMapping(BaseModel):
    """Represents the semantic mapping between a requested requirement field and a candidate CDS field."""

    requirement_field: str = Field(..., description="Field requested in the business requirement")
    matched_field: Optional[str] = Field(default=None, description="Matched attribute in discovered CDS candidate")
    source_entity: Optional[str] = Field(default=None, description="Originating CDS view or joined table")
    data_type: Optional[str] = Field(default=None, description="ABAP dictionary data type (e.g., abap.char(10))")
    mapping_type: MappingType = Field(default=MappingType.MISSING, description="Mapping classification")
    evidence_level: EvidenceLevel = Field(default=EvidenceLevel.LEVEL_6_MISSING, description="Strict evidence tier (Level 1-6)")
    evidence_detail: str = Field(default="", description="Detailed human-readable evidence justifying the mapping")
    association_name: Optional[str] = Field(default=None, description="Association alias name (e.g. _Item)")
    association_target_view: Optional[str] = Field(default=None, description="Target CDS view if verified association hop")
    association_target_field: Optional[str] = Field(default=None, description="Target field on verified association view")
    cardinality: Optional[str] = Field(default=None, description="Association cardinality (e.g. [0..1], [0..*])")
    transformation_rule: Optional[str] = Field(default=None, description="Calculation formula or transformation expression")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Mapping confidence score")


class CandidateEvaluation(BaseModel):
    """Detailed multi-factor evaluation of a real SAP CDS view candidate."""

    candidate: CDSCandidate
    rank: int = Field(default=1, description="Rank among discovered candidates")
    rank_score: float = Field(default=0.0, description="Composite ranking score (0-100)")
    coverage_percentage: float = Field(default=0.0, description="Percentage of requested concepts satisfied")
    matched_concepts: List[str] = Field(default_factory=list, description="List of satisfied concept names")
    missing_concepts: List[str] = Field(default_factory=list, description="List of unsatisfied concept names")
    field_mappings: List[FieldMapping] = Field(default_factory=list, description="Itemized concept-to-field mappings")
    scoring_breakdown: Dict[str, float] = Field(default_factory=dict, description="Exposed heuristic scoring weights and components")
    is_extension_viable: bool = Field(default=False, description="Whether missing concepts are verified obtainable via extension")
    suitability_reason: str = Field(default="", description="Human-readable justification for candidate score and rank")


class Decision(BaseModel):
    """Outcome of the Decision Agent: recommendation of REUSE, EXTEND, CREATE, or INSUFFICIENT_EVIDENCE."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    requirement_id: str = Field(..., description="ID of the evaluated BusinessRequirement")
    action: DecisionAction = Field(..., description="Recommended action: REUSE, EXTEND, CREATE, or INSUFFICIENT_EVIDENCE")
    recommended_view: Optional[str] = Field(default=None, description="Existing CDS view name to reuse or extend")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Agent confidence score")
    fit_percentage: float = Field(..., ge=0.0, le=100.0, description="Percentage of requirement fulfilled by base view")
    rationale: str = Field(..., description="Detailed technical justification for the recommended action")
    missing_fields: List[str] = Field(default_factory=list, description="Requested fields not present in base view")
    proposed_extension_name: Optional[str] = Field(default=None, description="Proposed extension name if action is EXTEND")
    proposed_sql_view_append_name: Optional[str] = Field(default=None, description="Proposed SQL view append name for classic CDS view extension (max 16 chars)")
    proposed_new_view_name: Optional[str] = Field(default=None, description="Proposed new CDS view name if action is CREATE")
    requires_composition: bool = Field(default=False, description="True if multi-CDS composition is required")
    complementary_views: List[str] = Field(default_factory=list, description="Complementary CDS candidates for multi-CDS composition")
    evaluated_candidates: List[CandidateEvaluation] = Field(default_factory=list, description="Ranked evaluations of all considered SAP candidates")
    structured_requirement: Optional[StructuredRequirement] = Field(default=None, description="Structured breakdown of concepts")
    evidence_summary: List[str] = Field(default_factory=list, description="Summary of verified evidence items")
    warnings: List[str] = Field(default_factory=list, description="Governance, architectural, or ambiguity warnings")
    tradeoffs: List[str] = Field(default_factory=list, description="Architectural tradeoffs and clean core impact notes")
    human_approval_required: bool = Field(default=True, description="Enforces human gatekeeper before any code deployment")
    is_approved: bool = Field(default=False, description="Whether human reviewer has signed off")
    approved_by: Optional[str] = Field(default=None, description="Reviewer identifier")
    approval_timestamp: Optional[datetime] = Field(default=None, description="Timestamp of approval")
    rejection_reason: Optional[str] = Field(default=None, description="Reason if rejected by reviewer")


class DataModelEntity(BaseModel):
    """Entity node within a conceptual or logical CDS data model."""

    name: str = Field(..., description="Entity name (CDS view or table)")
    layer: Optional[CDSLayer] = None
    fields: List[str] = Field(default_factory=list)
    key_fields: List[str] = Field(default_factory=list)


class DataModelAssociation(BaseModel):
    """Association edge between two CDS entities."""

    source_entity: str = Field(..., description="Source entity")
    target_entity: str = Field(..., description="Target associated entity")
    cardinality: str = Field(default="[0..1]", description="Cardinality (e.g., [1..1], [0..*])")
    on_condition: str = Field(..., description="ON clause expression")
    alias: str = Field(..., description="Association alias name")


class DataModel(BaseModel):
    """Comprehensive representation of the target CDS data model (visual and relational)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(..., description="Data model name (e.g., ZC_SalesOrderAnalytics)")
    root_entity: str = Field(..., description="Root CDS view or primary data source")
    entities: List[DataModelEntity] = Field(default_factory=list)
    associations: List[DataModelAssociation] = Field(default_factory=list)
    visual_layout: Dict[str, Any] = Field(
        default_factory=dict,
        description="Coordinates and styling for React Flow graph rendering",
    )


class SAPValidationStatus(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class SAPCheckMessage(BaseModel):
    """Individual compiler message or warning returned by SAP ADT checkrun."""

    uri: Optional[str] = Field(default=None, description="ADT URI with position fragment (e.g. #start=line,offset)")
    severity: str = Field(..., description="Message severity: 'E' (Error), 'W' (Warning), 'I' (Info), 'A' (Abort)")
    short_text: str = Field(..., description="Compiler message short text")
    msgid: Optional[str] = Field(default=None, description="ABAP message class ID")
    msgno: Optional[str] = Field(default=None, description="ABAP message number")
    variables: List[str] = Field(default_factory=list, description="Message variables (msgv1..msgv4)")
    line: Optional[int] = Field(default=None, description="Source line number (1-indexed)")
    column: Optional[int] = Field(default=None, description="Source column or offset")
    raw_safe_metadata: Optional[Dict[str, Any]] = Field(default=None, description="Sanitized safe metadata")


class SAPCheckrunResult(BaseModel):
    """Aggregate result of remote SAP ADT checkrun execution."""

    status: SAPValidationStatus = Field(default=SAPValidationStatus.UNAVAILABLE, description="Overall SAP validation outcome")
    reporter: Optional[str] = Field(default="abapCheckRun", description="ADT reporter identifier")
    triggering_uri: Optional[str] = Field(default=None, description="Target object ADT URI that was checked")
    check_report_status: Optional[str] = Field(default=None, description="Status attribute from checkReport (e.g. 'processed')")
    messages: List[SAPCheckMessage] = Field(default_factory=list, description="List of check messages returned")
    http_status: Optional[int] = Field(default=None, description="HTTP status code from ADT checkrun response")
    content_type: Optional[str] = Field(default=None, description="Content-Type header from ADT response")
    reason: Optional[str] = Field(default=None, description="Explanatory text when status is UNAVAILABLE or INSUFFICIENT_EVIDENCE")


class SAPReviewCheckrunResult(BaseModel):
    """Aggregate result of controlled remote SAP ADT checkrun for an approved inactive review package."""

    review_id: str = Field(..., description="Review ID of the checked CDS proposal")
    sap_object_name: str = Field(..., description="Target CDS object name checked in SAP DEV")
    status: SAPValidationStatus = Field(
        default=SAPValidationStatus.UNAVAILABLE,
        description="Overall SAP validation outcome (PASS, WARNING, ERROR, UNAVAILABLE)",
    )
    success: bool = Field(
        default=False,
        description="True if checkrun completed with 0 blocking E/A/X errors",
    )
    check_report_status: Optional[str] = Field(
        default=None,
        description="Report status from ADT checkReport (e.g. 'processed')",
    )
    blocking_errors: int = Field(
        default=0,
        description="Count of blocking errors (severity E, A, X)",
    )
    warnings: int = Field(
        default=0,
        description="Count of compiler warnings (severity W)",
    )
    information: int = Field(
        default=0,
        description="Count of informational messages (severity I, S)",
    )
    messages: List[SAPCheckMessage] = Field(
        default_factory=list,
        description="Structured check messages returned from SAP",
    )
    timestamp: datetime = Field(
        default_factory=utc_now,
        description="Timestamp of checkrun execution",
    )
    source_hash: Optional[str] = Field(
        default=None,
        description="Hash of the DDL source code verified by this checkrun",
    )
    http_status: Optional[int] = Field(
        default=None,
        description="HTTP status code from SAP ADT checkrun response",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Reason if checkrun failed or unavailable",
    )


class ValidationResult(BaseModel):
    """Syntactic, clean-core, and governance validation check result."""

    is_valid: bool = Field(..., description="True if no blocking validation errors exist")
    syntax_errors: List[str] = Field(default_factory=list, description="Syntax errors detected in CDS definition")
    naming_convention_violations: List[str] = Field(
        default_factory=list,
        description="Violations of clean-core naming standards (e.g. ZI_ / ZC_)",
    )
    clean_core_compliance_score: float = Field(
        default=100.0,
        ge=0.0,
        le=100.0,
        description="Clean core compliance rating (0-100%)",
    )
    governance_warnings: List[str] = Field(default_factory=list, description="Non-blocking architectural warnings")
    details: List[Dict[str, Any]] = Field(default_factory=list, description="Itemized validation checks")
    sap_validation_status: SAPValidationStatus = Field(
        default=SAPValidationStatus.UNAVAILABLE,
        description="Outcome of remote SAP ADT checkrun",
    )
    sap_checkrun_result: Optional[SAPCheckrunResult] = Field(
        default=None,
        description="Structured result from SAP ADT checkrun endpoint",
    )
    sap_checkrun_reason: Optional[str] = Field(
        default=None,
        description="Human-readable justification for SAP validation status",
    )


class DeploymentRequest(BaseModel):
    """Formal deployment request for creating/activating CDS objects in SAP DEV."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    object_name: str = Field(..., description="CDS view entity name")
    object_type: str = Field(default="CDS_VIEW_ENTITY", description="Object type (VIEW_ENTITY, EXTENSION)")
    target_system: str = Field(default="SAP DEV - Client 100", description="Target SAP system identifier")
    package: str = Field(..., description="ABAP development package (e.g., ZSALES_ANALYTICS)")
    transport_request: str = Field(..., description="Workbench Transport Request (TR) number")
    requester: str = Field(..., description="Developer requesting deployment")
    approver: Optional[str] = Field(default=None, description="Senior reviewer who approved deployment")
    status: DeploymentStatus = Field(default=DeploymentStatus.PENDING_APPROVAL)
    created_at: datetime = Field(default_factory=utc_now)
    audit_trail_id: Optional[str] = Field(default=None, description="Audit ledger reference")


class DraftElementType(str, Enum):
    SOURCE_VIEW = "SOURCE_VIEW"
    FIELD = "FIELD"
    ASSOCIATION = "ASSOCIATION"
    PARAMETER = "PARAMETER"
    ANNOTATION = "ANNOTATION"
    FILTER = "FILTER"


class ElementEvidenceRecord(BaseModel):
    """Ground-truth evidence justifying why a specific CDS element is present in the draft."""

    element_name: str = Field(..., description="CDS element or field name")
    element_type: DraftElementType = Field(default=DraftElementType.FIELD, description="Element classification")
    source_entity: Optional[str] = Field(default=None, description="Originating CDS view entity")
    relationship: Optional[str] = Field(default=None, description="Association alias or join path if transitive")
    evidence_level: EvidenceLevel = Field(default=EvidenceLevel.LEVEL_6_MISSING, description="Evidence hierarchy level (L1-L6)")
    evidence_detail: str = Field(default="", description="Detailed human-readable evidence justification")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence rating")
    is_verified: bool = Field(default=False, description="True only for verified levels (L1-L4)")


class DraftFieldElement(BaseModel):
    """Field projection element within a CDS draft."""

    field_name: str = Field(..., description="Output field name or alias")
    alias: Optional[str] = Field(default=None, description="Field alias if renamed")
    source_expression: str = Field(..., description="SELECT expression (e.g. SalesOrder, _Item.Material)")
    source_entity: str = Field(..., description="Originating CDS view entity")
    data_type: Optional[str] = Field(default=None, description="ABAP dictionary type if known")
    is_key: bool = Field(default=False, description="True if primary key component")
    evidence: ElementEvidenceRecord = Field(..., description="Ground-truth evidence binding")


class DraftAssociationElement(BaseModel):
    """Association definition within a CDS draft."""

    association_name: str = Field(..., description="Association alias (e.g. _Item)")
    target_entity: str = Field(..., description="Target CDS view entity")
    cardinality: str = Field(default="[0..*]", description="Cardinality expression")
    on_condition: str = Field(..., description="ON clause expression")
    is_verified: bool = Field(default=True, description="True if target DDL was verified via ADT")
    evidence: ElementEvidenceRecord = Field(..., description="Ground-truth evidence binding")


class DraftParameterElement(BaseModel):
    """Input parameter definition within a CDS draft."""

    parameter_name: str = Field(..., description="Parameter name (e.g. P_DisplayCurrency)")
    data_type: str = Field(..., description="ABAP type (e.g. abap.cuky)")
    default_value: Optional[str] = Field(default=None, description="Default parameter value if defined")
    evidence: ElementEvidenceRecord = Field(..., description="Ground-truth evidence binding")


class CDSDraft(BaseModel):
    """Complete, evidence-grounded CDS design proposal and synthesized draft artifact."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    requirement_id: str = Field(..., description="Associated BusinessRequirement ID")
    decision_action: DecisionAction = Field(..., description="Decision from Milestone 5 (REUSE, EXTEND, CREATE, INSUFFICIENT_EVIDENCE)")
    object_name: str = Field(..., description="Target CDS view or extension name")
    object_type: str = Field(default="VIEW_ENTITY", description="REUSE_PROPOSAL, VIEW_ENTITY_EXTENSION, CUSTOM_VIEW_ENTITY, COMPOSITION_PROPOSAL")
    sql_view_append_name: Optional[str] = Field(default=None, description="SQL view append structure name for classic CDS view extension (max 16 chars, customer namespace Z or Y)")
    proposed_layer: CDSLayer = Field(default=CDSLayer.BASIC, description="Proposed VDM layer")
    source_views: List[str] = Field(default_factory=list, description="Verified source CDS view entities")
    fields: List[DraftFieldElement] = Field(default_factory=list, description="Projected fields with evidence")
    associations: List[DraftAssociationElement] = Field(default_factory=list, description="Defined associations")
    parameters: List[DraftParameterElement] = Field(default_factory=list, description="Defined parameters")
    filters: List[str] = Field(default_factory=list, description="Where conditions / filter expressions")
    annotations: Dict[str, Any] = Field(default_factory=dict, description="Header and element annotations")
    ddl_source: str = Field(..., description="Synthesized ABAP CDS DDL code or consumption proposal")
    evidence_per_element: List[ElementEvidenceRecord] = Field(default_factory=list, description="Itemized evidence for all elements")
    warnings: List[str] = Field(default_factory=list, description="Design warnings, unsupported assumptions, or evidence gaps")
    validation_errors: List[str] = Field(default_factory=list, description="Static validation errors")
    generation_status: str = Field(default="GENERATED", description="GENERATED, VALIDATED, INSUFFICIENT_EVIDENCE, REJECTED")
    sap_validation_status: Optional[SAPValidationStatus] = Field(default=None, description="Outcome of remote SAP ADT checkrun")
    unresolved_items: List[str] = Field(default_factory=list, description="Requested concepts that could not be verified")
    code_source: str = Field(default="GEMINI_GENERATED", description="Source of code: GEMINI_GENERATED or USER_PROVIDED")
    original_user_code: Optional[str] = Field(default=None, description="Raw user-provided code if code_source is USER_PROVIDED")
    created_at: datetime = Field(default_factory=utc_now)


class ReviewAuditEvent(BaseModel):
    """Immutable audit record for human review actions."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action: str = Field(..., description="Action taken: REVIEW_APPROVED, REVIEW_REJECTED, REVIEW_CHANGES_REQUESTED")
    previous_status: ReviewStatus = Field(..., description="Review state before this action")
    new_status: ReviewStatus = Field(..., description="Review state after this action")
    timestamp: datetime = Field(default_factory=utc_now, description="Timestamp of the review event")
    reviewer: str = Field(..., description="Identifier of the reviewer")
    reason: Optional[str] = Field(default=None, description="Mandatory reason for rejection or change request")
    decision_action: DecisionAction = Field(..., description="Underlying architectural decision being reviewed")
    details: Dict[str, Any] = Field(default_factory=dict, description="Sanitized event metadata (no secrets)")


class CDSReviewPackage(BaseModel):
    """Structured, evidence-grounded review package presented to the human gatekeeper."""

    review_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique review package ID")
    requirement_id: str = Field(..., description="Business requirement ID")
    created_at: datetime = Field(default_factory=utc_now)
    status: ReviewStatus = Field(default=ReviewStatus.PENDING_REVIEW, description="Current review state")
    reviewer: Optional[str] = Field(default=None, description="Reviewer who approved, rejected, or requested changes")
    review_comment: Optional[str] = Field(default=None, description="Reviewer feedback or reason")
    review_timestamp: Optional[datetime] = Field(default=None, description="Timestamp of reviewer action")
    can_approve: bool = Field(default=False, description="Whether all deterministic approval gates are satisfied")
    blocking_reasons: List[str] = Field(default_factory=list, description="Reasons why approval is currently blocked")
    approval_confirmation_notice: str = Field(
        default="You are approving this AI-generated CDS proposal for the next controlled workflow stage. No SAP object will be modified by this approval.",
        description="Explicit confirmation text shown before approval",
    )

    # 1. Requirement Section
    original_requirement: BusinessRequirement
    normalized_requirement: Optional[StructuredRequirement] = None
    dimensions: List[str] = Field(default_factory=list)
    measures: List[str] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)

    # 2. SAP Evidence Section
    selected_candidate: Optional[CDSCandidate] = None
    candidate_package: Optional[str] = None
    candidate_object_type: Optional[str] = None
    candidate_release_status: Optional[str] = None
    structured_metadata: Optional[Any] = None
    real_fields: List[Any] = Field(default_factory=list)
    associations: List[Any] = Field(default_factory=list)
    evidence_levels: List[str] = Field(default_factory=list)

    # 3. Mapping Section
    field_mappings: List[FieldMapping] = Field(default_factory=list)
    mapping_warnings: List[str] = Field(default_factory=list)

    # 4. Decision Section
    decision: Decision
    decision_action: DecisionAction
    decision_confidence: float
    decision_rationale: str
    missing_fields: List[str] = Field(default_factory=list)
    decision_warnings: List[str] = Field(default_factory=list)

    # 5. Generated Artifact Section
    code_source: str = Field(default="GEMINI_GENERATED", description="Source of code: GEMINI_GENERATED or USER_PROVIDED")
    original_user_code: Optional[str] = Field(default=None, description="Raw user-provided code if code_source is USER_PROVIDED")
    cds_draft: Optional[CDSDraft] = None
    object_name: Optional[str] = None
    object_type: Optional[str] = None
    sql_view_append_name: Optional[str] = None
    ddl_source: Optional[str] = None
    generation_warnings: List[str] = Field(default_factory=list)

    # 6. Validation Section
    validation_result: Optional[ValidationResult] = None
    is_valid: bool = False
    sap_validation_status: SAPValidationStatus = SAPValidationStatus.UNAVAILABLE
    diagnostics: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    latest_sap_checkrun: Optional[SAPReviewCheckrunResult] = None

    # 7. Audit Section
    audit_events: List[ReviewAuditEvent] = Field(default_factory=list)


class SAPCreationResult(BaseModel):
    """Structured outcome of controlled SAP DEV CDS creation."""

    success: bool = Field(..., description="Whether the creation operation succeeded")
    operation: str = Field(default="CREATE_CDS_VIEW", description="Operation performed (CREATE_CDS_VIEW, EXTEND_CDS_VIEW, REUSE_STANDARD_VIEW)")
    review_id: str = Field(..., description="Approved review ID authorizing this creation")
    sap_object_name: str = Field(..., description="Technical SAP CDS object name")
    repository_uri: Optional[str] = Field(default=None, description="ADT repository URI of the created object")
    object_type: str = Field(default="DDLS/DF", description="ADT object type")
    package: str = Field(..., description="Target SAP ABAP package")
    creation_timestamp: datetime = Field(default_factory=utc_now)
    activation_status: str = Field(default="INACTIVE", description="Activation state (strictly INACTIVE in Milestone 13)")
    transport_request: Optional[str] = Field(default=None, description="Workbench Transport Request number if returned")
    sap_http_status: Optional[int] = Field(default=None, description="HTTP status code from SAP ADT")
    sap_write_performed: bool = Field(default=False, description="Whether an authentic network write was executed")
    diagnostics: List[Dict[str, Any]] = Field(default_factory=list, description="Step-by-step diagnostic records")
    error_message: Optional[str] = Field(default=None, description="Sanitized error description if failed")


class SAPActivationResult(BaseModel):
    """Structured outcome of controlled SAP DEV CDS activation."""

    success: bool = Field(..., description="Whether the activation operation succeeded")
    review_id: str = Field(..., description="Approved review ID authorizing this activation")
    sap_object_name: str = Field(..., description="Technical SAP CDS object name")
    repository_uri: Optional[str] = Field(default=None, description="ADT repository URI of the activated object")
    activation_status: str = Field(default="ACTIVE", description="Final activation status (ACTIVE or INACTIVE)")
    transport_request: Optional[str] = Field(default=None, description="Workbench Transport Request number")
    sap_http_status: Optional[int] = Field(default=None, description="HTTP status code from SAP ADT activation")
    sap_write_performed: bool = Field(default=False, description="Whether an authentic network write was executed")
    diagnostics: List[Dict[str, Any]] = Field(default_factory=list, description="Step-by-step diagnostic records")
    syntax_messages: List[Dict[str, Any]] = Field(default_factory=list, description="Structured compiler diagnostics")
    error_message: Optional[str] = Field(default=None, description="Sanitized error description if failed")
    activated_at: datetime = Field(default_factory=utc_now)
    activated_source_retrieved: Optional[bool] = Field(default=None, description="Whether authentic activated DDL was retrieved from SAP DEV")
    extraction_refresh_status: Optional[str] = Field(default=None, description="Status of I_DataExtractionEnabledView refresh")
    extraction_enabled_confirmed: Optional[bool] = Field(default=None, description="Whether SAP reports the view as extraction enabled post-activation")
    post_activation_status: Optional[str] = Field(default=None, description="Canonical post-activation status (e.g. EXTRACTION_ENABLED_VERIFIED)")


class CDSWorkflowState(TypedDict, total=False):
    """Encapsulates the complete lifecycle state flowing through the CDS workflow."""

    # 1. Input Stage
    requirement: Optional[BusinessRequirement]
    structured_requirement: Optional[StructuredRequirement]

    # 2. Discovery & Mapping Stage
    candidates: List[CDSCandidate]
    evaluated_candidates: List[Any]
    field_mappings: List[FieldMapping]
    discovery_status: Optional[str]
    mapping_warnings: List[str]
    llm_mapping: Optional[Dict[str, Any]]
    pipeline_status: Optional[str]

    # 3. Decision Stage
    decision: Optional[Decision]
    proposed_extension_name: Optional[str]
    proposed_sql_view_append_name: Optional[str]

    # 4. Modeling & Generation Stage
    data_model: Optional[DataModel]
    cds_draft: Optional[CDSDraft]
    sql_view_append_name: Optional[str]
    generated_cds_code: Optional[str]

    # 5. Validation Stage
    validation_result: Optional[ValidationResult]

    # 6. Governance & Review Gate Stage
    deployment_request: Optional[DeploymentRequest]
    review_status: ReviewStatus
    review_package: Optional[Any]
    is_human_approved: bool

    # 7. Audit & Pipeline Execution Metadata
    audit_logs: List[Dict[str, Any]]
    errors: List[str]
    current_step: str


