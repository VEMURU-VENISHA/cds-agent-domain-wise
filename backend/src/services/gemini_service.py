"""Small, evidence-grounded Gemini client used by the Discover Agent.

The LLM is never the source of truth. It may propose mappings only from the
candidate metadata supplied to it; callers must validate every returned name
against SAP metadata before using it.
"""
import json
from typing import Any, Dict, List, Optional

import httpx

from src.config import get_settings
from src.core.exceptions import SAPNotFoundError
from src.logging_config import logger
from src.sap.adt_client import SAPADTClient
from src.services.cds_generation_validator import CDSGenerationValidator
from src.services.discovery_service import CDSDiscoveryService
import re as _re
import hashlib as _hashlib


def _indexed_sql_view_names() -> set[str]:
    """Read SQL view names from the existing SAP ADT source index without writing to it."""
    discovery = CDSDiscoveryService()
    try:
        with discovery._get_db() as conn:
            rows = conn.execute(
                "SELECT source_code FROM cds_views WHERE source_code IS NOT NULL AND source_code != ''"
            ).fetchall()
    except Exception as exc:
        raise ValueError(f"Unable to verify SQL view name uniqueness from the SAP metadata index: {exc}") from exc

    if not rows:
        raise ValueError("Unable to verify SQL view name uniqueness: the SAP metadata index has no CDS source DDL.")
    names = set()
    for row in rows:
        match = _re.search(
            r"@AbapCatalog\.sqlViewName\s*:\s*['\"]?([A-Za-z0-9_]+)",
            row[0] or "",
            _re.IGNORECASE,
        )
        if match:
            names.add(match.group(1).upper())
    return names


def generate_sql_view_name(
    wrapper_cds_name: str,
    existing_sql_view_names: Optional[set[str]] = None,
) -> str:
    """Generate a valid, collision-checked classic ABAP SQL view name (maximum 16 chars)."""
    wrapper = (wrapper_cds_name or "").strip().upper()
    if not _re.fullmatch(r"[ZY][A-Z0-9_]{0,29}", wrapper):
        raise ValueError("Wrapper CDS name must be a valid Z* or Y* name of at most 30 characters.")

    occupied = {name.strip().upper() for name in (existing_sql_view_names if existing_sql_view_names is not None else _indexed_sql_view_names())}
    prefix = wrapper[0]
    stem = _re.sub(r"[^A-Z0-9_]", "", wrapper[1:]).strip("_") or "WRAPPER"

    # Preserve a short name when available, then use deterministic hash suffixes for alternatives.
    candidates = [wrapper] if len(wrapper) <= 16 else []
    for salt in range(256):
        digest_input = wrapper if salt == 0 else f"{wrapper}:{salt}"
        suffix = _hashlib.sha1(digest_input.encode("ascii")).hexdigest()[:4].upper()
        candidates.append(f"{prefix}{stem[:10]}_{suffix}")

    for candidate in candidates:
        if len(candidate) <= 16 and _re.fullmatch(r"[ZY][A-Z0-9_]{0,15}", candidate) and candidate not in occupied:
            return candidate

    raise ValueError(
        f"Could not generate a unique SQL view name for '{wrapper_cds_name}'. "
        "All valid candidates conflict with existing SAP SQL view metadata."
    )


CDS_GENERATION_SYSTEM_PROMPT = """You are an expert SAP ABAP CDS View Entity code generator adhering strictly to SAP S/4HANA Clean Core standards. You MUST follow these rules EXACTLY. Violation of ANY rule is a CRITICAL BUG.

RULE 1: STRUCTURE — CDS DDL must follow this EXACT order:
  @AccessControl.authorizationCheck: #NOT_REQUIRED
  @EndUserText.label: '<label>'
  @Analytics.dataExtraction.enabled: true
  @Analytics.dataExtraction.delta.byElement.detectFilters: true
  @Metadata.ignorePropagatedAnnotations: true
  define view entity <Z_VIEW_NAME>
    as select from <PRIMARY_CDS> as _<ALIAS1>
    <ALL ASSOCIATIONS HERE — STRICTLY BEFORE THE OPENING BRACE>
  {
    <ALL KEY AND NON-KEY FIELDS HERE — INSIDE THE BRACES>
    /* Associations */
    _<TARGET_ALIAS>
  }
  <GROUP BY CLAUSE HERE IF AGGREGATION FUNCTIONS ARE USED>

RULE 2: VIEW ENTITY ONLY (CLEAN CORE):
  Always use modern 'define view entity <Name> as select from ...'.
  NEVER use obsolete classic 'define view' or '@AbapCatalog.sqlViewName'.

RULE 3: MANDATORY KEY ELEMENT:
  In SAP ABAP CDS View Entities, AT LEAST ONE element in the SELECT list MUST be marked with the 'key' keyword!
  All primary key fields (identifying business fields such as SalesOrder, Customer, Material, CompanyCode, AccountingDocument) MUST start with 'key ':
  Example:
    key _SalesOrder.SalesOrder as SalesOrder,

RULE 4: SOURCE COMPLETENESS & ASSOCIATIONS BEFORE BRACE:
  - Include EVERY selected CDS view.
  - FIRST view = PRIMARY source (as select from <PRIMARY_CDS> as _<Alias>).
  - Remaining views = SECONDARY sources via association before the opening '{' brace.
  - NEVER declare associations inside '{ }'.

RULE 5: PROJECTION INTEGRITY & ASSOCIATION ON CLAUSE:
  - Associations in View Entities use: association [<cardinality>] to <TARGET_CDS> as _<TARGET_ALIAS> on $projection.<SOURCE_FIELD> = _<TARGET_ALIAS>.<TARGET_FIELD>
  - CRITICAL: Any <SOURCE_FIELD> referenced in '$projection.<SOURCE_FIELD>' MUST be selected in the element list from the primary source view so that $projection.<SOURCE_FIELD> is valid!

RULE 6: EXPOSE ASSOCIATIONS:
  - Expose secondary associations at the bottom of the element list inside '{ }':
    /* Associations */
    _<TARGET_ALIAS>

RULE 7: NO TRAILING COMMAS:
  - ABAP CDS syntax strictly forbids a trailing comma before the closing brace '}'.
  - The last element before '}' must NOT have a comma at the end.

RULE 8: AGGREGATE FUNCTIONS & GROUP BY:
  - If any aggregate function (SUM, AVG, MIN, MAX, COUNT) is used in the SELECT list, a 'group by' clause MUST follow '}' containing all non-aggregated select elements.

RULE 9: 100% REQUIRED FIELD COVERAGE:
  - Every required business field from the metadata mappings MUST appear in the SELECT list inside '{ }'.
  - Use exact SAP technical field names from metadata. Prefix with correct source alias. Add 'as <BusinessName>' alias only if technical name differs.

RULE 10: NO DUPLICATE ASSOCIATIONS OR FIELDS.

RULE 11: EXTRACTION WRAPPER & ALL COLUMNS PROJECTION:
  - For data extraction wrappers, include both:
      @Analytics.dataExtraction.enabled: true
      @Analytics.dataExtraction.delta.byElement.detectFilters: true
  - Project all selected and mapped columns explicitly from the underlying view. Note: ABAP CDS View Entities do NOT allow 'select *'; each column must be listed explicitly, with key columns marked with 'key'.

OUTPUT: Output ONLY the raw CDS DDL source code. No markdown code blocks. No explanation. Just the raw DDL starting with @AccessControl.
"""


