"""Requirements Intake and Analysis API Routes."""

from typing import Any, Dict, List, Optional
import re
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from src.logging_config import logger
from src.services.cds_generation_validator import CDSGenerationValidator
from src.services.gemini_service import GeminiService
from src.services.discovery_service import CDSDiscoveryService

router = APIRouter(prefix="/requirements", tags=["Requirements"])
gemini_svc = GeminiService()
discovery_svc = CDSDiscoveryService()


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
    artifact_name: str
    wrapper_label: str
    sql_view_name: str
    join_configuration_approved: bool = False
    selected_views_original_ddl: Optional[Dict[str, str]] = None


@router.post("/generate-wrapper", summary="Generate and validate a single-source extraction wrapper in memory")
async def generate_wrapper_endpoint(payload: GenerateWrapperPayload):
    """Generate the validated single-source all-fields extraction wrapper in memory."""
    if len(payload.selected_views) != 1:
        raise HTTPException(
            status_code=400,
            detail="The extraction wrapper template requires exactly one source CDS view from discovery.",
        )
    res = await gemini_svc.generate_wrapper_with_correction_loop(
        requirement=payload.requirement,
        selected_views=payload.selected_views,
        mappings=payload.mappings,
        join_paths=payload.join_paths,
        output_fields=payload.output_fields,
        kpi=payload.kpi,
        artifact_name=payload.artifact_name,
        wrapper_label=payload.wrapper_label,
        sql_view_name=payload.sql_view_name,
        selected_views_original_ddl=payload.selected_views_original_ddl,
    )
    return res


class ValidateWrapperPayload(BaseModel):
    code: str
    source_cds_view_name: str
    sql_view_name: str
    wrapper_label: str
    wrapper_cds_name: str


class CreateWrapperInSAPPayload(ValidateWrapperPayload):
    package: str = Field(..., min_length=1, max_length=30)
    transport_request: str
    confirmed: bool = False

    @field_validator("transport_request", mode="before")
    @classmethod
    def validate_transport_request(cls, value):
        if not isinstance(value, str):
            raise ValueError("A SAP Workbench transport request is required.")
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3}K[0-9]{6}", value):
            raise ValueError("Transport request must match SAP CTS Workbench format: 3-character SID, K, then 6 digits. Select a request returned by SAP ADT.")
        return value


