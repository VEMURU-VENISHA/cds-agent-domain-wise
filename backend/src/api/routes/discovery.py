"""SAP Discovery API Routes."""

from typing import List, Optional, Union, Dict, Any
from fastapi import APIRouter, Query
from src.models.domain import CDSCandidate, CDSLayer
from src.logging_config import logger
from src.sap.connector import get_sap_connector
from src.sap.models import SAPSystemInfo, CDSMetadata

router = APIRouter(prefix="/discovery", tags=["SAP Discovery"])


@router.get(
    "/system-profile",
    response_model=SAPSystemInfo,
    summary="Returns SAP target system metadata profile",
)
async def get_sap_profile():
    """Returns the baseline SAP S/4HANA target system profile."""
    connector = get_sap_connector()
    return await connector.get_system_info()


@router.get(
    "/search",
    response_model=List[CDSCandidate],
    summary="Searches CDS views in SAP (enforces unconnected guardrail in Milestone 1)",
)
async def search_cds_views(
    q: str = Query(..., min_length=2, description="Search term for view name or business description"),
    package: Optional[str] = Query(None, description="Optional ABAP package filter"),
    layer: Optional[CDSLayer] = Query(None, description="VDM layer filter"),
):
    """Invokes the SAP connector to locate CDS candidates."""
    connector = get_sap_connector()
    # In Milestone 1, this raises SAPNotConnectedError
    return await connector.search_cds(query=q, package=package, layer=layer)


@router.get(
    "/view/{view_name}",
    response_model=CDSMetadata,
    summary="Returns complete verified metadata for one CDS view",
)
async def get_cds_view_metadata(view_name: str):
    connector = get_sap_connector()
    return await connector.get_cds_metadata(view_name)


from pydantic import BaseModel
from src.services.discovery_service import CDSDiscoveryService
from src.services.extraction_wrapper_policy import can_create_extraction_wrapper

discovery_svc = CDSDiscoveryService()


class DiscoveryRunPayload(BaseModel):
    requirement: str
    required_fields: List[dict]
    search_mode: str = "fast"
    use_cache: bool = True
    selected_domains: Optional[List[str]] = None
    extraction_enabled_only: Optional[bool] = None
    require_delta_support: Optional[bool] = None
    standard_only: bool = True


@router.get("/metadata-status", summary="Gets real metadata index status and connection stats")
async def get_metadata_status():
    return discovery_svc.get_metadata_status()


@router.post("/refresh-metadata", summary="Refreshes local index from live SAP ADT")
async def refresh_metadata():
    return await discovery_svc.refresh_metadata()


@router.get("/direct-search", summary="Direct search across indexed CDS views")
async def direct_search(q: str = Query(..., min_length=1)):
    results = await discovery_svc.direct_search(q)
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("cds_name"):
                status = await discovery_svc.get_verified_component_extraction_status(
                    item["cds_name"], allow_live_source_retrieval=False
                )
                item["component_extraction_statuses"] = [status]
                item["can_create_extraction_wrapper"] = can_create_extraction_wrapper(status)
    return results


@router.get("/view-details/{view_name}", summary="Full details for one CDS view")
async def get_view_details_full(view_name: str):
    details = await discovery_svc.get_view_details(view_name)
    extraction_status = await discovery_svc.get_verified_component_extraction_status(view_name)
    if not details:
        return {
            "cds_name": view_name,
            "description": "Not available from current ADT metadata.",
            "domain": "Cross-Application / Core",
            "grain": "Entity",
            "source_type": "CDS View Entity",
            "package": "SAP_STANDARD",
            "fields": [],
            "keys": [],
            "associations": [],
            "joins": [],
            "odata_available": False,
            "odata_status": "Unknown",
            "odata_annotation": "Not available from current ADT metadata.",
            "component_extraction_statuses": [extraction_status],
            "can_create_extraction_wrapper": can_create_extraction_wrapper(extraction_status),
        }
    details["component_extraction_statuses"] = [extraction_status]
    details["can_create_extraction_wrapper"] = can_create_extraction_wrapper(extraction_status)
    return details


@router.get("/source/{view_name}", summary="Fetches genuine DDL source from SAP ADT with field tracing and status")
async def get_live_cds_source(
    view_name: str,
    fields: Optional[str] = Query(None, description="Optional comma-separated list of required fields to trace against DDL source"),
):
    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    return await discovery_svc.get_view_source_details(view_name, requested_fields=field_list)