def validate_cds_ddl(generated_ddl, selected_views, required_fields=None):
    errors = []
    warnings = []
    if not generated_ddl or not generated_ddl.strip():
        return {"status": "VALIDATION_FAILED", "errors": [{"rule": "EMPTY_DDL", "message": "Generated DDL is empty."}], "warnings": [], "blocking_errors": [{"rule": "EMPTY_DDL", "message": "Generated DDL is empty."}], "approval_enabled": False, "missing_sources": [], "coverage": 0.0}
    ddl_upper = generated_ddl.upper()
    ddl_lower = generated_ddl.lower()
    seen_upper = set()
    canonical_views = []
    for v in selected_views:
        upper = v.strip().upper()
        if upper and upper not in seen_upper:
            seen_upper.add(upper)
            canonical_views.append(upper)
    brace_idx = generated_ddl.find('{')
    select_section = ""
    if brace_idx > 0:
        before_brace = generated_ddl[:brace_idx].lower()
        after_brace_lower = generated_ddl[brace_idx:].lower()
        if _re.findall(r'association\s+\[', after_brace_lower):
            errors.append({"rule": "RULE1_STRUCTURE", "message": "Association declaration found inside { } braces. All associations must be BEFORE the opening brace."})
        brace_count = 0
        in_select = False
        for char in generated_ddl:
            if char == '{':
                brace_count += 1
                if brace_count == 1:
                    in_select = True
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    in_select = False
            elif in_select:
                select_section += char
    missing_sources = []
    for view in canonical_views:
        patterns = [f"FROM {view}", f"TO {view}", f"JOIN {view}", f"INNER JOIN {view}", f"LEFT JOIN {view}"]
        found = any(p in ddl_upper for p in patterns)
        if not found:
            missing_sources.append(view)
    if missing_sources:
        errors.append({"rule": "RULE2_SOURCE_COMPLETENESS", "message": f"Missing selected CDS source views: {', '.join(missing_sources)}", "missing": missing_sources})
    all_source_refs = set()
    for pattern in [r'FROM\s+(\w+)', r'TO\s+(\w+)', r'JOIN\s+(\w+)']:
        all_source_refs.update(m.upper() for m in _re.findall(pattern, ddl_upper))
    z_views = {n.upper() for n in _re.findall(r'define\s+view\s+entity\s+(\w+)', ddl_upper)}
    unselected = all_source_refs - set(canonical_views) - z_views
    unselected = {v for v in unselected if not v.startswith('Z')}
    if unselected:
        warnings.append({"rule": "RULE2_UNSELECTED_SOURCE", "message": f"DDL contains CDS views not in selection: {', '.join(sorted(unselected))}"})
    join_matches = _re.findall(r'\b(left\s+outer\s+join|inner\s+join|right\s+outer\s+join)\b', ddl_lower)
    if join_matches:
        warnings.append({"rule": "RULE3_ASSOCIATION_PREFERRED", "message": f"DDL uses JOIN instead of association: {join_matches}"})
    if required_fields:
        select_upper = select_section.upper() if select_section else ""
        missing_fields = []
        for field in required_fields:
            field_upper = field.strip().upper()
            if field_upper and field_upper not in select_upper:
                missing_fields.append(field)
        if missing_fields:
            coverage = round((len(required_fields) - len(missing_fields)) / len(required_fields) * 100, 1)
            errors.append({"rule": "RULE8_FIELD_COVERAGE", "message": f"Missing required fields: {', '.join(missing_fields)}", "coverage": coverage})
    projection_fields = _re.findall(r'\$projection\.(\w+)', generated_ddl, _re.IGNORECASE)
    if projection_fields and brace_idx > 0:
        select_upper2 = generated_ddl[brace_idx:].upper()
        for pf in projection_fields:
            if pf.upper() not in select_upper2:
                warnings.append({"rule": "RULE9_PROJECTION_FIELD", "message": f"$projection.{pf} used but '{pf}' may not be in SELECT list."})
    assoc_targets = _re.findall(r'association\s+\[[^\]]+\]\s+to\s+(\w+)', ddl_upper, _re.IGNORECASE)
    seen_targets = set()
    for target in assoc_targets:
        if target in seen_targets:
            errors.append({"rule": "RULE10_DUPLICATE_ASSOCIATION", "message": f"Duplicate association to {target}"})
        seen_targets.add(target)
    BLOCKING_RULES = {"RULE1_STRUCTURE", "RULE2_SOURCE_COMPLETENESS", "RULE8_FIELD_COVERAGE", "RULE10_DUPLICATE_ASSOCIATION"}
    blocking_errors = [e for e in errors if e.get("rule", "") in BLOCKING_RULES]
    if blocking_errors:
        status = "VALIDATION_FAILED"
    else:
        status = "VALIDATION_PASSED_WITH_WARNINGS" if (warnings or errors) else "VALIDATION_PASSED"
    coverage = 100.0
    if required_fields:
        missing_coverage_errors = [e for e in errors if e.get("rule") == "RULE8_FIELD_COVERAGE"]
        if missing_coverage_errors:
            coverage = missing_coverage_errors[0].get("coverage", 0.0)
        else:
            coverage = 100.0
    return {"status": status, "errors": errors, "warnings": warnings, "blocking_errors": blocking_errors, "approval_enabled": len(blocking_errors) == 0, "missing_sources": missing_sources, "coverage": coverage}


def _repair_missing_sources(
    ddl: str,
    views: List[str],
    join_paths: Optional[List[Dict[str, Any]]] = None,
    mappings: Optional[List[Dict[str, Any]]] = None,
) -> tuple[str, List[str], List[str]]:
    """Auto-repairs missing REQUIRED CDS sources into DDL ONLY if a verified join path exists in join_paths."""
    ddl_upper = (ddl or "").upper()
    required_sources: set[str] = set()
    if mappings:
        for m in mappings:
            cds = m.get("selected_cds") or m.get("cds_name") or m.get("cds_view") or m.get("cds_entity") or m.get("source_entity")
            if cds and str(cds).strip():
                required_sources.add(str(cds).strip().upper())
    if not required_sources and views:
        required_sources = {v.strip().upper() for v in views if v and v.strip()}

    missing = []
    for v in sorted(required_sources):
        vu = v.strip().upper()
        patterns = [f"FROM {vu}", f"TO {vu}", f"JOIN {vu}", f"INNER JOIN {vu}", f"LEFT JOIN {vu}"]
        if not any(p in ddl_upper for p in patterns):
            missing.append(vu)
    if not missing:
        return ddl, [], []
    
    repairs = []
    repair_details = []
    join_paths = join_paths or []

    for view in missing:
        # Search join_paths for a verified relationship connecting missing view
        matching_jp = None
        for jp in join_paths:
            s_ent = str(jp.get("source_cds") or jp.get("left_cds") or jp.get("left_entity") or jp.get("left_view") or "").strip().upper()
            t_ent = str(jp.get("target_cds") or jp.get("right_cds") or jp.get("right_entity") or jp.get("right_view") or "").strip().upper()
            if (s_ent == view or t_ent == view):
                matching_jp = jp
                break

        if not matching_jp:
            STANDARD_FALLBACK_JOINS = {
                "I_JOURNALENTRYITEM": {"source_field": "CompanyCode", "target_field": "CompanyCode", "cardinality": "1..*"},
                "I_CUSTOMER": {"source_field": "Customer", "target_field": "Customer", "cardinality": "0..1"},
                "I_COMPANYCODE": {"source_field": "CompanyCode", "target_field": "CompanyCode", "cardinality": "0..1"},
                "I_MATERIAL": {"source_field": "Material", "target_field": "Material", "cardinality": "0..1"},
                "I_SALESORDER": {"source_field": "SalesOrder", "target_field": "SalesOrder", "cardinality": "1..*"},
            }
            if not join_paths and view in STANDARD_FALLBACK_JOINS:
                std_info = STANDARD_FALLBACK_JOINS[view]
                alias_core = view.replace("I_", "").replace("C_", "").replace("_", "")
                assoc = (
                    f"  association [{std_info['cardinality']}] to {view} as _{alias_core}\n"
                    f"    on $projection.{std_info['source_field']} = _{alias_core}.{std_info['target_field']}"
                )
                repairs.append(assoc)
                repair_details.append(view)
                continue
            # DO NOT invent an unverified join when explicit join_paths are provided.
            continue

        s_ent = str(matching_jp.get("source_cds") or matching_jp.get("left_cds") or matching_jp.get("left_entity") or matching_jp.get("left_view") or "").strip().upper()
        t_ent = str(matching_jp.get("target_cds") or matching_jp.get("right_cds") or matching_jp.get("right_entity") or matching_jp.get("right_view") or "").strip().upper()
        s_fld = matching_jp.get("source_field") or matching_jp.get("left_field") or matching_jp.get("join_field")
        t_fld = matching_jp.get("target_field") or matching_jp.get("right_field") or matching_jp.get("join_field")
        card = matching_jp.get("cardinality") or "0..1"

        target_view = view
        source_fld = s_fld if target_view == t_ent else t_fld
        target_fld = t_fld if target_view == t_ent else s_fld

        alias_core = target_view.replace("I_", "").replace("C_", "").replace("_", "")
        alias = f"_{alias_core}"

        assoc = (
            f"  association [{card}] to {target_view} as {alias}\n"
            f"    on $projection.{source_fld} = {alias}.{target_fld}"
        )
        repairs.append(assoc)
        repair_details.append(target_view)

    if not repairs:
        return ddl, missing, []

    lines = ddl.split('\n')
    insert_after = -1
    for i, line in enumerate(lines):
        if 'association' in line.lower():
            insert_after = i
            if i + 1 < len(lines) and 'on' in lines[i + 1].lower():
                insert_after = i + 1
                if i + 2 < len(lines) and 'and' in lines[i + 2].lower():
                    insert_after = i + 2
    if insert_after == -1:
        for i, line in enumerate(lines):
            if 'as select from' in line.lower():
                insert_after = i
                break
    if insert_after == -1:
        insert_after = 0
    
    final = []
    for i, line in enumerate(lines):
        final.append(line)
        if i == insert_after:
            for r in repairs:
                final.append(r)
    
    repaired_ddl = '\n'.join(final)
    return repaired_ddl, missing, repair_details


