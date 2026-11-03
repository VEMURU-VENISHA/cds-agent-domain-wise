export type DecisionAction = 'REUSE' | 'EXTEND' | 'CREATE' | 'INSUFFICIENT_EVIDENCE';

export type EvidenceLevel =
  | 'LEVEL_1_EXACT_FIELD'
  | 'LEVEL_2_LABEL_MATCH'
  | 'LEVEL_3_VERIFIED_ASSOCIATION'
  | 'LEVEL_4_HEURISTIC_SYNONYM'
  | 'LEVEL_5_UNVERIFIED_SUGGESTION'
  | 'LEVEL_6_MISSING';

export interface HealthStatus {
  status: string;
  app_name: string;
  version: string;
  environment: string;
  timestamp: string;
  database: {
    status: string;
    driver: string;
  };
  sap_connector: {
    status: string;
    system_id: string;
    client: string;
    s4hana_release: string;
    s4core_release: string;
    basis_release: string;
    is_connected: boolean;
  };
}

export interface BusinessRequirement {
  id: string;
  title: string;
  description: string;
  business_domain: string;
  target_use_case: string;
  required_entities: string[];
  required_fields: string[];
  filter_criteria: string[];
  created_at: string;
}

export interface StructuredRequirement {
  dimensions: string[];
  measures: string[];
  attributes: string[];
  parameters: string[];
  filters: string[];
  all_requested_concepts: string[];
}

export interface CDSAnnotationMetadata {
  name: string;
  value: any;
  scope: string;
  raw_text?: string;
  evidence_level: EvidenceLevel;
}

export interface CDSFieldMetadata {
  name: string;
  alias?: string;
  source_expression?: string;
  is_key: boolean;
  data_type?: string;
  abap_type: string;
  length: number;
  decimals: number;
  description: string;
  annotations?: Record<string, any>;
  evidence_level: EvidenceLevel;
  evidence_source: string;
  evidence_detail?: string;
}

export interface CDSAssociationMetadata {
  name: string;
  target_entity: string;
  cardinality: string;
  on_condition: string;
  is_exposed: boolean;
  evidence_level: EvidenceLevel;
  evidence_source: string;
  evidence_detail?: string;
}

export type CDSConstructType = 'CLASSIC_VIEW' | 'VIEW_ENTITY' | 'UNKNOWN';

export interface CDSViewMetadata {
  view_name: string;
  description: string;
  object_type: string;
  source_type: string;
  cds_construct_type?: CDSConstructType;
  version: string;
  package?: string;
  application_component?: string;
  domain?: string;
  layer: 'BASIC' | 'COMPOSITE' | 'CONSUMPTION' | 'EXTENSION';
  sql_view_name?: string;
  source_tables: string[];
  parameters: Array<{ name: string; type: string }>;
  fields: CDSFieldMetadata[];
  associations: CDSAssociationMetadata[];
  annotations: CDSAnnotationMetadata[];
  raw_source?: string;
  evidence_level: EvidenceLevel;
  evidence_source: string;
  parsed_at: string;
}

export interface CDSCandidate {
  view_name: string;
  sql_view_name?: string;
  description: string;
  layer: 'BASIC' | 'COMPOSITE' | 'CONSUMPTION';
  release_status: string;
  confidence_score: number;
  source_tables: string[];
  associations: string[];
  available_fields: string[];
  key_fields: string[];
  is_extendable: boolean;
  package?: string;
  application_component?: string;
  domain?: string;
  object_type?: string;
  source_type?: string;
  cds_construct_type?: CDSConstructType;
  version?: string;
  structured_metadata?: CDSViewMetadata;
  field_details?: CDSFieldMetadata[];
  association_details?: CDSAssociationMetadata[];
  annotation_details?: CDSAnnotationMetadata[];
  // Authoritative SAP Data Extraction Metadata (I_DataExtractionEnabledView)
  data_extraction_enabled?: boolean;
  extraction_status?: 'VERIFIED_ENABLED' | 'VERIFIED_NOT_ENABLED' | 'UNVERIFIED' | string;
  delta_cdc_supported?: boolean;
  is_sap_released_view?: boolean;
  extraction_details?: string;
  component_extraction_statuses?: ComponentExtractionStatus[];
}

export interface ComponentExtractionStatus {
  cds_name: string;
  status: 'TRUE' | 'FALSE' | 'UNKNOWN';
  annotation_status: 'TRUE' | 'FALSE' | 'UNKNOWN';
  sap_metadata_status: 'TRUE' | 'FALSE' | 'UNKNOWN';
  evidence: string[];
  sap_description?: string;
  can_create_extraction_wrapper?: boolean;
}

