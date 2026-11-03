import {
  HealthStatus,
  CDSReviewPackage,
  SAPCreationResult,
  DataPreviewResult,
  TransportRequestItem,
  IncrementalSyncResult,
  ExtractionStatusResult,
} from '../types';

const API_BASE = '/api/v1';

export async function fetchHealth(): Promise<HealthStatus> {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) {
    throw new Error(`Failed to fetch platform health: ${res.statusText}`);
  }
  return res.json();
}

export async function finalApproveProposal(payload: {
  requirement: string;
  selected_domains?: string[];
  required_fields?: any[];
  selected_views?: string[];
  field_mappings?: any[];
  join_paths?: any[];
  kpi?: any;
  artifact_name: string;
  package?: string;
  ddl_source: string;
  transport_request: string;
  reviewer?: string;
  comment?: string;
  review_id?: string;
  code_source?: string;
  original_user_code?: string;
}): Promise<CDSReviewPackage> {
  const res = await fetch(`${API_BASE}/reviews/final-approve`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to final approve proposal and synthesize review package');
  }
  return res.json();
}

export async function createCdsInSap(
  reviewId: string,
  transportRequest?: string
): Promise<SAPCreationResult> {
  const res = await fetch(`${API_BASE}/reviews/${reviewId}/create-sap`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      transport_request: transportRequest || null,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    if (err?.detail && typeof err.detail === 'object') {
      return {
        success: false,
        status: err.detail.status || 'SAP_CREATE_FAILED',
        sap_object_name: err.detail.artifact_name || '',
        message: err.detail.message || 'Failed to execute SAP DEV creation',
        error: err.detail.message || 'Failed to execute SAP DEV creation',
        package: 'ZTRAINING_2026',
        review_id: reviewId,
        sap_write_performed: false,
      } as any;
    }
    const errMsg = typeof err?.detail === 'string' ? err.detail : 'Failed to execute SAP DEV creation';
    throw new Error(errMsg);
  }
  return res.json();
}

export async function extractFields(requirement: string, businessDomains?: string[]) {
  const res = await fetch(`${API_BASE}/requirements/extract-fields`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requirement, business_domains: businessDomains }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to extract requirement fields');
  }
  return res.json();
}

export async function runDiscovery(
  requirement: string,
  requiredFields: any[],
  searchMode: string = 'fast',
  useCache: boolean = true,
  selectedDomains?: string[],
  extractionEnabledOnly?: boolean,
  requireDeltaSupport?: boolean,
  standardOnly: boolean = true
) {
  const res = await fetch(`${API_BASE}/discovery/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      requirement,
      required_fields: requiredFields,
      search_mode: searchMode,
      use_cache: useCache,
      selected_domains: selectedDomains,
      extraction_enabled_only: extractionEnabledOnly,
      require_delta_support: requireDeltaSupport,
      standard_only: standardOnly,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Discovery failed');
  }
  return res.json();
}

export async function fetchExtractionStatus(viewName: string): Promise<ExtractionStatusResult> {
  const res = await fetch(`${API_BASE}/discovery/extraction-status/${encodeURIComponent(viewName)}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || `Failed to fetch extraction status for ${viewName}`);
  }
  return res.json();
}

export async function directSearch(query: string) {
  const res = await fetch(`${API_BASE}/discovery/direct-search?q=${encodeURIComponent(query)}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Direct CDS search failed');
  }
  return res.json();
}

export async function fetchViewDetails(viewName: string) {
  const res = await fetch(`${API_BASE}/discovery/view-details/${encodeURIComponent(viewName)}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to fetch view details');
  }
  return res.json();
}

export async function fetchLiveCdsSource(viewName: string, fields?: string[]) {
  const query = fields && fields.length > 0 ? `?fields=${encodeURIComponent(fields.join(','))}` : '';
  const res = await fetch(`${API_BASE}/discovery/source/${encodeURIComponent(viewName)}${query}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to fetch CDS source');
  }
  return res.json();
}

export async function generateFieldVariants(
  fieldName: string,
  requirement?: string,
  businessDomain?: string
): Promise<{
  name: string;
  field_type: string;
  underlying_concepts: string[];
  calculation_approach: string;
  variants: string[];
}> {
  const res = await fetch(`${API_BASE}/requirements/generate-field-variants`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      field_name: fieldName,
      requirement: requirement || '',
      business_domain: businessDomain || 'Sales & Distribution',
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to generate field variants');
  }
  return res.json();
}

export async function fetchMetadataStatus() {
  const res = await fetch(`${API_BASE}/discovery/metadata-status`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to fetch metadata status');
  }
  return res.json();
}

export async function refreshMetadata() {
  const res = await fetch(`${API_BASE}/discovery/refresh-metadata`, { method: 'POST' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to refresh metadata');
  }
  return res.json();
}

export async function generateCdsCode(payload: {
  requirement: string;
  selected_views: string[];
  mappings: any[];
  join_paths?: any[];
  kpi?: any;
  artifact_name?: string;
}) {
  const res = await fetch(`${API_BASE}/requirements/generate-code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Code generation failed');
  }
  return res.json();
}

export async function validateUserCode(code: string, verifiedContext: any) {
  const res = await fetch(`${API_BASE}/requirements/validate-user-code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, verified_context: verifiedContext }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'User code validation failed');
  }
  return res.json();
}

export async function regenerateCdsCode(payload: {
  current_code: string;
  feedback: string;
  verified_context?: any;
}) {
  const res = await fetch(`${API_BASE}/requirements/regenerate-code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Code regeneration failed');
  }
  return res.json();
}