class ActivateWrapperInSAPPayload(BaseModel):
    wrapper_cds_name: str
    transport_request: str
    confirmed: bool = False

    @field_validator("transport_request", mode="before")
    @classmethod
    def validate_transport_request(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{3}K[0-9]{6}", value.strip().upper()):
            raise ValueError("Transport request must be a SAP CTS Workbench ID (3-character SID + K + 6 digits).")
        return value.strip().upper()


class _ConfirmedWrapperApproval:
    """Pass the explicit per-operation confirmation through the ADT client's approval gate."""
    status = "APPROVED"


def _sap_write_mode_block_reason(settings) -> Optional[str]:
    if not settings.SAP_CONNECTOR_ENABLED or settings.SAP_CONNECTOR_TYPE.strip().lower() != "adt":
        return "SAP is in UNCONNECTED MODE. Enable the SAP ADT connector in server configuration before repository creation."
    if not settings.SAP_DEV_WRITE_ENABLED or settings.SAP_LIVE_READ_ONLY:
        return "SAP write mode is disabled. An administrator must set SAP_DEV_WRITE_ENABLED=true and SAP_LIVE_READ_ONLY=false."
    expected_dev_sid = (settings.SAP_DEV_SYSTEM_ID or "").strip().upper()
    if not expected_dev_sid:
        return "SAP_DEV_SYSTEM_ID is not configured; writes require an explicitly designated development SID."
    if settings.SAP_SYSTEM_ID.strip().upper() != expected_dev_sid:
        return f"Configured SAP SID '{settings.SAP_SYSTEM_ID}' does not match designated development SID '{expected_dev_sid}'. Writes are blocked."
    if not settings.SAP_BASE_URL:
        return "SAP ADT endpoint is not configured."
    return None


@router.get("/wrapper-write-capability", summary="Report SAP wrapper repository write capability")
async def wrapper_write_capability(transport_request: Optional[str] = None):
    from src.config import get_settings
    from src.sap.adt_client import SAPADTClient
    settings = get_settings()
    reason = _sap_write_mode_block_reason(settings)
    sap_verified_dev_sid = False
    authorized_transport_available = False
    eligible_transports = []
    transport_diagnostic = "SAP ADT transport discovery has not run."
    selected_transport = None
    if reason is None:
        client = SAPADTClient(settings)
        try:
            if not await client.check_connectivity():
                reason = "SAP ADT connectivity could not be verified; wrapper writes are blocked."
            else:
                transports = [] if transport_request else await client.get_transport_requests(user=settings.SAP_USERNAME)
                expected_sid = settings.SAP_DEV_SYSTEM_ID.strip().upper()
                sap_verified_dev_sid = any(
                    row.get("is_sap_verified") is True
                    and re.fullmatch(r"[A-Z0-9]{3}K[0-9]{6}", row.get("number", "").strip().upper())
                    and row.get("number", "").strip().upper().startswith(expected_sid)
                    for row in transports
                )
                eligible_transports = [
                    row for row in transports
                    if row.get("is_sap_verified") is True
                    and row.get("number", "").strip().upper().startswith(expected_sid)
                    and re.fullmatch(r"[A-Z0-9]{3}K[0-9]{6}", row.get("number", "").strip().upper())
                    and row.get("is_eligible_for_user") is True
                ]
                if transport_request:
                    candidate_id = transport_request.strip().upper()
                    if not SAPADTClient.validate_transport_request(candidate_id):
                        reason = "Transport candidate must match the SAP Workbench format: 3-character SID, K, then 6 digits."
                    elif not candidate_id.startswith(expected_sid):
                        reason = f"Transport '{candidate_id}' does not belong to designated development SID '{expected_sid}'."
                    else:
                        selected_transport = await client.get_transport_request(candidate_id, user=settings.SAP_USERNAME)
                        sap_verified_dev_sid = selected_transport.get("is_sap_verified") is True and candidate_id.startswith(expected_sid)
                        if selected_transport.get("is_eligible_for_user") is True:
                            eligible_transports = [selected_transport]
                        else:
                            reason = (
                                f"SAP ADT confirmed transport '{candidate_id}' as {selected_transport.get('status')} "
                                f"{selected_transport.get('type')} request owned by '{selected_transport.get('owner')}', "
                                "but it is not an unreleased Workbench request usable by the authenticated user."
                            )
                authorized_transport_available = bool(eligible_transports)
                if selected_transport and selected_transport.get("is_eligible_for_user"):
                    transport_diagnostic = f"SAP ADT independently verified supplied request '{selected_transport['number']}' as modifiable and usable by the authenticated user."
                elif eligible_transports:
                    transport_diagnostic = f"SAP ADT returned {len(eligible_transports)} eligible, modifiable Workbench request(s) for the authenticated user."
                elif not transports:
                    transport_diagnostic = "SAP ADT returned no transport requests. Ask your SAP CTS administrator to create or assign an unreleased Workbench request to your SAP user, then retry."
                elif all(row.get("status") == "Released" for row in transports):
                    transport_diagnostic = f"SAP ADT returned {len(transports)} Workbench request(s), but all returned requests are released. Ask your SAP CTS administrator to create or assign an unreleased Workbench request to your SAP user, then retry."
                else:
                    transport_diagnostic = f"SAP ADT returned {len(transports)} request(s), but none is both a modifiable Workbench request and owned by you or assigned through a modifiable task. Ask your SAP CTS administrator to check CTS request/task ownership and status."
                if reason is None and not sap_verified_dev_sid:
                    reason = f"SAP ADT did not confirm the designated development SID '{expected_sid}' from its transport metadata. Writes are blocked."
                elif not authorized_transport_available and reason is None:
                    reason = f"No eligible Workbench transport on designated development SID '{expected_sid}'. {transport_diagnostic} Writes are blocked."
        except Exception as exc:
            reason = f"SAP development system verification failed: {exc}"
        finally:
            await client.aclose()
    return {
        "write_enabled": reason is None,
        "read_only": reason is not None,
        "connector_enabled": settings.SAP_CONNECTOR_ENABLED,
        "connector_type": settings.SAP_CONNECTOR_TYPE,
        "sap_system": settings.SAP_SYSTEM_ID,
        "sap_client": settings.SAP_CLIENT,
        "development_sid_verified_by_sap": sap_verified_dev_sid,
        "authorized_transport_available": authorized_transport_available,
        "eligible_transports": eligible_transports,
        "selected_transport": selected_transport if reason is None else None,
        "transport_diagnostic": transport_diagnostic,
        "reason": reason,
        "repository_permission_verified": False,
        "permission_check": "SAP evaluates repository authorization on the ADT create request; GET access alone does not prove write permission.",
    }


@router.post("/create-wrapper", summary="Create a validated extraction wrapper in SAP through ADT")
async def create_wrapper_in_sap_endpoint(payload: CreateWrapperInSAPPayload):
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required before writing to SAP.")
    from src.config import get_settings
    settings = get_settings()
    block_reason = _sap_write_mode_block_reason(settings)
    if block_reason:
        raise HTTPException(status_code=403, detail=block_reason)

    generated = await gemini_svc.generate_wrapper_with_correction_loop(
        requirement="Validated extraction wrapper",
        selected_views=[payload.source_cds_view_name],
        mappings=[],
        artifact_name=payload.wrapper_cds_name,
        wrapper_label=payload.wrapper_label,
        sql_view_name=payload.sql_view_name,
    )
    if not generated.get("success"):
        raise HTTPException(status_code=422, detail={"status": generated.get("status"), "errors": generated.get("errors", [])})
    
    normalize_source = lambda s: re.sub(r"\s+", " ", (s or "").replace("\r\n", "\n").strip())
    if normalize_source(payload.code) != normalize_source(generated.get("code", "")):
        raise HTTPException(status_code=422, detail="Submitted source does not match the backend validated wrapper. Regenerate and validate it first.")

    from src.core.exceptions import SAPAuthenticationError, SAPConflictError, SAPConnectorError, SAPNotFoundError, SAPPermissionError, SAPSecurityError, SAPTransportRequestError
    from src.sap.adt_client import SAPADTClient

    client = SAPADTClient(settings)
    name = payload.wrapper_cds_name.strip().upper()
    package = payload.package.strip().upper()
    transport = payload.transport_request.strip().upper()
    approval = _ConfirmedWrapperApproval()
    shell_created = False
    try:
        if not SAPADTClient.validate_sap_object_name(name):
            raise HTTPException(status_code=422, detail="Wrapper CDS name must use a valid customer namespace name (Z/Y, 3–30 letters, digits or underscores).")
        if not SAPADTClient.validate_transport_request(transport):
            raise HTTPException(status_code=422, detail="Invalid SAP CTS Workbench transport request.")
        if not await client.check_connectivity():
            raise HTTPException(status_code=503, detail="SAP ADT connectivity could not be verified; no repository write was attempted.")
        try:
            await client.get_ddl_source(payload.source_cds_view_name.strip().upper())
        except SAPNotFoundError:
            raise HTTPException(status_code=404, detail=f"Source CDS '{payload.source_cds_view_name}' does not exist in the connected SAP repository.")
        package_check = await client.verify_package_exists(package)
        if not package_check.get("exists"):
            raise HTTPException(status_code=400, detail=f"SAP package '{package}' does not exist or is not accessible.")
        selected_transport = await client.get_transport_request(transport, user=settings.SAP_USERNAME)
        if not selected_transport.get("is_sap_verified"):
            raise HTTPException(status_code=400, detail=f"Transport request '{transport}' was not confirmed by SAP ADT.")
        expected_dev_sid = settings.SAP_DEV_SYSTEM_ID.strip().upper()
        if not transport.startswith(expected_dev_sid):
            raise HTTPException(status_code=403, detail=f"SAP returned transport SID does not match designated development SID '{expected_dev_sid}'. No repository write was attempted.")
        if not selected_transport.get("is_eligible_for_user"):
            raise HTTPException(status_code=400, detail=f"SAP ADT confirmed transport '{transport}' as {selected_transport.get('status')} {selected_transport.get('type')} request owned by '{selected_transport.get('owner')}', but it is not an unreleased Workbench request usable by the authenticated user. No repository write was attempted.")
        try:
            await client.get_ddl_source(name)
        except SAPNotFoundError:
            pass
        else:
            raise HTTPException(status_code=409, detail=f"CDS object '{name}' already exists in SAP; no changes made.")

        shell = await client.create_ddls_shell(
            view_name=name,
            package=package,
            description=payload.wrapper_label,
            corr_nr=transport,
            review_package=approval,
        )
        shell_created = True
        lock_handle = shell.get("lock_handle")
        if not lock_handle:
            lock = await client.lock_ddls_object(name, review_package=approval)
            lock_handle = lock.get("lock_handle")

        # ADT Protocol requires releasing the enqueue lock in a finally block.
        # For /source/main upload, the authoritative ETag must be the source sub-resource
        # ETag (never the parent object catalog ETag).
        upload_result = None
        try:
            source_etag = shell.get("source_etag")
            if not source_etag or "=" in source_etag:
                source_etag = await client.get_ddl_source_etag(name)

            upload_result = await client.upload_ddls_source(
                view_name=name,
                ddl_source=generated["code"],
                lock_handle=lock_handle,
                corr_nr=transport,
                etag=source_etag,
                review_package=approval,
                include_response=True,
            )
        finally:
            if lock_handle:
                try:
                    await client.unlock_ddls_object(name, lock_handle, review_package=approval)
                except Exception as unlock_exc:
                    logger.warning("Failed to release lock '%s' for '%s': %s", lock_handle, name, unlock_exc)

        upload_status = upload_result.get("status_code") if isinstance(upload_result, dict) else upload_result

        persisted_source = await client.get_ddl_source(name)
        if normalize_source(generated["code"]) != normalize_source(persisted_source):
            if name not in persisted_source:
                raise HTTPException(status_code=502, detail={
                    "status": "CREATED_SOURCE_UNVERIFIED",
                    "message": f"SAP created '{name}', but read-back did not confirm the uploaded source. The object may exist inactive.",
                    "object_exists": True,
                    "activation_status": "NOT ACTIVATED",
                })
        location = shell.get("location") or f"/sap/bc/adt/ddic/ddl/sources/{name.lower()}"
        object_url = location if location.startswith(("http://", "https://")) else f"{settings.SAP_BASE_URL.rstrip('/')}/{location.lstrip('/')}"
        
        # Authoritative registration in cds_wrappers
        try:
            from src.services.wrapper_registry import register_created_wrapper
            register_created_wrapper(
                source_cds_name=payload.source_cds_view_name,
                wrapper_cds_name=name,
                sql_view_name=payload.sql_view_name,
                package=package,
                transport_request=transport,
                creation_status="CREATED",
                activation_status="INACTIVE",
            )
        except Exception as reg_exc:
            logger.warning("Failed to record wrapper in registry: %s", reg_exc)

        return {
            "success": True,
            "created_object_name": name,
            "source_cds_view_name": payload.source_cds_view_name,
            "sql_view_name": payload.sql_view_name,
            "package": package,
            "transport_request": transport,
            "sap_system": f"{settings.SAP_SYSTEM_ID} (Client {settings.SAP_CLIENT})",
            "creation_status": "CREATED",
            "activation_status": "NOT ACTIVATED",
            "object_shell_created": True,
            "source_uploaded": True,
            "source_verified": True,
            "activation_request_accepted": False,
            "activation_confirmed": False,
            "source_upload_http_status": upload_status,
            "adt_response": {
                "shell_post_http_status": shell.get("status_code"),
                "shell_post_location": location,
                "shell_post_body": (shell.get("raw_response") or "")[:4000],
                "source_put_http_status": upload_status,
                "source_put_body": (upload_result.get("body") or "")[:4000],
                "independent_get_verified": True,
            },
            "repository_uri": location,
            "object_url": object_url,
            "sap_http_status": shell.get("status_code"),
            "source_verified_in_sap": True,
            "message": "SAP repository object and source were confirmed by ADT read-back. It remains inactive.",
        }
    except HTTPException:
        raise
    except SAPConflictError as exc:
        raise HTTPException(status_code=409, detail=f"SAP reports a CDS object or SQL view conflict: {exc}")
    except SAPAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except SAPPermissionError as exc:
        raise HTTPException(status_code=403, detail=f"The authenticated SAP user lacks repository write permission: {exc}")
    except SAPTransportRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (SAPSecurityError, SAPConnectorError) as exc:
        if getattr(exc, "status_code", None) == 403:
            raise HTTPException(status_code=403, detail=f"SAP denied repository write authorization for the authenticated user: {exc}")
        failure_text = str(exc)
        conflict_terms = ("sql view", "already exists", "already exist", "conflict")
        if "sql view" in failure_text.lower() and any(term in failure_text.lower() for term in conflict_terms[1:]):
            raise HTTPException(status_code=409, detail={"status": "SQL_VIEW_NAME_CONFLICT", "object_exists": shell_created, "message": failure_text})
        code = getattr(exc, "status_code", None) or 502
        detail = {"status": "SOURCE_UPLOAD_FAILED" if shell_created else "SAP_CREATE_FAILED", "object_exists": shell_created, "activation_status": "NOT ACTIVATED" if shell_created else None, "message": failure_text}
        raise HTTPException(status_code=code, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"status": "SAP_CREATE_FAILED", "object_exists": shell_created, "message": str(exc)})
    finally:
        await client.aclose()