export interface ExtractionFilteringStats {
  filtered_single_count?: number;
  filtered_multi_count?: number;
  excluded_single_count?: number;
  excluded_multi_count?: number;
  require_delta_support?: boolean;
  metadata_complete?: boolean;
  error?: string;
}

export interface ExtractionStatusResult {
  cds_name: string;
  extraction_status: 'VERIFIED_ENABLED' | 'VERIFIED_NOT_ENABLED' | 'UNVERIFIED' | string;
  data_extraction_enabled: boolean | null;
  status?: 'TRUE' | 'FALSE' | 'UNKNOWN';
  can_create_extraction_wrapper?: boolean;
  annotation_status?: 'TRUE' | 'FALSE' | 'UNKNOWN';
  sap_metadata_status?: 'TRUE' | 'FALSE' | 'UNKNOWN';
  evidence?: string[];
  sap_description?: string;
  description?: string;
  is_sap_released_view?: boolean;
  delta_cdc_supported?: boolean;
  error?: string;
}

export interface FieldMapping {
  requirement_field: string;
  matched_field?: string;
  source_entity?: string;
  data_type?: string;
  mapping_type: 'DIRECT_MATCH' | 'ASSOCIATION_HOP' | 'CALCULATED' | 'MISSING' | 'UNRESOLVED';
  evidence_level: EvidenceLevel;
  evidence_detail: string;
  association_target_view?: string;
  association_target_field?: string;
  confidence: number;
}

export interface CandidateEvaluation {
  candidate: CDSCandidate;
  rank: number;
  rank_score: number;
  coverage_percentage: number;
  matched_concepts: string[];
  missing_concepts: string[];
  field_mappings: FieldMapping[];
  scoring_breakdown: Record<string, number>;
  is_extension_viable: boolean;
  suitability_reason: string;
}

export interface Decision {
  id: string;
  requirement_id: string;
  action: DecisionAction;
  recommended_view?: string;
  confidence_score: number;
  fit_percentage: number;
  rationale: string;
  missing_fields: string[];
  proposed_new_view_name?: string;
  proposed_extension_name?: string;
  proposed_sql_view_append_name?: string;
  requires_composition?: boolean;
  complementary_views?: string[];
  evaluated_candidates?: CandidateEvaluation[];
  structured_requirement?: StructuredRequirement;
  evidence_summary?: string[];
  warnings?: string[];
  tradeoffs: string[];
  human_approval_required: boolean;
  is_approved: boolean;
  approved_by?: string;
}

export type DraftElementType =
  | 'SOURCE_VIEW'
  | 'FIELD'
  | 'ASSOCIATION'
  | 'PARAMETER'
  | 'ANNOTATION'
  | 'FILTER';

export interface ElementEvidenceRecord {
  element_name: string;
  element_type: DraftElementType;
  source_entity?: string;
  relationship?: string;
  evidence_level: EvidenceLevel;
  evidence_detail: string;
  confidence: number;
  is_verified: boolean;
}

export interface DraftFieldElement {
  field_name: string;
  alias?: string;
  source_expression: string;
  source_entity: string;
  data_type?: string;
  is_key: boolean;
  evidence: ElementEvidenceRecord;
}

export interface DraftAssociationElement {
  association_name: string;
  target_entity: string;
  cardinality: string;
  on_condition: string;
  is_verified: boolean;
  evidence: ElementEvidenceRecord;
}

export interface DraftParameterElement {
  parameter_name: string;
  data_type: string;
  default_value?: string;
  evidence: ElementEvidenceRecord;
}

export type SAPValidationStatus =
  | 'PASS'
  | 'WARNING'
  | 'ERROR'
  | 'UNAVAILABLE'
  | 'INSUFFICIENT_EVIDENCE';

export interface SAPCheckMessage {
  uri?: string;
  severity: string;
  short_text: string;
  msgid?: string;
  msgno?: string;
  variables: string[];
  line?: number;
  column?: number;
  raw_safe_metadata?: Record<string, any>;
}

export interface SAPCheckrunResult {
  status: SAPValidationStatus;
  reporter?: string;
  triggering_uri?: string;
  check_report_status?: string;
  messages: SAPCheckMessage[];
  http_status?: number;
  content_type?: string;
  reason?: string;
}

export interface SAPReviewCheckrunResult {
  review_id: string;
  sap_object_name: string;
  status: SAPValidationStatus;
  success: boolean;
  check_report_status?: string;
  blocking_errors: number;
  warnings: number;
  information: number;
  messages: SAPCheckMessage[];
  timestamp: string;
  source_hash?: string;
  http_status?: number;
  reason?: string;
}