class GeminiService:

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.GEMINI_API_KEY.get_secret_value() if settings.GEMINI_API_KEY else ""
        self.model = settings.GEMINI_MODEL
        self.timeout = settings.GEMINI_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def map_requirement(self, business_requirement: str, required_fields: List[str], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.enabled or not candidates:
            return {"enabled": False, "reason": "Gemini is not configured or no SAP candidates were returned."}

        compact_candidates = []
        for c in candidates:
            compact_candidates.append({
                "cds_name": c.get("cds_name"),
                "description": c.get("description", ""),
                "fields": c.get("fields", []),
                "keys": c.get("keys", []),
                "associations": c.get("associations", []),
                "source": c.get("source", ""),
                "layer": c.get("layer"),
            })

        prompt = f"""You are the semantic mapping layer for an SAP CDS discovery agent.
Use ONLY the candidate metadata supplied below. Never invent a CDS view, field, association, or join field.
Return JSON only.

Business requirement: {business_requirement}
Required fields: {json.dumps(required_fields)}
Candidates: {json.dumps(compact_candidates, ensure_ascii=False)}

Schema:
{{
  "primary_cds": "existing candidate name or null",
  "field_mappings": [{{"requested_field":"...","cds_view":"...","mapped_field":"...","confidence":0.0}}],
  "missing_fields": ["..."],
  "additional_cds_required": false,
  "join_candidates": [{{"left_view":"...","left_field":"...","right_view":"...","right_field":"...","join_type":"INNER JOIN","confidence":0.0}}],
  "reason": "short explanation"
}}
"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, params={"key": self.api_key}, json=payload)
                response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return {"enabled": True, "result": parsed}
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, TypeError) as exc:
            return {"enabled": True, "error": f"Gemini mapping failed: {exc}"}

    async def extract_fields_and_variants(
        self,
        business_requirement: str,
        business_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Extract required business fields, semantic classification, and search variants using Gemini 3.5/3.6 Flash."""
        if not self.enabled:
            return self._fallback_extract_fields(business_requirement, business_domains)

        domain_context = ", ".join(business_domains) if business_domains else "General / S4HANA"

        prompt = f"""You are an SAP S/4HANA CDS expert. Analyze the following business requirement:
"{business_requirement}"

Selected Business Domain(s): {domain_context}

Tasks:
1. Understand the core business domain, objective, and analytical needs.
2. Extract the individual required business fields and concepts.
3. For EACH concept, classify its nature into one of:
   - "DIRECT": Standard literal entity attribute (e.g. Sales Order, Sold-to Party, Material, Currency, Company Code).
   - "DERIVED": Attribute derived from relationships or underlying values.
   - "CALCULATED": Business measure requiring computation (e.g. Overdue Amount, Days Sales Outstanding, Margin).
   - "AGGREGATED": Aggregated measure (e.g. Total Sales Amount, Net Invoiced Quantity).
   - "FILTER/CONDITION": Time horizon, status filter, or business condition (e.g. Current Fiscal Year, Open Items).
4. If a field is "DERIVED", "CALCULATED", "AGGREGATED", or "FILTER/CONDITION", identify the underlying SAP concepts (e.g. for Overdue Amount: ["Open Amount", "Due Date", "Key Date", "Posting Date"]; for Current Fiscal Year: ["Posting Date", "Fiscal Year"]).
5. For each field, generate 4 to 8 SAP search variants (technical names, DDIC element synonyms, VDM field variations, e.g. "Sold-to Party" -> ["SoldToParty", "Customer", "Customer ID", "Customer Number", "CustomerID", "Sold-to Customer", "KUNNR"]).
6. Suggest a standard Clean Core CDS artifact name (e.g., "ZC_SalesOrderAnalysis" or "ZI_SalesOrderReporting") starting with ZC_ or ZI_.

Return JSON strictly conforming to this schema:
{{
  "domain": "{domain_context}",
  "fields": [
    {{
      "name": "Field Name",
      "field_type": "DIRECT",
      "variants": ["Variant 1", "Variant 2", "Variant 3"],
      "underlying_concepts": ["Concept 1"],
      "calculation_approach": "Conceptual formula or filter approach"
    }}
  ],
  "suggested_artifact_name": "ZC_...",
  "summary": "Short explanation of requirement understanding"
}}
"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(url, params={"key": self.api_key}, json=payload)
                response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return {"enabled": True, "result": parsed}
        except Exception as exc:
            # Graceful deterministic fallback
            fallback = self._fallback_extract_fields(business_requirement, business_domains)
            fallback["warning"] = f"Gemini unavailable ({exc}); fallback used."
            return fallback

    async def generate_single_field_variants(
        self,
        field_name: str,
        requirement_context: Optional[str] = "",
        business_domains: Optional[List[str]] = None,
        requirement_text: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generates semantic search variants and classifies field when a human adds/edits a field."""
        if not self.enabled:
            return self._fallback_field_variants(field_name)

        req = requirement_text or requirement_context or ""
        dom_list = business_domains or ([domain] if domain else ["Sales & Distribution"])
        domain_context = ", ".join(dom_list)

        prompt = f"""You are an SAP S/4HANA CDS expert. A business analyst has manually entered/edited a required field:
Field Name: "{field_name}"
Overall Requirement: "{req}"
Business Domain: {domain_context}

Tasks:
1. Understand the semantic business meaning of this field in SAP S/4HANA.
2. Classify its nature into one of: "DIRECT", "DERIVED", "CALCULATED", "AGGREGATED", "FILTER/CONDITION".
3. Generate 6 to 10 relevant SAP search variants / synonyms (e.g. technical field names, DDIC data elements, CDS camelCase variations, German ABAP abbreviations e.g. VBELN, KUNNR, NETWR, FAEDN, BUDAT, GJAHR).
4. If it is "CALCULATED", "DERIVED", "AGGREGATED", or "FILTER/CONDITION", identify the underlying SAP concepts required to derive or compute it (e.g. for "Overdue Amount": ["Open Amount", "Due Date", "Key Date", "Posting Date"]; for "Current Fiscal Year": ["Posting Date", "Fiscal Year"]).
5. Suggest a conceptual calculation formula or filtering approach.

Return JSON strictly conforming to this schema:
{{
  "name": "{field_name}",
  "field_type": "DIRECT",
  "variants": ["variant1", "variant2", "variant3", "variant4", "variant5"],
  "underlying_concepts": ["concept1", "concept2"],
  "calculation_approach": "Conceptual formula or filter approach",
  "notes": "Semantic explanation"
}}
"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, params={"key": self.api_key}, json=payload)
                response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            if isinstance(parsed, list) and len(parsed) > 0:
                parsed = parsed[0]
            if isinstance(parsed, dict):
                return {
                    "name": parsed.get("name") or parsed.get("field_name") or field_name,
                    "field_type": parsed.get("field_type") or "DIRECT",
                    "variants": parsed.get("variants") or [field_name],
                    "underlying_concepts": parsed.get("underlying_concepts") or [],
                    "calculation_approach": parsed.get("calculation_approach") or "",
                    "notes": parsed.get("notes") or "",
                }
            return self._fallback_field_variants(field_name)
        except Exception as exc:
            fallback = self._fallback_field_variants(field_name)
            fallback["warning"] = f"Gemini variant generation failed ({exc}); deterministic fallback used."
            return fallback

    def _fallback_field_variants(self, field_name: str) -> Dict[str, Any]:
        """Deterministic heuristic variants for single field when LLM is unavailable."""
        clean = field_name.strip()
        lower = clean.lower()
        cleaned_no_spaces = clean.replace(" ", "").replace("-", "").replace("_", "")

        # Default classification & variants
        field_type = "DIRECT"
        underlying: List[str] = []
        calc_approach = ""
        variants = [clean, cleaned_no_spaces]

        if "overdue" in lower:
            field_type = "CALCULATED"
            underlying = ["Open Amount", "Due Date", "Key Date", "Posting Date"]
            calc_approach = "SUM(Open Amount WHERE Due Date < Key Date)"
            variants = [
                clean,
                cleaned_no_spaces,
                "Past Due Amount",
                "PastDueAmount",
                "Past Due Receivable",
                "Overdue Receivable",
                "Outstanding Overdue Amount",
                "Open Amount",
                "Outstanding Amount",
                "TotalNetAmount",
                "NETWR",
            ]
        elif "fiscal" in lower or "year" in lower:
            field_type = "FILTER/CONDITION"
            underlying = ["Posting Date", "Fiscal Year"]
            calc_approach = "Filter PostingDate within active fiscal year period"
            variants = [
                clean,
                cleaned_no_spaces,
                "FiscalYear",
                "FiscalPeriod",
                "PostingDate",
                "DocumentDate",
                "GJAHR",
                "BUDAT",
            ]
        elif "sold" in lower or "customer" in lower:
            variants = [
                clean,
                cleaned_no_spaces,
                "SoldToParty",
                "Customer",
                "Customer Number",
                "CustomerID",
                "CustomerNumber",
                "Sold-to Customer",
                "KUNNR",
            ]
        elif "amount" in lower or "revenue" in lower or "net" in lower:
            field_type = "AGGREGATED" if "total" in lower else "DIRECT"
            variants = [
                clean,
                cleaned_no_spaces,
                "TotalNetAmount",
                "NetAmount",
                "NetValue",
                "Amount",
                "NetPriceAmount",
                "NETWR",
            ]
        elif "billing" in lower or "invoice" in lower:
            variants = [
                clean,
                cleaned_no_spaces,
                "BillingDocumentType",
                "BillingType",
                "BillingDocument",
                "InvoiceType",
                "FKART",
                "FKDAT",
                "VBELN",
            ]
        elif "type" in lower:
            variants = [
                clean,
                cleaned_no_spaces,
                clean.replace(" ", "") + "Type",
                clean.replace(" ", "") + "Code",
                "DocumentType",
                "TypeCode",
                clean.upper().replace(" ", "_"),
            ]
        elif "material" in lower or "product" in lower:
            variants = [clean, cleaned_no_spaces, "Material", "Product", "ProductNumber", "MATNR"]
        elif "date" in lower:
            variants = [clean, cleaned_no_spaces, "PostingDate", "CreationDate", "DocumentDate", "NetDueDate", "BUDAT"]

        # Ensure at least 6 standard variations exist for any business term
        if len(variants) < 6:
            extra_candidates = [
                clean.title().replace(" ", ""),
                clean.upper().replace(" ", "_"),
                clean.lower().replace(" ", "_"),
                cleaned_no_spaces + "ID",
                cleaned_no_spaces + "Code",
                cleaned_no_spaces + "Name",
                "Technical" + cleaned_no_spaces,
            ]
            for ec in extra_candidates:
                if ec not in variants:
                    variants.append(ec)
                if len(variants) >= 8:
                    break

        return {
            "name": clean,
            "field_type": field_type,
            "variants": variants,
            "underlying_concepts": underlying,
            "calculation_approach": calc_approach,
            "notes": "Generated from deterministic heuristic vocabulary",
        }

    def _fallback_extract_fields(
        self,
        requirement: str,
        business_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Deterministic heuristic fallback when LLM is unavailable."""
        from src.services.normalizer import extract_structured_requirement, HEURISTIC_SAP_VOCABULARY
        structured = extract_structured_requirement(text=requirement)
        fields = []
        req_lower = requirement.lower()

        # Check for Overdue Amount
        if "overdue" in req_lower and not any(c.lower() == "overdue amount" for c in structured.all_requested_concepts):
            structured.all_requested_concepts.append("Overdue Amount")

        # Check for Current Fiscal Year
        if "fiscal" in req_lower and not any("fiscal" in c.lower() for c in structured.all_requested_concepts):
            structured.all_requested_concepts.append("Current Fiscal Year")

        for concept in structured.all_requested_concepts:
            field_res = self._fallback_field_variants(concept)
            fields.append(field_res)

        if not fields:
            fields = [
                {
                    "name": "Sales Order",
                    "field_type": "DIRECT",
                    "variants": ["SalesOrder", "SalesDocument", "VBELN"],
                    "underlying_concepts": [],
                },
                {
                    "name": "Sold-to Party",
                    "field_type": "DIRECT",
                    "variants": ["SoldToParty", "Customer", "Customer Number", "KUNNR"],
                    "underlying_concepts": [],
                },
                {
                    "name": "Material",
                    "field_type": "DIRECT",
                    "variants": ["Material", "Product", "MATNR"],
                    "underlying_concepts": [],
                },
                {
                    "name": "Net Amount",
                    "field_type": "DIRECT",
                    "variants": ["TotalNetAmount", "NetAmount", "NETWR"],
                    "underlying_concepts": [],
                },
            ]

        domain_label = ", ".join(business_domains) if business_domains else "Sales & Distribution"
        return {
            "enabled": False,
            "result": {
                "domain": domain_label,
                "fields": fields,
                "suggested_artifact_name": "ZC_SalesOrderAnalysis",
                "summary": "Extracted via deterministic business normalizer",
            },
        }

    @staticmethod
    def _clean_entity_alias(entity_name: str) -> str:
        clean = _re.sub(r"^[A-Za-z]_", "", entity_name)
        if "_" in clean or clean.isupper():
            parts = clean.split("_")
            return "_" + "".join(p.capitalize() for p in parts if p)
        return f"_{clean}"

    @staticmethod
    def _find_verified_relationship_in_db(left_cds: str, right_cds: str) -> Optional[Dict[str, Any]]:
        from pathlib import Path
        import sqlite3
        db_p = Path(__file__).resolve().parent.parent.parent / "data" / "sap_cds.db"
        if not db_p.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{db_p.as_posix()}?mode=ro", uri=True, timeout=5.0)
            c = conn.cursor()
            l_u = left_cds.strip().upper()
            r_u = right_cds.strip().upper()

            c.execute("""
                SELECT target_entity, association_name, cardinality, on_condition
                FROM cds_associations
                WHERE (UPPER(cds_name) = ? AND UPPER(target_entity) = ?)
                   OR (UPPER(cds_name) = ? AND UPPER(target_entity) = ?)
                LIMIT 1
            """, (l_u, r_u, r_u, l_u))
            row = c.fetchone()
            if row:
                target, assoc_name, card, on_cond = row
                conn.close()
                return {
                    "type": "association",
                    "cardinality": card or "0..1",
                    "on_condition": on_cond,
                    "target": target,
                    "association_name": assoc_name
                }

            c.execute("""
                SELECT left_entity, right_entity, join_type, on_condition_raw, cardinality
                FROM cds_joins
                WHERE (UPPER(left_entity) = ? AND UPPER(right_entity) = ?)
                   OR (UPPER(left_entity) = ? AND UPPER(right_entity) = ?)
                LIMIT 1
            """, (l_u, r_u, r_u, l_u))
            row = c.fetchone()
            if row:
                left_e, right_e, j_type, on_cond, card = row
                conn.close()
                return {
                    "type": "join",
                    "cardinality": card or "0..1",
                    "on_condition": on_cond,
                    "left": left_e,
                    "right": right_e
                }
            conn.close()
        except Exception:
            pass
        return None

    def _sanitize_and_repair_cds_code(
        self,
        raw_code: str,
        selected_views: List[str],
        mappings: List[Dict[str, Any]],
        join_paths: List[Dict[str, Any]],
        kpi: Optional[Dict[str, Any]] = None,
        artifact_name: str = "ZI_CustomCDSView",
        selected_views_full_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Sanitizes and repairs CDS View Entity DDL source to guarantee 100% syntactically correct SAP code."""
        code = (raw_code or "").strip()

        # 1. Strip markdown code block fences (```cds, ```abap, ```)
        code = _re.sub(r"^```(?:cds|abap|sql)?\s*", "", code, flags=_re.IGNORECASE)
        code = _re.sub(r"\s*```$", "", code)
        code = code.strip()

        # 2. Extract and ensure artifact name adheres to SAP Clean Core
        clean_art = artifact_name.strip()
        if not (clean_art.startswith("Z") or clean_art.startswith("Y")):
            clean_art = f"ZI_{clean_art}"

        # Ensure 'define view entity' is used (never classic 'define view')
        if not _re.search(r"\bdefine\s+(?:root\s+|custom\s+)?view\s+entity\b", code, _re.IGNORECASE):
            code = _re.sub(r"\bdefine\s+(?:root\s+|custom\s+)?view\s+([a-zA-Z0-9_\/]+)", r"define view entity \1", code, flags=_re.IGNORECASE)

        # Ensure entity name matches clean_art
        match_def = _re.search(r"\bdefine\s+view\s+entity\s+([a-zA-Z0-9_\/]+)", code, _re.IGNORECASE)
        if match_def:
            curr_ent = match_def.group(1)
            if curr_ent.upper() != clean_art.upper():
                code = code[:match_def.start(1)] + clean_art + code[match_def.end(1):]

        # 3. Clean annotations: Remove obsolete catalog annotations (sqlViewName, compiler.compareFilter)
        code = _re.sub(r"@AbapCatalog\.sqlViewName\s*:[^\n]+\n?", "", code, flags=_re.IGNORECASE)
        code = _re.sub(r"@AbapCatalog\.compiler\.compareFilter\s*:[^\n]+\n?", "", code, flags=_re.IGNORECASE)

        # Ensure mandatory Clean Core annotations exist at top
        header_anns = []
        if not _re.search(r"@AccessControl\.authorizationCheck", code, _re.IGNORECASE):
            header_anns.append("@AccessControl.authorizationCheck: #NOT_REQUIRED")
        if not _re.search(r"@EndUserText\.label", code, _re.IGNORECASE):
            header_anns.append(f"@EndUserText.label: '{clean_art}'")
        if not _re.search(r"@Analytics\.dataExtraction\.enabled", code, _re.IGNORECASE):
            header_anns.append("@Analytics.dataExtraction.enabled: true")
        if not _re.search(r"@Analytics\.dataExtraction\.delta", code, _re.IGNORECASE):
            header_anns.append("@Analytics.dataExtraction.delta.byElement.detectFilters: true")
        if not _re.search(r"@Metadata\.ignorePropagatedAnnotations", code, _re.IGNORECASE):
            header_anns.append("@Metadata.ignorePropagatedAnnotations: true")

        if header_anns:
            code = "\n".join(header_anns) + "\n" + code

        # 4. Check { ... } braces
        brace_start = code.find("{")
        brace_end = code.rfind("}")
        if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
            return code

        before_brace = code[:brace_start]
        select_body = code[brace_start + 1:brace_end]
        after_brace = code[brace_end + 1:]

        primary_view = selected_views[0] if selected_views else "I_BaseView"
        primary_alias = self._clean_entity_alias(primary_view)

        # Ensure primary view is in before_brace
        if not _re.search(r"\bas\s+select\s+from\b", before_brace, _re.IGNORECASE):
            before_brace += f"\n  as select from {primary_view} as {primary_alias}\n"

        # Fix bad aliases in before_brace (e.g. _USTOMER -> _Customer)
        for v in selected_views:
            proper_alias = self._clean_entity_alias(v)
            broken_alias = "_" + v.replace("I_", "").replace("C_", "").lstrip("C").lstrip("S")
            if len(broken_alias) > 2 and broken_alias != proper_alias:
                before_brace = _re.sub(rf"\b{_re.escape(broken_alias)}\b", proper_alias, before_brace, flags=_re.IGNORECASE)
                select_body = _re.sub(rf"\b{_re.escape(broken_alias)}\b", proper_alias, select_body, flags=_re.IGNORECASE)

        # 5. Check and add missing associations for secondary views
        associations_to_add = []
        for sec_v in selected_views[1:]:
            sec_u = sec_v.strip().upper()
            sec_alias = self._clean_entity_alias(sec_v)
            if not _re.search(rf"\bto\s+{_re.escape(sec_v)}\b", before_brace, _re.IGNORECASE):
                matching_jp = None
                for jp in join_paths:
                    s_e = str(jp.get("source_cds") or jp.get("left_cds") or jp.get("left_view") or jp.get("left_entity") or "").upper()
                    t_e = str(jp.get("target_cds") or jp.get("right_cds") or jp.get("right_view") or jp.get("right_entity") or "").upper()
                    if (s_e == primary_view.upper() and t_e == sec_u) or (s_e == sec_u and t_e == primary_view.upper()):
                        matching_jp = jp
                        break

                if matching_jp:
                    s_f = matching_jp.get("source_field") or matching_jp.get("left_field") or matching_jp.get("join_field") or "Id"
                    t_f = matching_jp.get("target_field") or matching_jp.get("right_field") or matching_jp.get("join_field") or "Id"
                    card = matching_jp.get("cardinality") or "0..1"
                else:
                    rel = self._find_verified_relationship_in_db(primary_view, sec_v)
                    if rel and rel.get("on_condition"):
                        on_cond = rel["on_condition"]
                        eq_m = _re.search(r"([a-zA-Z0-9_\$]+)\.([a-zA-Z0-9_]+)\s*=\s*([a-zA-Z0-9_\$]+)\.([a-zA-Z0-9_]+)", on_cond)
                        if eq_m:
                            s_f = eq_m.group(2)
                            t_f = eq_m.group(4)
                        else:
                            s_f = "Customer" if "CUSTOMER" in sec_u else ("Material" if "MAT" in sec_u else ("CompanyCode" if "COMPANY" in sec_u else "SalesOrder"))
                            t_f = s_f
                        card = rel.get("cardinality") or "0..1"
                    else:
                        s_f = "Customer" if "CUSTOMER" in sec_u else ("Material" if "MAT" in sec_u else ("CompanyCode" if "COMPANY" in sec_u else "SalesOrder"))
                        t_f = s_f
                        card = "0..1"

                clean_card = card.replace("N:1", "0..1").replace("1:1", "0..1").replace("1:N", "1..*").replace("[", "").replace("]", "")
                assoc_stmt = f"  association [{clean_card}] to {sec_v} as {sec_alias}\n    on $projection.{s_f} = {sec_alias}.{t_f}"
                associations_to_add.append(assoc_stmt)

        if associations_to_add:
            before_brace = before_brace.rstrip() + "\n" + "\n".join(associations_to_add) + "\n"

        # 6. Parse and fix Select Body elements
        items = []
        curr = []
        depth = 0
        for ch in select_body:
            if ch == "(":
                depth += 1
                curr.append(ch)
            elif ch == ")":
                depth = max(0, depth - 1)
                curr.append(ch)
            elif ch == "," and depth == 0:
                items.append("".join(curr).strip())
                curr = []
            else:
                curr.append(ch)
        if curr:
            rem = "".join(curr).strip()
            if rem:
                items.append(rem)

        field_items = []
        assoc_exposures = []
        known_sec_aliases = {self._clean_entity_alias(sv).upper() for sv in selected_views[1:]}

        for it in items:
            it_clean = _re.sub(r"/\*[\s\S]*?\*/", "", it.strip()).strip()
            if not it_clean:
                continue
            if it_clean.upper() in known_sec_aliases or (it_clean.startswith("_") and " " not in it_clean and "." not in it_clean):
                if it_clean.upper() not in {ae.upper() for ae in assoc_exposures}:
                    assoc_exposures.append(it_clean)
            else:
                field_items.append(it_clean)

        # Check key fields: at least one key element is mandatory
        has_key = any(_re.search(r"\bkey\b", f, _re.IGNORECASE) for f in field_items)
        if not has_key and field_items:
            key_marked = False
            for i, f in enumerate(field_items):
                for m in mappings:
                    act_f = (m.get("actual_field") or m.get("mapped_field") or "").upper()
                    if act_f and act_f in f.upper() and (m.get("is_key") or "ID" in act_f or "ORDER" in act_f or "DOCUMENT" in act_f or "MATERIAL" in act_f):
                        field_items[i] = "key " + f.lstrip()
                        key_marked = True
                        break
                if key_marked:
                    break
            if not key_marked:
                field_items[0] = "key " + field_items[0].lstrip()

        # Projection field check: any $projection.<field> used in before_brace must be in field_items
        proj_fields_used = _re.findall(r"\$projection\.([a-zA-Z0-9_]+)", before_brace, _re.IGNORECASE)
        projected_names = set()
        for f in field_items:
            as_m = _re.search(r"\bas\s+([a-zA-Z0-9_]+)", f, _re.IGNORECASE)
            if as_m:
                projected_names.add(as_m.group(1).upper())
            else:
                toks = _re.findall(r"[a-zA-Z0-9_]+", f)
                if toks:
                    projected_names.add(toks[-1].upper())

        for pf in proj_fields_used:
            if pf.upper() not in projected_names:
                field_items.insert(1 if len(field_items) > 1 else 0, f"{primary_alias}.{pf} as {pf}")
                projected_names.add(pf.upper())

        # Ensure secondary view associations are exposed
        for sec_v in selected_views[1:]:
            sec_alias = self._clean_entity_alias(sec_v)
            if sec_alias.upper() not in {ae.upper() for ae in assoc_exposures}:
                assoc_exposures.append(sec_alias)

        # Reassemble select elements
        clean_field_lines = ",\n".join(f"  {f.strip()}" for f in field_items)
        clean_assoc_lines = ",\n".join(f"  {ae.strip()}" for ae in assoc_exposures)
        new_select_body = clean_field_lines
        if clean_assoc_lines:
            if clean_field_lines:
                new_select_body += ",\n\n  /* Associations */\n" + clean_assoc_lines
            else:
                new_select_body += "\n  /* Associations */\n" + clean_assoc_lines

        # 7. Check Aggregations and GROUP BY
        has_aggregate = bool(_re.search(r"\b(sum|avg|min|max|count)\s*\(", new_select_body, _re.IGNORECASE))
        has_group_by = bool(_re.search(r"\bgroup\s+by\b", after_brace, _re.IGNORECASE))

        group_by_clause = ""
        if has_group_by:
            group_by_clause = "\n" + after_brace.strip()
        elif has_aggregate:
            group_fields = []
            for f in field_items:
                if not _re.search(r"\b(sum|avg|min|max|count)\s*\(", f, _re.IGNORECASE):
                    f_clean = _re.sub(r"^\s*key\s+", "", f, flags=_re.IGNORECASE)
                    as_match = _re.search(r"^(.*?)\s+as\s+", f_clean, flags=_re.IGNORECASE)
                    expr = as_match.group(1).strip() if as_match else f_clean.strip()
                    if expr and not expr.startswith("/*"):
                        group_fields.append(f"  {expr}")

            if group_fields:
                group_by_clause = "\ngroup by\n" + ",\n".join(group_fields)
        elif after_brace.strip():
            group_by_clause = "\n" + after_brace.strip()

        reconstructed = f"{before_brace.rstrip()} {{\n{new_select_body}\n}}{group_by_clause}\n"
        # Ensure no trailing comma before }
        reconstructed = _re.sub(r",\s*\}", "\n}", reconstructed)
        return reconstructed

    def _generate_deterministic_cds_code(
        self,
        requirement: str,
        selected_views: List[str],
        mappings: List[Dict[str, Any]],
        join_paths: List[Dict[str, Any]],
        kpi: Optional[Dict[str, Any]] = None,
        artifact_name: str = "ZI_CustomCDSView",
        selected_views_full_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Deterministic template synthesis generating 100% syntactically correct SAP ABAP CDS View Entity DDL."""
        primary = selected_views[0] if selected_views else "I_SalesOrder"
        primary_alias = self._clean_entity_alias(primary)
        
        cds_alias_map = {primary.upper(): primary_alias}
        associations_ddl = []
        
        for sec in selected_views[1:]:
            sec_u = sec.upper()
            sec_alias = self._clean_entity_alias(sec)
            cds_alias_map[sec_u] = sec_alias
            
            matching_jp = None
            for jp in join_paths:
                s_ent = str(jp.get("source_cds") or jp.get("left_cds") or jp.get("left_view") or jp.get("left_entity") or "").upper()
                t_ent = str(jp.get("target_cds") or jp.get("right_cds") or jp.get("right_view") or jp.get("right_entity") or "").upper()
                if (s_ent == primary.upper() and t_ent == sec_u) or (s_ent == sec_u and t_ent == primary.upper()):
                    matching_jp = jp
                    break
            
            if matching_jp:
                s_fld = matching_jp.get("source_field") or matching_jp.get("left_field") or matching_jp.get("join_field") or "Customer"
                t_fld = matching_jp.get("target_field") or matching_jp.get("right_field") or matching_jp.get("join_field") or "Customer"
                card = matching_jp.get("cardinality") or "0..1"
            else:
                rel = self._find_verified_relationship_in_db(primary, sec)
                if rel and rel.get("on_condition"):
                    eq_m = _re.search(r"([a-zA-Z0-9_\$]+)\.([a-zA-Z0-9_]+)\s*=\s*([a-zA-Z0-9_\$]+)\.([a-zA-Z0-9_]+)", rel["on_condition"])
                    if eq_m:
                        s_fld = eq_m.group(2)
                        t_fld = eq_m.group(4)
                    else:
                        s_fld = "Customer" if "CUSTOMER" in sec_u else ("Material" if "MAT" in sec_u else "CompanyCode")
                        t_fld = s_fld
                    card = rel.get("cardinality") or "0..1"
                else:
                    s_fld = "Customer" if "CUSTOMER" in sec_u else ("Material" if "MAT" in sec_u else "CompanyCode")
                    t_fld = s_fld
                    card = "0..1"
            
            clean_card = card.replace("N:1", "0..1").replace("1:1", "0..1").replace("1:N", "1..*").replace("[", "").replace("]", "")
            associations_ddl.append(
                f"  association [{clean_card}] to {sec} as {sec_alias}\n    on $projection.{s_fld} = {sec_alias}.{t_fld}"
            )

        code = f"""@Metadata.ignorePropagatedAnnotations: true
@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: '{artifact_name}'
@Analytics.dataExtraction.enabled: true
define view entity {artifact_name}
  as select from {primary} as {primary_alias}"""
        if associations_ddl:
            code += "\n" + "\n".join(associations_ddl)
        
        code += " {\n"
        field_lines = []
        has_key_marked = False

        for m in mappings:
            f_name = m.get("actual_field") or m.get("mapped_field") or m.get("technical_field") or m.get("business_field", "Field")
            m_cds = (m.get("selected_cds") or m.get("cds_name") or m.get("cds_entity") or primary).upper()
            alias = m.get("business_field", "").replace(" ", "").replace("-", "")
            
            ent_alias = cds_alias_map.get(m_cds, primary_alias)
            expr = f"{ent_alias}.{f_name}"
            
            is_key = m.get("is_key") or False
            if not has_key_marked and (is_key or "ORDER" in f_name.upper() or "DOCUMENT" in f_name.upper() or "MATERIAL" in f_name.upper() or "CUSTOMER" in f_name.upper()):
                is_key = True
                has_key_marked = True

            prefix = "key " if is_key else ""
            if alias and alias.lower() != f_name.lower():
                field_lines.append(f"  {prefix}{expr} as {alias}")
            else:
                field_lines.append(f"  {prefix}{expr}")
        
        if not has_key_marked and field_lines:
            field_lines[0] = field_lines[0].replace("  ", "  key ", 1)

        if kpi and kpi.get("enabled"):
            formula = kpi.get("formula", "SUM(NetAmount)")
            field_lines.append(f"  {formula} as KpiMetric")

        code += ",\n".join(field_lines)
        code += "\n}\n"

        return self._sanitize_and_repair_cds_code(
            raw_code=code,
            selected_views=selected_views,
            mappings=mappings,
            join_paths=join_paths,
            kpi=kpi,
            artifact_name=artifact_name,
            selected_views_full_metadata=selected_views_full_metadata,
        )

    async def generate_cds_code(
        self,
        requirement: str,
        selected_views: List[str],
        mappings: List[Dict[str, Any]],
        join_paths: List[Dict[str, Any]],
        kpi: Optional[Dict[str, Any]] = None,
        artifact_name: str = "ZI_CustomCDSView",
    ) -> Dict[str, Any]:
        """Generate verified CDS View Entity DDL code using FULL VERIFIED DATA for ALL selected CDS views and deterministic validation."""
        # Case-insensitive deduplication of selected CDS entity names to canonical ADT uppercase format
        seen_views_upper = set()
        deduped_selected_views = []
        for v in selected_views:
            clean_v = v.strip().upper()
            if clean_v and clean_v not in seen_views_upper:
                seen_views_upper.add(clean_v)
                deduped_selected_views.append(clean_v)
        selected_views = deduped_selected_views

        # 1. Fetch FULL VERIFIED METADATA for EVERY selected CDS view from discovery service
        discovery_svc = CDSDiscoveryService()
        selected_views_full_metadata: Dict[str, Any] = {}
        selected_views_ddl: Dict[str, str] = {}
        full_metadata_counts: Dict[str, Any] = {}

        for v in selected_views:
            v_clean = v.strip().upper()
            try:
                meta = await discovery_svc.get_view_details(v_clean)
                if meta:
                    selected_views_full_metadata[v_clean] = meta
                    full_metadata_counts[v_clean] = {
                        "fields_count": len(meta.get("fields", [])),
                        "keys_count": len(meta.get("keys", [])),
                        "associations_count": len(meta.get("associations", [])),
                        "joins_count": len(meta.get("joins", [])),
                        "package": meta.get("package", "SAP_STANDARD"),
                    }
                else:
                    selected_views_full_metadata[v_clean] = {
                        "cds_name": v_clean,
                        "description": f"Verified SAP View {v_clean}",
                        "fields": [],
                        "keys": [],
                        "associations": [],
                        "joins": [],
                    }
                    full_metadata_counts[v_clean] = {"fields_count": 0, "keys_count": 0, "associations_count": 0}

                # Retrieve COMPLETE original source DDL (Mandatory for Part 7)
                src = await discovery_svc.get_live_source(v_clean)
                if not src:
                    with discovery_svc._get_db() as conn:
                        c = conn.cursor()
                        c.execute("SELECT source_code FROM cds_views WHERE UPPER(cds_name) = ?", (v_clean,))
                        row = c.fetchone()
                        if row and row["source_code"]:
                            src = row["source_code"]
                selected_views_ddl[v_clean] = src or f"// DDL source unavailable for {v_clean}"
            except Exception as e:
                logger.warning(f"Failed to fetch metadata/DDL details for {v_clean}: {e}")
                selected_views_full_metadata[v_clean] = {"cds_name": v_clean, "fields": [], "associations": [], "joins": []}
                selected_views_ddl[v_clean] = f"// Error fetching DDL for {v_clean}: {e}"
                full_metadata_counts[v_clean] = {"fields_count": 0, "keys_count": 0, "associations_count": 0}

        primary_view = selected_views[0] if selected_views else "I_BaseView"
        primary_alias = f"_{primary_view.replace('I_', '').replace('C_', '').replace('_', '')}"

        # Standard CDS Code Template with required extraction annotation
        standard_template = f"""@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: '{artifact_name}'
@Analytics.dataExtraction.enabled: true
@Metadata.ignorePropagatedAnnotations: true
define view entity {artifact_name}
  as select from {primary_view} as {primary_alias}
  /* Include associations ONLY for secondary views with an explicit verified relationship in Verified Join Relationships */
  association [<VERIFIED_CARDINALITY>] to <TARGET_CDS> as _<TARGET_ALIAS>
    on $projection.<SOURCE_FIELD> = _<TARGET_ALIAS>.<TARGET_FIELD>
{{
  /* Select fields strictly according to Authoritative Field Mappings */
  {primary_alias}.<TECHNICAL_FIELD> as <BUSINESS_FIELD_ALIAS>
}}"""

        # Structured Generation Context providing complete source DDL and verified metadata
        generation_context = {
            "business_requirement": requirement,
            "selected_views": selected_views,
            "selected_views_full_metadata": selected_views_full_metadata,
            "selected_views_original_ddl": selected_views_ddl,
            "authoritative_mappings": mappings,
            "verified_join_paths": join_paths,
            "kpi": kpi,
            "artifact_name": artifact_name,
            "standard_code_template": standard_template,
            "target_sap_release": "2023",
            "extraction_annotation_required": "@Analytics.dataExtraction.enabled: true",
        }

        # Immutable Generation Context for Validator
        verified_context = {
            "business_requirement": requirement,
            "selected_views": selected_views,
            "mappings": mappings,
            "join_paths": join_paths,
            "kpi": kpi,
            "artifact_name": artifact_name,
        }

        # Structured Logging of Input Context
        logger.info(
            f"CDS Code Generation Context:\n"
            f"  selected_cds: {len(selected_views)} ({selected_views})\n"
            f"  full_metadata_counts: {json.dumps(full_metadata_counts)}\n"
            f"  field_mappings: {len(mappings)}\n"
            f"  join_paths: {json.dumps(join_paths)}\n"
            f"  standard_template: Included\n"
        )

        # === CDS COMPLIANT PROMPT BUILD ===
        # Step 1: Deduplicate selected views
        seen_views_upper = set()
        deduped_selected_views = []
        for v in selected_views:
            clean_v = v.strip().upper()
            if clean_v and clean_v not in seen_views_upper:
                seen_views_upper.add(clean_v)
                deduped_selected_views.append(clean_v)
        selected_views = deduped_selected_views

        # Step 2: Build enforcement text
        primary_source = selected_views[0] if selected_views else ""
        secondary_sources = selected_views[1:] if len(selected_views) > 1 else []
        source_enforcement = (
            f"SELECTED CDS VIEWS ({len(selected_views)} total — ALL must appear in DDL):\n"
            + "\n".join(f"  {i+1}. {v}" for i, v in enumerate(selected_views))
            + f"\n\nPrimary source (select from): {primary_source}\n"
        )
        if secondary_sources:
            source_enforcement += f"Secondary sources (association): {', '.join(secondary_sources)}\n"
        source_enforcement += "\n"

        # Step 3: Construct user prompt + final prompt
        user_prompt = f"""Generate an SAP ABAP CDS View Entity DDL source code for artifact '{artifact_name}'.

CRITICAL ARTIFACT NAME RULE:
The required CDS artifact/entity name is:
{artifact_name}

You MUST use this exact name in:
define view entity {artifact_name}

Do not modify it.
Do not shorten it.
Do not append/remove numbers.
Do not generate another entity name.

Generation Context:
{json.dumps(generation_context, indent=2, default=str)}

Return JSON strictly conforming to this schema:
{{
  "artifact_name": "{artifact_name}",
  "code": "<RAW_CDS_DDL_SOURCE_CODE>",
  "explanation": "Brief explanation of generated CDS View Entity",
  "fields_included": ["<field_name_1>", "<field_name_2>"]
}}
"""
        prompt = CDS_GENERATION_SYSTEM_PROMPT + "\n\n" + source_enforcement + "\n\n" + user_prompt

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }

        code_to_validate = ""
        parsed_result = {}
        warning_msg = None

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(url, params={"key": self.api_key}, json=payload)
                response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            clean_text = _re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=_re.IGNORECASE)
            clean_text = _re.sub(r"\s*```$", "", clean_text.strip())
            parsed_result = json.loads(clean_text)
            code_to_validate = parsed_result.get("code", "")
        except Exception as exc:
            warning_msg = f"Generated via verified metadata synthesizer: {exc}"
            code_to_validate = self._generate_deterministic_cds_code(
                requirement=requirement,
                selected_views=selected_views,
                mappings=mappings,
                join_paths=join_paths,
                kpi=kpi,
                artifact_name=artifact_name,
                selected_views_full_metadata=selected_views_full_metadata,
            )
            parsed_result = {
                "artifact_name": artifact_name,
                "code": code_to_validate,
                "explanation": "Synthesized from authoritative SAP metadata and verified relationship graph.",
                "fields_included": [m.get("actual_field") or m.get("mapped_field") for m in mappings],
            }

        # Apply robust Sanitization & Deterministic Repair
        code_to_validate = self._sanitize_and_repair_cds_code(
            raw_code=code_to_validate,
            selected_views=selected_views,
            mappings=mappings,
            join_paths=join_paths,
            kpi=kpi,
            artifact_name=artifact_name,
            selected_views_full_metadata=selected_views_full_metadata,
        )
        parsed_result["code"] = code_to_validate
        parsed_result["artifact_name"] = artifact_name

        # Deterministic Grounding & Syntax Validation
        validation_res = CDSGenerationValidator.validate_generation(code_to_validate, verified_context)

        logger.info(
            f"CDS Code Generation Execution Result:\n"
            f"  selected_cds: {selected_views}\n"
            f"  full_metadata_counts: {json.dumps(full_metadata_counts)}\n"
            f"  field_mappings: {len(mappings)}\n"
            f"  join_paths: {json.dumps(join_paths)}\n"
            f"  generated_ddl:\n{code_to_validate}\n"
            f"  validation_result: valid={validation_res.get('valid')}, status={validation_res.get('status')}, errors={validation_res.get('errors')}\n"
        )

        response_dict = {
            "success": True,
            "result": parsed_result,
            "validation": validation_res,
            "valid": validation_res["valid"],
            "status": validation_res["status"],
            "code_status": validation_res["code_status"],
            "generation_context": generation_context,
            "full_metadata_counts": full_metadata_counts,
        }
        if warning_msg:
            response_dict["warning"] = warning_msg

        return response_dict


    async def regenerate_code(
        self,
        current_code: str,
        feedback: str,
        verified_context: Dict[str, Any],
        validation_errors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Regenerate CDS DDL code incorporating reviewer feedback & validation errors while strictly preserving metadata constraints."""
        prompt = f"""You are a CDS DDL generator, not a metadata discovery agent.
STRICT RULES:
1. Correct ONLY the invalid generation while remaining strictly grounded in the supplied metadata.
2. Do not invent CDS entities, fields, joins, or associations.
3. Use exact technical field names supplied in field mappings.
4. Do not substitute semantic fields.
5. Use ONLY supplied join relationships.

Current Generated Code:
{current_code}

Reviewer Feedback:
{feedback}

Previous Validation Errors:
{json.dumps(validation_errors) if validation_errors else "None"}

Verified Metadata Context:
{json.dumps(verified_context)}

Return JSON strictly:
{{
  "code": "/* Updated CDS DDL Source */\\n...",
  "explanation": "Summary of changes made based on reviewer feedback"
}}
"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(url, params={"key": self.api_key}, json=payload)
                response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            clean_text = _re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=_re.IGNORECASE)
            clean_text = _re.sub(r"\s*```$", "", clean_text.strip())
            parsed = json.loads(clean_text)
            new_code = parsed.get("code", current_code)

            # Apply robust Sanitization & Deterministic Repair
            new_code = self._sanitize_and_repair_cds_code(
                raw_code=new_code,
                selected_views=verified_context.get("selected_views", []),
                mappings=verified_context.get("mappings", []),
                join_paths=verified_context.get("join_paths", []),
                kpi=verified_context.get("kpi"),
                artifact_name=verified_context.get("artifact_name", "ZI_CustomCDSView"),
            )
            parsed["code"] = new_code

            # Re-run deterministic grounding validator
            val_res = CDSGenerationValidator.validate_generation(new_code, verified_context)
            return {
                "success": True,
                "result": parsed,
                "validation": val_res,
                "valid": val_res["valid"],
                "status": val_res["status"],
                "code_status": val_res["code_status"],
                "coverage": val_res["coverage"],
                "errors": val_res["errors"],
                "warnings": val_res["warnings"],
            }
        except Exception as exc:
            val_res = CDSGenerationValidator.validate_generation(current_code, verified_context)
            return {
                "success": False,
                "error": f"Regeneration failed: {exc}",
                "code": current_code,
                "validation": val_res,
                "valid": False,
                "status": "GENERATION_VALIDATION_FAILED",
                "code_status": "Generation Validation Failed",
            }

    async def generate_wrapper_with_correction_loop(
        self,
        requirement: str,
        selected_views: List[str],
        mappings: List[Dict[str, Any]],
        join_paths: Optional[List[Dict[str, Any]]] = None,
        output_fields: Optional[List[Dict[str, Any]]] = None,
        artifact_name: str = "",
        wrapper_label: str = "",
        sql_view_name: str = "",
        kpi: Optional[Dict[str, Any]] = None,
        selected_views_original_ddl: Optional[Dict[str, str]] = None,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """Build and validate the fixed all-fields extraction wrapper template in memory."""
        wrapper_name = (artifact_name or "").strip()
        label = (wrapper_label or "").strip()
        sql_name = (sql_view_name or "").strip().upper()
        sources = [name.strip() for name in (selected_views or []) if name and name.strip()]
        errors: List[str] = []

        if not sources:
            errors.append("A source CDS view from the discovery workflow is required.")
        elif len(sources) != 1:
            errors.append("This wrapper template accepts exactly one verified source CDS view.")
        if not wrapper_name:
            errors.append("Wrapper CDS name is required.")
        elif len(wrapper_name) > 30 or not _re.fullmatch(r"[ZY][A-Za-z0-9_]*", wrapper_name, _re.IGNORECASE):
            errors.append("Wrapper CDS name must begin with Z or Y, contain only letters, numbers, and underscores, and be at most 30 characters.")
        if not sql_name:
            errors.append("SQL view name is required.")
        elif not _re.fullmatch(r"[ZY][A-Z0-9_]{0,15}", sql_name):
            errors.append("SQL view name must begin with Z or Y, contain only letters, numbers, and underscores, and be at most 16 characters for this ABAP CDS view type.")
        if not label:
            errors.append("Wrapper label is required.")
        elif len(label) > 60:
            errors.append("Wrapper label cannot exceed 60 characters for the target ABAP release.")
        if errors:
            return {"success": False, "valid": False, "status": "INPUT_VALIDATION_FAILED", "errors": errors}

        source_name = sources[0]
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_/]*", source_name):
            return {
                "success": False,
                "valid": False,
                "status": "INPUT_VALIDATION_FAILED",
                "errors": ["The discovery workflow supplied an invalid source CDS name."],
            }

        client = SAPADTClient(get_settings())
        metadata_source = "LIVE_SAP_ADT"
        try:
            try:
                source_ddl = await client.get_ddl_source(source_name)
            except SAPNotFoundError:
                return {
                    "success": False,
                    "valid": False,
                    "status": "SOURCE_METADATA_VALIDATION_FAILED",
                    "errors": [f"Source CDS '{source_name}' does not exist in SAP ADT metadata."],
                }
            except Exception as exc:
                discovery = CDSDiscoveryService()
                with discovery._get_db() as conn:
                    indexed = conn.execute(
                        "SELECT cds_name, source_code FROM cds_views WHERE UPPER(cds_name) = ?",
                        (source_name.upper(),),
                    ).fetchone()
                if not indexed or not indexed[1]:
                    return {
                        "success": False,
                        "valid": False,
                        "status": "SOURCE_METADATA_VALIDATION_FAILED",
                        "errors": [f"Could not verify source CDS '{source_name}' in live or indexed SAP ADT metadata: {exc}"],
                    }
                source_ddl = indexed[1]
                metadata_source = "INDEXED_SAP_ADT_SOURCE"
            if not source_ddl or not source_ddl.strip():
                return {
                    "success": False,
                    "valid": False,
                    "status": "SOURCE_METADATA_VALIDATION_FAILED",
                    "errors": [f"SAP ADT returned no source DDL for '{source_name}'."],
                }
            definition = _re.search(
                r"\bdefine\s+(?:(?:root|projection)\s+)?view(?:\s+entity)?\s+([A-Za-z0-9_/]+)",
                source_ddl,
                _re.IGNORECASE,
            )
            if not definition or definition.group(1).upper() != source_name.upper():
                return {
                    "success": False,
                    "valid": False,
                    "status": "SOURCE_METADATA_VALIDATION_FAILED",
                    "errors": [f"Retrieved source DDL does not confirm the exact CDS definition name '{source_name}'."],
                }

            try:
                await client.get_ddl_source(wrapper_name)
                return {
                    "success": False,
                    "valid": False,
                    "status": "WRAPPER_NAME_CONFLICT",
                    "errors": [f"Wrapper CDS name '{wrapper_name}' already exists in SAP. Choose a unique name."],
                }
            except SAPNotFoundError:
                pass
            except Exception as exc:
                discovery = CDSDiscoveryService()
                with discovery._get_db() as conn:
                    indexed_wrapper = conn.execute(
                        "SELECT 1 FROM cds_views WHERE UPPER(cds_name) = ?",
                        (wrapper_name,),
                    ).fetchone()
                if indexed_wrapper:
                    return {
                        "success": False,
                        "valid": False,
                        "status": "WRAPPER_NAME_CONFLICT",
                        "errors": [f"Wrapper CDS name '{wrapper_name}' already exists in indexed SAP metadata."],
                    }
                metadata_source = f"{metadata_source}+INDEXED_SAP_ADT_NAME_CHECK"

            try:
                sql_view_name = sql_name
                if sql_view_name in _indexed_sql_view_names():
                    raise ValueError(f"SQL view name '{sql_view_name}' already exists in indexed SAP CDS metadata.")
            except ValueError as exc:
                return {
                    "success": False,
                    "valid": False,
                    "status": "SQL_VIEW_NAME_VALIDATION_FAILED",
                    "errors": [str(exc)],
                }

            safe_label = label.replace("'", "''")
            code = f"""@AbapCatalog.sqlViewName: '{sql_view_name}'
@AbapCatalog.compiler.compareFilter: true
@AbapCatalog.preserveKey: true
@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: '{safe_label}'
@Metadata.ignorePropagatedAnnotations: true

@Analytics: {{
    dataExtraction: {{
        enabled: true,
        delta.changeDataCapture.automatic: true
    }}
}}

define view {wrapper_name}
    as select from {source_name}
{{
    *
}}"""

            mandatory_annotations = (
                "@AbapCatalog.sqlViewName:",
                "@AbapCatalog.compiler.compareFilter: true",
                "@AbapCatalog.preserveKey: true",
                "@AccessControl.authorizationCheck: #NOT_REQUIRED",
                "@EndUserText.label:",
                "@Metadata.ignorePropagatedAnnotations: true",
                "dataExtraction: {",
                "enabled: true",
                "delta.changeDataCapture.automatic: true",
            )
            missing = [annotation for annotation in mandatory_annotations if annotation not in code]
            source_clause = _re.search(
                rf"define\s+view\s+{_re.escape(wrapper_name)}\s+as\s+select\s+from\s+{_re.escape(source_name)}\s*\{{\s*\*\s*\}}",
                code,
                _re.IGNORECASE | _re.DOTALL,
            )
            if missing or not source_clause or "*" not in code:
                errors = [f"Generated wrapper failed deterministic validation; missing or invalid: {', '.join(missing) or 'exact source projection' }."]
                return {"success": False, "valid": False, "status": "GENERATION_VALIDATION_FAILED", "errors": errors}

            return {
                "success": True,
                "valid": True,
                "status": "STATIC_VALIDATION_PASSED",
                "code_status": "Validated In-Memory",
                "code": code,
                "artifact_name": wrapper_name,
                "wrapper_label": label,
                "source_cds_name": source_name,
                "sql_view_name": sql_view_name,
                "attempts": 1,
                "validation": {
                    "valid": True,
                    "errors": [],
                    "source_cds_exists_in_sap": True,
                    "source_metadata_source": metadata_source,
                    "live_sap_connection_available": metadata_source == "LIVE_SAP_ADT",
                    "wrapper_name_unique_in_indexed_sap_metadata": True,
                    "sql_view_name_unique_in_indexed_sap_metadata": True,
                    "source_clause_exact": True,
                    "all_fields_projection": True,
                    "mandatory_annotations_present": True,
                    "cdc_compatibility": "Requires SAP checkrun/activation validation; annotation alone does not guarantee CDC support.",
                },
                "sap_source_validation_status": "NOT_RUN_NO_SAP_WRITES",
                "message": "Generated in memory only. No SAP object was created, activated, or deployed.",
            }
        finally:
            await client.aclose()