@router.post("/run", summary="Run complete un-truncated discovery with single and multi-CDS validation")
async def run_discovery_pipeline(payload: DiscoveryRunPayload):
    return await discovery_svc.run_discovery(
        requirement_text=payload.requirement,
        required_fields=payload.required_fields,
        search_mode=payload.search_mode,
        use_cache=payload.use_cache,
        selected_domains=payload.selected_domains,
        extraction_enabled_only=payload.extraction_enabled_only,
        require_delta_support=payload.require_delta_support,
        standard_only=payload.standard_only,
    )


@router.get("/extraction-status/{view_name}", summary="Checks authoritative data extraction enabled status for a CDS view")
async def get_view_extraction_status(view_name: str):
    """Returns per-view status only when source and SAP catalog evidence agree."""
    evidence = await discovery_svc.get_verified_component_extraction_status(view_name)
    status = evidence.get("status", "UNKNOWN")
    return {
        **evidence,
        "extraction_status": "VERIFIED_ENABLED" if status == "TRUE" else "VERIFIED_NOT_ENABLED" if status == "FALSE" else "UNVERIFIED",
        "data_extraction_enabled": True if status == "TRUE" else False if status == "FALSE" else None,
        "can_create_extraction_wrapper": can_create_extraction_wrapper(evidence),
    }



class IncrementalSyncPayload(BaseModel):
    prefixes: Optional[List[str]] = None
    max_per_prefix: int = 100


@router.get("/preview/{view_name}", summary="Fetches live data preview records for a CDS view from SAP ADT")
async def get_cds_data_preview(
    view_name: str,
    max_rows: int = Query(10, ge=1, le=50, description="Max rows to preview"),
):
    """Executes read-only live data preview query on target CDS view."""
    return await discovery_svc.get_data_preview(cds_name=view_name, max_rows=max_rows)


@router.post("/sync-incremental", summary="Incrementally syncs custom/recently modified CDS objects from SAP DEV")
async def sync_incremental_endpoint(payload: Optional[IncrementalSyncPayload] = None):
    pfx = payload.prefixes if payload else None
    max_pfx = payload.max_per_prefix if payload else 100
    return await discovery_svc.sync_incremental(prefixes=pfx, max_per_prefix=max_pfx)


from src.services.table_discovery_service import TableDiscoveryService

table_discovery_svc = TableDiscoveryService()


class TableDiscoveryPayload(BaseModel):
    tables: Union[str, List[str]]
    matching_mode: str = "ANY"
    max_depth: int = 4
    standard_only: bool = True


@router.get("/table-lineage", summary="Discovers CDS views that consume one or multiple SAP tables as source, join, or association")
async def get_table_lineage_query(
    tables: str = Query(..., description="SAP transparent table name(s), comma, space, or 'and' separated"),
    matching_mode: str = Query("ANY", description="'ANY' or 'ALL' table matching mode"),
    extraction_enabled_only: bool = Query(True, description="Filter strictly to views confirmed in I_DataExtractionEnabledView"),
    require_delta_support: bool = Query(False, description="Require Delta Change Data Capture support"),
    standard_only: bool = Query(True, description="Filter strictly to genuine standard SAP CDS views (excludes custom Z*/Y* views)"),
):
    """Returns unique CDS views consuming the specified SAP table(s) with positively verified extraction status."""
    tbl_clean = tables.strip()
    logger.info(f"[TableLineage] Searching CDS views for transparent table(s): {tbl_clean} (mode={matching_mode}, delta={require_delta_support}, standard_only={standard_only})")
    result = await table_discovery_svc.run_discovery(
        tbl_clean,
        matching_mode=matching_mode,
        standard_only=standard_only,
    )
    if require_delta_support:
        result["recommendations"] = [r for r in result["recommendations"] if r.get("cdc_status") == "CDC_SUPPORTED"]
        result["summary_metrics"]["unique_extraction_recommended"] = len(result["recommendations"])
    return result


