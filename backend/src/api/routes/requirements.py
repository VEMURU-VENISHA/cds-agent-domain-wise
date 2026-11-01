"""Requirements Intake and Analysis API Routes."""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from src.services.cds_generation_validator import CDSGenerationValidator
from src.services.gemini_service import GeminiService

router = APIRouter(prefix="/requirements", tags=["Requirements"])
gemini_svc = GeminiService()


class ExtractFieldsPayload(BaseModel):
    requirement: str
    business_domains: Optional[List[str]] = None


class GenerateCodePayload(BaseModel):
    requirement: str
    selected_views: List[str]
    mappings: List[dict]
    join_paths: List[dict] = []
    kpi: Optional[dict] = None
    artifact_name: str = "ZC_SalesOrderAnalysis"


class RegenerateCodePayload(BaseModel):
    current_code: str
    feedback: str
    verified_context: dict = {}


class ValidateArtifactNamePayload(BaseModel):
    name: str
    package: str = "ZTRAINING_2026"


class GenerateFieldVariantsPayload(BaseModel):
    field_name: str
    requirement: Optional[str] = ""
    domain: Optional[str] = "Sales & Distribution"
    business_domains: Optional[List[str]] = None


@router.post("/extract-fields", summary="Extract required fields and search variants using Gemini 3.5/3.6 Flash")
async def extract_fields_endpoint(payload: ExtractFieldsPayload):
    req_text = payload.requirement.strip()
    if not req_text:
        raise HTTPException(status_code=400, detail="Requirement cannot be empty")
    res = await gemini_svc.extract_fields_and_variants(
        business_requirement=req_text,
        business_domains=payload.business_domains,
    )
    return res


@router.post("/generate-field-variants", summary="Generate semantic search variants and classification for a single field")
async def generate_field_variants_endpoint(payload: GenerateFieldVariantsPayload):
    field_name = payload.field_name.strip()
    if not field_name:
        raise HTTPException(status_code=400, detail="Field name cannot be empty")
    domains = payload.business_domains or ([payload.domain] if payload.domain else ["Sales & Distribution"])
    res = await gemini_svc.generate_single_field_variants(
        field_name=field_name,
        requirement_context=payload.requirement or "",
        business_domains=domains,
    )
    return res



@router.post("/generate-code", summary="Generate verified CDS code using Gemini and verified SAP metadata")
async def generate_code_endpoint(payload: GenerateCodePayload):
    res = await gemini_svc.generate_cds_code(
        requirement=payload.requirement,
        selected_views=payload.selected_views,
        mappings=payload.mappings,
        join_paths=payload.join_paths,
        kpi=payload.kpi,
        artifact_name=payload.artifact_name,
    )
    return res


class ValidateUserCodePayload(BaseModel):
    code: str = Field(..., description="User-provided CDS View Entity DDL source code")
    verified_context: Dict[str, Any] = Field(..., description="Context for deterministic validator")


@router.post("/validate-user-code", summary="Validate user-provided CDS View Entity code against deterministic metadata validator")
async def validate_user_code_endpoint(payload: ValidateUserCodePayload):
    code = payload.code
    v_ctx = payload.verified_context
    res = CDSGenerationValidator.validate_generation(code, v_ctx)
    status_label = "USER_CODE_VALIDATED" if res["valid"] else "USER_CODE_VALIDATION_FAILED"
    res["status"] = status_label
    res["code_source"] = "USER_PROVIDED"
    res["approval_enabled"] = True
    return res



@router.post("/regenerate-code", summary="Regenerate CDS code with user review feedback")
async def regenerate_code_endpoint(payload: RegenerateCodePayload):
    res = await gemini_svc.regenerate_code(
        current_code=payload.current_code,
        feedback=payload.feedback,
        verified_context=payload.verified_context,
    )
    return res


@router.post("/validate-artifact-name", summary="Validate CDS artifact name rules and conflict check")
async def validate_artifact_name_endpoint(payload: ValidateArtifactNamePayload):
    name = payload.name.strip().upper()
    errors = []
    if not name:
        errors.append("Artifact name cannot be empty.")
    if len(name) > 30:
        errors.append("Artifact name exceeds SAP 30-character limit.")
    if not (name.startswith("Z") or name.startswith("Y")):
        errors.append("Custom CDS name must begin with 'Z' or 'Y' namespace prefix.")
    import re
    if not re.match(r"^[A-Z0-9_]+$", name):
        errors.append("Artifact name can only contain letters, numbers, and underscores.")

    # Check local SQLite DB for collisions
    exists = False
    try:
        from src.services.discovery_service import CDSDiscoveryService
        ds = CDSDiscoveryService()
        with ds._get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM cds_views WHERE UPPER(cds_name) = ?", (name,))
            if c.fetchone():
                exists = True
                errors.append(f"View '{name}' already exists in SAP repository. Overwrite prevented.")
    except Exception:
        pass

    return {
        "name": name,
        "valid": len(errors) == 0,
        "errors": errors,
        "package": payload.package,
        "exists": exists,
    }


class GenerateWrapperPayload(BaseModel):
    requirement: str
    selected_views: List[str]
    mappings: List[dict]
    join_paths: List[dict] = []
    output_fields: Optional[List[dict]] = None
    kpi: Optional[dict] = None
    artifact_name: str = "ZC_ExtractionWrapper"
    join_configuration_approved: bool = False
    selected_views_original_ddl: Optional[Dict[str, str]] = None


@router.post("/generate-wrapper", summary="Generate custom extraction-enabled wrapper with human-in-the-loop join gate and validation loop")
async def generate_wrapper_endpoint(payload: GenerateWrapperPayload):
    """Enforces join approval gate before LLM code generation and executes in-memory correction loop."""
    if len(payload.selected_views) > 1 and not payload.join_configuration_approved:
        raise HTTPException(
            status_code=400,
            detail="Source CDS and join configuration must be explicitly approved ('APPROVE SOURCE CDS AND JOIN CONFIGURATION') before generating wrapper code.",
        )
    res = await gemini_svc.generate_wrapper_with_correction_loop(
        requirement=payload.requirement,
        selected_views=payload.selected_views,
        mappings=payload.mappings,
        join_paths=payload.join_paths,
        output_fields=payload.output_fields,
        kpi=payload.kpi,
        artifact_name=payload.artifact_name,
        selected_views_original_ddl=payload.selected_views_original_ddl,
    )
    return res


class ValidateMemoryPayload(BaseModel):
    code: str
    verified_context: Dict[str, Any]


@router.post("/validate-memory", summary="Deterministic in-memory validation and SAP source-only capability investigation")
async def validate_memory_endpoint(payload: ValidateMemoryPayload):
    """Executes deterministic static validation with line numbers without modifying SAP repository."""
    return CDSGenerationValidator.investigate_and_validate_in_memory(
        code=payload.code,
        verified_context=payload.verified_context,
    )