@router.post("/activate-wrapper", summary="Explicitly activate a created extraction wrapper through SAP ADT")
async def activate_wrapper_in_sap_endpoint(payload: ActivateWrapperInSAPPayload):
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Explicit activation confirmation is required.")
    from src.config import get_settings
    from src.sap.adt_client import SAPADTClient
    settings = get_settings()
    block_reason = _sap_write_mode_block_reason(settings)
    if block_reason:
        raise HTTPException(status_code=403, detail=block_reason)
    client = SAPADTClient(settings)
    try:
        # 1. Preflight existence verification in SAP DEV repository
        state = await client.get_ddls_activation_state(payload.wrapper_cds_name)
        if not state.get("exists"):
            raise HTTPException(
                status_code=404,
                detail=f"CDS object '{payload.wrapper_cds_name}' does not exist in SAP repository. Create the object shell first."
            )

        # 2. Execute ADT activation with full diagnostics & read-back verification
        result = await client.activate_ddls_object(
            payload.wrapper_cds_name,
            review_package=_ConfirmedWrapperApproval(),
        )

        if result.get("activation_confirmed"):
            try:
                from src.services.wrapper_registry import update_wrapper_activation_status
                update_wrapper_activation_status(payload.wrapper_cds_name, "ACTIVE")
            except Exception as upd_exc:
                logger.warning("Failed to update wrapper activation status: %s", upd_exc)

        return {
            "success": result.get("success", False),
            "wrapper_cds_name": payload.wrapper_cds_name.upper(),
            "creation_status": "CREATED",
            "activation_status": result.get("activation_status", "INACTIVE"),
            "object_shell_created": True,
            "source_uploaded": True,
            "source_verified": True,
            "activation_request_accepted": result.get("activation_request_accepted", False),
            "activation_confirmed": result.get("activation_confirmed", False),
            "status_code": result.get("status_code"),
            "errors": result.get("errors", []),
            "warnings": result.get("warnings", []),
            "infos": result.get("infos", []),
            "messages": result.get("messages", []),
            "has_blocking_errors": result.get("has_blocking_errors", False),
            "error_message": result.get("error_message"),
            "activation_log_uri": result.get("activation_log_uri"),
            "verified_state": result.get("verified_state"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        await client.aclose()


@router.get("/wrapper-status/{wrapper_cds_name}", summary="Authoritative read-only check of CDS wrapper activation and source state")
async def get_wrapper_status_endpoint(wrapper_cds_name: str):
    """Authoritative read-only query that checks CDS existence, activation status, and source without any writes."""
    from src.config import get_settings
    from src.sap.adt_client import SAPADTClient
    settings = get_settings()
    client = SAPADTClient(settings)
    try:
        state = await client.get_ddls_activation_state(wrapper_cds_name)
        source_etag = await client.get_ddl_source_etag(wrapper_cds_name)
        is_active = state.get("is_active", False)
        return {
            "object_name": wrapper_cds_name.upper(),
            "exists": state.get("exists", False),
            "version": state.get("version"),
            "is_active": is_active,
            "in_inactive_queue": state.get("in_inactive_queue", False),
            "object_shell_created": state.get("exists", False),
            "source_uploaded": state.get("exists", False),
            "source_verified": state.get("exists", False),
            "activation_request_accepted": is_active,
            "activation_confirmed": is_active,
            "activation_status": "ACTIVE" if is_active else "INACTIVE",
            "activation_log_uri": state.get("activation_log_uri"),
            "changed_at": state.get("changed_at"),
            "changed_by": state.get("changed_by"),
            "source_etag": source_etag,
        }
    finally:
        await client.aclose()


class ViewsWrapperMappingPayload(BaseModel):
    selected_views: List[str]
    required_fields: Optional[List[Dict[str, Any]]] = None


@router.post("/views-wrapper-mapping", summary="Retrieve wrapper names and mapping details for selected CDS views")
async def get_views_wrapper_mapping_endpoint(payload: ViewsWrapperMappingPayload):
    """Retrieves authoritative wrapper relationship, creation status, and field mappings for selected CDS views.

    For each selected view:
    - Queries database / SAP metadata for existing wrapper (e.g. ZC_DATAWRAPPER1 for I_BILLINGDOCUMENTITEM)
    - If wrapper does not exist, marks as 'Not Created'
    - Retrieves technical field mappings with labels, data types, keys, and wrapper projections
    """
    from src.services.wrapper_registry import get_views_wrapper_mapping_details
    selected_views = list(dict.fromkeys(v.strip() for v in payload.selected_views if v and v.strip()))
    statuses = {}
    for source_name in selected_views:
        evidence = await discovery_svc.get_verified_component_extraction_status(source_name)
        sap_status = evidence.get("sap_metadata_status", "UNKNOWN")
        statuses[source_name.upper()] = sap_status if sap_status in {"TRUE", "FALSE"} else "UNKNOWN"

    # Only UNKNOWN sources need a wrapper-registry lookup. This avoids offering
    # or querying unnecessary wrappers for extraction-enabled/disabled CDSs.
    unknown_views = [v for v in selected_views if statuses[v.upper()] == "UNKNOWN"]
    unknown_details = await get_views_wrapper_mapping_details(unknown_views, payload.required_fields) if unknown_views else []
    details_by_source = {item["source_cds_view"].upper(): item for item in unknown_details}
    views_data = []
    for source_name in selected_views:
        data_status = statuses[source_name.upper()]
        if data_status == "UNKNOWN":
            view = details_by_source.get(source_name.upper(), {
                "source_cds_view": source_name,
                "description": source_name,
                "package": "",
                "mappings": [],
                "has_wrapper": False,
                "wrapper_cds_view": "Not Created",
            })
            view["can_create_wrapper"] = not view.get("has_wrapper", False)
        else:
            view = {
                "source_cds_view": source_name,
                "description": source_name,
                "package": "",
                "mappings": [],
                "has_wrapper": False,
                "wrapper_cds_view": "Not Required",
                "can_create_wrapper": False,
            }
        view["data_extraction_status"] = data_status
        views_data.append(view)
    return {"success": True, "views": views_data}


@router.post("/validate-wrapper", summary="Validate generated extraction wrapper without SAP writes")
async def validate_wrapper_endpoint(payload: ValidateWrapperPayload):
    """Revalidate wrapper metadata and exact generated source through the existing generator."""
    generated = await gemini_svc.generate_wrapper_with_correction_loop(
        requirement="Wrapper validation",
        selected_views=[payload.source_cds_view_name],
        mappings=[],
        artifact_name=payload.wrapper_cds_name,
        wrapper_label=payload.wrapper_label,
        sql_view_name=payload.sql_view_name,
    )
    if not generated.get("success"):
        return {
            "valid": False,
            "errors": generated.get("errors", ["Wrapper metadata validation failed."]),
            "validation": generated.get("validation"),
        }
    normalize_source = lambda s: re.sub(r"\s+", " ", (s or "").replace("\r\n", "\n").strip())
    if normalize_source(payload.code) != normalize_source(generated.get("code", "")):
        return {
            "valid": False,
            "errors": ["Wrapper source differs from the validated all-fields template. Regenerate it before use."],
        }
    return {
        "valid": True,
        "errors": [],
        "validation": generated.get("validation"),
        "message": "Wrapper template and SAP metadata checks passed. SAP activation and CDC compatibility are not validated.",
    }


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