export interface ValidationResult {
  is_valid: boolean;
  syntax_errors: string[];
  naming_convention_violations: string[];
  clean_core_compliance_score: number;
  governance_warnings: string[];
  details: Array<{ check: string; passed: boolean; detail: string }>;
  sap_validation_status?: SAPValidationStatus;
  sap_checkrun_result?: SAPCheckrunResult;
  sap_checkrun_reason?: string;
}

export interface CDSDraft {
  id: string;
  requirement_id: string;
  decision_action: DecisionAction;
  object_name: string;
  object_type: string;
  sql_view_append_name?: string;
  proposed_layer: 'BASIC' | 'COMPOSITE' | 'CONSUMPTION';
  source_views: string[];
  fields: DraftFieldElement[];
  associations: DraftAssociationElement[];
  parameters: DraftParameterElement[];
  filters: string[];
  annotations: Record<string, any>;
  ddl_source: string;
  evidence_per_element: ElementEvidenceRecord[];
  warnings: string[];
  validation_errors: string[];
  generation_status: string;
  sap_validation_status?: SAPValidationStatus;
  unresolved_items: string[];
  created_at: string;
}

export type ReviewStatus = 'PENDING_REVIEW' | 'APPROVED' | 'REJECTED' | 'CHANGES_REQUESTED';

export interface ReviewAuditEvent {
  id: string;
  action: string;
  previous_status: ReviewStatus;
  new_status: ReviewStatus;
  timestamp: string;
  reviewer: string;
  reason?: string;
  decision_action: DecisionAction;
  details?: Record<string, any>;
}

export interface CDSReviewPackage {
  review_id: string;
  requirement_id: string;
  created_at: string;
  status: ReviewStatus;
  reviewer?: string;
  review_comment?: string;
  review_timestamp?: string;
  can_approve: boolean;
  blocking_reasons: string[];
  approval_confirmation_notice: string;

  // 1. Requirement Section
  original_requirement: BusinessRequirement;
  normalized_requirement?: StructuredRequirement;
  dimensions: string[];
  measures: string[];
  filters: string[];

  // 2. SAP Evidence Section
  selected_candidate?: CDSCandidate;
  candidate_package?: string;
  candidate_object_type?: string;
  candidate_release_status?: string;
  structured_metadata?: CDSViewMetadata;
  real_fields: CDSFieldMetadata[];
  associations: CDSAssociationMetadata[];
  evidence_levels: string[];

  // 3. Mapping Section
  field_mappings: FieldMapping[];
  mapping_warnings: string[];

  // 4. Decision Section
  decision: Decision;
  decision_action: DecisionAction;
  decision_confidence: number;
  decision_rationale: string;
  missing_fields: string[];
  decision_warnings: string[];

  // 5. Generated Artifact Section
  cds_draft?: CDSDraft;
  object_name?: string;
  object_type?: string;
  sql_view_append_name?: string;
  ddl_source?: string;
  generation_warnings: string[];

  // 6. Validation Section
  validation_result?: ValidationResult;
  is_valid: boolean;
  sap_validation_status?: SAPValidationStatus;
  diagnostics: Array<{ check: string; passed: boolean; detail: string }>;
  errors: string[];
  warnings: string[];
  latest_sap_checkrun?: SAPReviewCheckrunResult | null;

  // 7. Audit Section
  audit_events: ReviewAuditEvent[];
}

export interface PipelineResult {
  requirement: BusinessRequirement;
  structured_requirement?: StructuredRequirement;
  evaluated_candidates?: CandidateEvaluation[];
  decision: Decision;
  generated_code_preview: string;
  cds_draft?: CDSDraft;
  validation_result?: ValidationResult;
  current_step: string;
  audit_events_count: number;
  discovery_status?: string;
  mapping_warnings?: string[];
  errors?: string[];
  review_id?: string;
  review_status?: ReviewStatus;
  can_approve?: boolean;
}

export interface SAPCreationResult {
  success: boolean;
  operation?: string;
  review_id?: string;
  sap_object_name?: string;
  name?: string;
  repository_uri?: string;
  object_type?: string;
  package?: string;
  activation_status?: string;
  transport_request?: string;
  sap_http_status?: number;
  sap_write_performed?: boolean;
  diagnostics?: Array<{ step: string; passed: boolean; detail: string }>;
  error_message?: string;
  status?: string;
  message?: string;
  error?: string;
}