export async function validateArtifactName(name: string, pkg: string = 'ZTRAINING_2026') {
  const res = await fetch(`${API_BASE}/requirements/validate-artifact-name`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, package: pkg }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Artifact validation failed');
  }
  return res.json();
}

export async function directCreateInSap(payload: {
  name: string;
  package: string;
  source: string;
  transport_request: string;
}) {
  const res = await fetch(`${API_BASE}/reviews/direct-create-sap`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'SAP ADT creation failed');
  }
  return res.json();
}

export async function fetchDataPreview(cdsName: string, maxRows: number = 10): Promise<DataPreviewResult> {
  const res = await fetch(`${API_BASE}/discovery/preview/${encodeURIComponent(cdsName)}?max_rows=${maxRows}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || `Failed to fetch data preview for ${cdsName}`);
  }
  return res.json();
}

export async function fetchTransportRequests(): Promise<TransportRequestItem[]> {
  const res = await fetch(`${API_BASE}/reviews/transports`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to fetch transport requests from SAP');
  }
  return res.json();
}

export async function runIncrementalSync(prefixes?: string[], maxPerPrefix: number = 100): Promise<IncrementalSyncResult> {
  const res = await fetch(`${API_BASE}/discovery/sync-incremental`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prefixes: prefixes || ['Z*', 'Y*'], max_per_prefix: maxPerPrefix }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Incremental synchronization failed');
  }
  return res.json();
}

export async function fetchTableLineage(
  tableName: string,
  extractionEnabledOnly: boolean = true,
  requireDeltaSupport: boolean = false,
  matchingMode: 'ANY' | 'ALL' = 'ANY',
  standardOnly: boolean = true
) {
  const clean = tableName.trim();
  const query = `?tables=${encodeURIComponent(clean)}&matching_mode=${matchingMode}&extraction_enabled_only=${extractionEnabledOnly}&require_delta_support=${requireDeltaSupport}&standard_only=${standardOnly}`;
  const res = await fetch(`${API_BASE}/discovery/table-lineage${query}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || `Failed to fetch lineage for table(s) ${tableName}`);
  }
  return res.json();
}

export async function classifyCdsView(cdsName: string) {
  const res = await fetch(`${API_BASE}/discovery/classify/${encodeURIComponent(cdsName)}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || `Failed to classify CDS view ${cdsName}`);
  }
  return res.json();
}

export async function fetchAllFieldsForViews(viewNames: string[]) {
  const res = await fetch(`${API_BASE}/discovery/all-fields`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ view_names: viewNames }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to fetch fields for views');
  }
  return res.json();
}

export async function validateJoinConfiguration(
  selectedViews: string[],
  relationships: any[],
  outputFields?: any[]
) {
  const res = await fetch(`${API_BASE}/discovery/validate-joins`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      selected_views: selectedViews,
      relationships,
      output_fields: outputFields || null,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to validate join configuration');
  }
  return res.json();
}

export async function approveJoinConfiguration(
  selectedViews: string[],
  relationships: any[],
  outputFields?: any[]
) {
  const res = await fetch(`${API_BASE}/discovery/approve-joins`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      selected_views: selectedViews,
      relationships,
      output_fields: outputFields || null,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to approve join configuration');
  }
  return res.json();
}

export async function generateWrapperCode(payload: {
  requirement: string;
  selected_views: string[];
  mappings: any[];
  join_paths?: any[];
  output_fields?: any[];
  kpi?: any;
  artifact_name: string;
  wrapper_label: string;
  sql_view_name: string;
  join_configuration_approved: boolean;
  selected_views_original_ddl?: Record<string, string>;
}) {
  const res = await fetch(`${API_BASE}/requirements/generate-wrapper`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'Failed to generate custom extraction wrapper');
  }
  return res.json();
}

export async function validateWrapperCode(payload: {
  code: string;
  source_cds_view_name: string;
  sql_view_name: string;
  wrapper_label: string;
  wrapper_cds_name: string;
}) {
  const res = await fetch(`${API_BASE}/requirements/validate-wrapper`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(result?.detail || 'Failed to validate extraction wrapper');
  return result;
}

export async function createWrapperInSap(payload: {
  code: string;
  source_cds_view_name: string;
  sql_view_name: string;
  wrapper_label: string;
  wrapper_cds_name: string;
  package: string;
  transport_request: string;
  confirmed: boolean;
}) {
  const res = await fetch(`${API_BASE}/requirements/create-wrapper`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = result?.detail;
    throw new Error(typeof detail === 'string' ? detail : detail?.message || detail?.errors?.join(' ') || 'SAP CDS creation failed');
  }
  return result;
}

export async function fetchWrapperWriteCapability(transportRequest?: string): Promise<{
  write_enabled: boolean;
  reason?: string | null;
  eligible_transports?: Array<{ number: string; description?: string; owner?: string }>;
  transport_diagnostic?: string;
}> {
  const query = transportRequest ? `?transport_request=${encodeURIComponent(transportRequest)}` : '';
  const res = await fetch(`${API_BASE}/requirements/wrapper-write-capability${query}`);
  const result = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(result?.detail || 'Unable to read SAP write mode');
  return result;
}

export async function activateWrapperInSap(payload: {
  wrapper_cds_name: string;
  transport_request: string;
  confirmed: boolean;
}) {
  const res = await fetch(`${API_BASE}/requirements/activate-wrapper`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = result?.detail;
    throw new Error(typeof detail === 'string' ? detail : detail?.message || 'SAP CDS activation failed');
  }
  return result;
}

export async function validateMemoryOnly(code: string, verifiedContext: any) {
  const res = await fetch(`${API_BASE}/requirements/validate-memory`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, verified_context: verifiedContext }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail || 'In-memory validation failed');
  }
  return res.json();
}