@router.get("/table-lineage/{table_name:path}", summary="Discovers all CDS views that take a transparent SAP table as source, join, or association")
async def get_table_lineage_endpoint(
    table_name: str,
    matching_mode: str = Query("ANY", description="'ANY' or 'ALL' table matching mode"),
    extraction_enabled_only: bool = Query(True, description="Filter strictly to views confirmed in I_DataExtractionEnabledView"),
    require_delta_support: bool = Query(False, description="Require Delta Change Data Capture support"),
    standard_only: bool = Query(True, description="Filter strictly to genuine standard SAP CDS views (excludes custom Z*/Y* views)"),
):
    """Returns all CDS views that consume the specified SAP table as primary source, join, or association."""
    tbl = table_name.strip()
    logger.info(f"[TableLineage] Searching CDS views for transparent table(s): {tbl} (mode={matching_mode}, delta={require_delta_support}, standard_only={standard_only})")
    result = await table_discovery_svc.run_discovery(
        tbl,
        matching_mode=matching_mode,
        standard_only=standard_only,
    )
    if require_delta_support:
        result["recommendations"] = [r for r in result["recommendations"] if r.get("cdc_status") == "CDC_SUPPORTED"]
        result["summary_metrics"]["unique_extraction_recommended"] = len(result["recommendations"])
    return result


@router.post("/table-discovery/run", summary="Executes complete Table-to-CDS extraction discovery")
async def run_table_discovery_endpoint(payload: TableDiscoveryPayload):
    """Executes complete Table-to-CDS extraction discovery with recursive dependency graph and strict extraction verification."""
    logger.info(f"[TableDiscovery] Run requested for tables: {payload.tables} (mode={payload.matching_mode}, standard_only={payload.standard_only})")
    return await table_discovery_svc.run_discovery(
        tables_input=payload.tables,
        matching_mode=payload.matching_mode,
        max_depth=payload.max_depth,
        standard_only=payload.standard_only,
    )


@router.get("/table-discovery/runs", summary="Retrieves history of past table discovery runs")
async def get_table_discovery_runs(limit: int = Query(50, ge=1, le=100)):
    """Retrieves history of past table discovery runs stored in SQLite."""
    return table_discovery_svc.get_run_history(limit=limit)


@router.get("/table-discovery/runs/{run_id}", summary="Retrieves details and results for a specific discovery run")
async def get_table_discovery_run_details(run_id: str):
    """Retrieves details, inputs, results, and dependency paths for a specific discovery run."""
    details = table_discovery_svc.get_run_details(run_id)
    if not details:
        return {"error": f"Run {run_id} not found", "run_id": run_id}
    return details


from src.services.cds_classifier import cds_classifier
from src.services.join_validator import join_validator, JoinRelationship, OutputFieldSelection


class AllFieldsPayload(BaseModel):
    view_names: List[str]


class ValidateJoinsPayload(BaseModel):
    selected_views: List[str]
    relationships: List[dict] = []
    output_fields: Optional[List[dict]] = None


@router.get("/classify/{cds_name}", summary="Classifies a CDS view as BASIC, COMPOSITE, CONSUMPTION, PARAMETERIZED, or UNKNOWN")
async def classify_cds_view(cds_name: str):
    """Classifies a CDS view candidate using authentic SAP DDL source and verified metadata."""
    res = cds_classifier.classify_view(cds_name)
    return res.to_dict()


@router.post("/all-fields", summary="Loads all verified technical fields for selected CDS views")
async def get_all_fields_endpoint(payload: AllFieldsPayload):
    """Returns all available fields with data types, lengths, decimals, and key status for selected views."""
    return join_validator.get_all_fields_for_views(payload.view_names)


@router.post("/validate-joins", summary="Validates multi-CDS relationships, data types, cardinalities, and risks")
async def validate_joins_endpoint(payload: ValidateJoinsPayload):
    """Performs deterministic join validation distinguishing SAP-verified vs user-proposed relationships."""
    relationships = [JoinRelationship(**r) for r in payload.relationships] if payload.relationships else []
    output_fields = [OutputFieldSelection(**f) for f in payload.output_fields] if payload.output_fields else None
    res = join_validator.validate_join_configuration(
        selected_views=payload.selected_views,
        relationships=relationships,
        output_fields=output_fields,
    )
    return res.model_dump()


@router.post("/approve-joins", summary="Explicit human approval gate for source CDS and join configuration")
async def approve_joins_endpoint(payload: ValidateJoinsPayload):
    """Enforces human approval of source CDS and join configuration before LLM code generation."""
    relationships = [JoinRelationship(**r) for r in payload.relationships] if payload.relationships else []
    output_fields = [OutputFieldSelection(**f) for f in payload.output_fields] if payload.output_fields else None
    res = join_validator.approve_join_configuration(
        selected_views=payload.selected_views,
        relationships=relationships,
        output_fields=output_fields,
    )
    return res.model_dump()