export interface SAPActivationResult {
  success: boolean;
  review_id: string;
  sap_object_name: string;
  repository_uri?: string;
  activation_status: string;
  transport_request?: string;
  sap_http_status?: number;
  sap_write_performed: boolean;
  diagnostics: Array<Record<string, any>>;
  syntax_messages: Array<Record<string, any>>;
  error_message?: string;
  activated_at?: string;
}

export interface DataPreviewResult {
  cds_name: string;
  total_rows: number;
  columns: string[];
  rows: Array<Record<string, any>>;
}

export interface TransportRequestItem {
  number: string;
  owner: string;
  description: string;
  status: string;
  type: string;
  is_default?: boolean;
}

export interface IncrementalSyncResult {
  success: boolean;
  prefixes: string[];
  new_views_count: number;
  updated_views_count: number;
  new_views: string[];
  elapsed_seconds: number;
  timestamp: string;
}

export type CDSClassificationType = 'BASIC' | 'COMPOSITE' | 'CONSUMPTION' | 'PARAMETERIZED' | 'UNKNOWN';

export type WorkflowStatus =
  | 'REQUIREMENT_ANALYSIS_STARTED'
  | 'REQUIRED_FIELDS_EXTRACTED'
  | 'EXTRACTION_FIRST_SEARCH_STARTED'
  | 'EXISTING_EXTRACTION_ENABLED_CDS'
  | 'FALLBACK_CATALOG_SEARCH_STARTED'
  | 'BASIC_CDS_DISCOVERY_COMPLETED'
  | 'SOURCE_SELECTION_PENDING'
  | 'SOURCE_CDS_APPROVED'
  | 'JOIN_CONFIGURATION_PENDING'
  | 'JOIN_CONFIGURATION_APPROVED'
  | 'WRAPPER_GENERATION_STARTED'
  | 'WRAPPER_GENERATED'
  | 'STATIC_VALIDATION_PASSED'
  | 'SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED'
  | 'VALIDATION_FAILED'
  | 'FINAL_HUMAN_REVIEW_PENDING'
  | 'FINAL_APPROVAL_GRANTED'
  | 'SAVED_INACTIVE'
  | 'CHECKRUN_PASSED'
  | 'ACTIVATED_IN_SAP_DEV'
  | 'EXTRACTION_ENABLED_VERIFIED'
  | 'NO_SOLUTION';

export interface ClassificationResult {
  cds_name: string;
  classification: CDSClassificationType;
  classification_evidence: string;
  is_basic: boolean;
  is_parameterized: boolean;
  vdm_view_type_annotation?: string;
  parameter_count?: number;
  parameters?: string[];
  vdm_evidence_detail?: string;
}

export interface OutputFieldSelection {
  field_name: string;
  source_cds: string;
  data_type: string;
  length?: number | null;
  decimals?: number | null;
  is_key: boolean;
  field_label?: string;
  selected_for_output: boolean;
  output_alias?: string | null;
}

export interface JoinCondition {
  left_field: string;
  right_field: string;
  operator?: string;
  logic_operator?: string;
}

export interface JoinRelationship {
  left_cds: string;
  right_cds: string;
  join_type: string;
  conditions: JoinCondition[];
  cardinality?: string;
  relationship_evidence_type?: string;
  evidence_detail?: string;
  is_user_edited?: boolean;
}

export interface JoinValidationIssue {
  code: string;
  message: string;
  severity: 'ERROR' | 'WARNING' | 'INFO';
  left_cds?: string;
  right_cds?: string;
  field?: string;
}

export interface JoinValidationResult {
  is_valid: boolean;
  status: WorkflowStatus;
  errors: JoinValidationIssue[];
  warnings: JoinValidationIssue[];
  relationships: JoinRelationship[];
  output_fields: OutputFieldSelection[];
  requires_approval: boolean;
  is_approved: boolean;
}

export interface CorrectionLoopAttempt {
  attempt: number;
  prompt_focus?: string;
  generated_code?: string;
  errors_count: number;
  warnings_count: number;
  errors: Array<{ rule: string; message: string; line_number?: number }>;
  valid: boolean;
}

export interface WrapperGenerationResult {
  code: string;
  explanation: string;
  artifact_name: string;
  validation: {
    valid: boolean;
    errors: Array<{ rule: string; message: string; line_number?: number }>;
    warnings: Array<{ rule: string; message: string; line_number?: number }>;
    status?: string;
  };
  sap_source_validation_status?: string;
  sap_source_validation_investigation?: string;
  correction_attempts_count: number;
  correction_history?: CorrectionLoopAttempt[];
  status: WorkflowStatus;
}



