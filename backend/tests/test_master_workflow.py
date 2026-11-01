"""Comprehensive verification suite for Master Implementation Instruction.

Covers:
- Part 2: Strict CDS Classification (BASIC accepted, COMPOSITE/CONSUMPTION/PARAMETERIZED/UNKNOWN excluded)
- Part 3: Extraction-First Discovery (I_DataExtractionEnabledView, direct reuse recommendation, no wrapper)
- Part 4: Fallback Search & Wrapper Source Discovery (full catalog, non-extraction BASIC eligible as wrapper source)
- Part 5: Human-in-the-loop Source Selection (complete verified fields, associations, DDL, evidence)
- Part 6: Dedicated Join Configuration (all fields output selection, verified vs proposed, approval gate)
- Part 7: LLM Wrapper Generation (@Analytics.dataExtraction.enabled: true, original DDL passed, approved joins)
- Part 8: In-Memory CDS Validation (deterministic line numbers, SAP source-only unsupported, 0 writes)
- Part 9: LLM Validation Correction Loop (max 3 attempts, line-numbered correction prompt)
- Part 10 & 11: Human Review Gate & Post-Activation Extraction Confirmation (EXTRACTION_ENABLED_VERIFIED)
"""

import pytest
import sqlite3
from unittest.mock import AsyncMock, patch, MagicMock

from src.models.domain import CDSClassificationType, WorkflowStatus
from src.services.cds_classifier import cds_classifier
from src.services.discovery_service import CDSDiscoveryService
from src.services.join_validator import (
    join_validator,
    JoinRelationship,
    JoinCondition,
    OutputFieldSelection,
)
from src.services.cds_generation_validator import CDSGenerationValidator
from src.services.gemini_service import GeminiService


# ==============================================================================
# PART 2: STRICT CDS CLASSIFICATION TESTS
# ==============================================================================
class TestStrictCDSClassification:

    def test_basic_view_type_accepted(self):
        ddl = """
        @VDM.viewType: #BASIC
        define view entity I_Customer as select from kna1 {
            key kunnr as Customer,
            name1 as CustomerName
        }
        """
        res = cds_classifier.classify_source(ddl, "I_Customer")
        assert res.classification == CDSClassificationType.BASIC
        assert res.is_basic is True
        assert res.is_parameterized is False
        assert cds_classifier.is_basic(ddl) is True

    def test_nested_vdm_basic_accepted(self):
        ddl = """
        @VDM: { viewType: #BASIC }
        define view entity I_SalesOrder as select from vbak {
            key vbeln as SalesOrder
        }
        """
        res = cds_classifier.classify_source(ddl, "I_SalesOrder")
        assert res.classification == CDSClassificationType.BASIC
        assert res.is_basic is True
        assert cds_classifier.is_basic(ddl) is True

    def test_composite_view_type_excluded(self):
        ddl = """
        @VDM.viewType: #COMPOSITE
        define view entity I_SalesOrderComposite as select from I_SalesOrder {
            key SalesOrder
        }
        """
        res = cds_classifier.classify_source(ddl, "I_SalesOrderComposite")
        assert res.classification == CDSClassificationType.COMPOSITE
        assert res.is_basic is False
        assert cds_classifier.is_basic(ddl) is False

    def test_consumption_view_type_excluded(self):
        ddl = """
        @VDM.viewType: #CONSUMPTION
        define view entity C_SalesOrderManage as select from I_SalesOrder {
            key SalesOrder
        }
        """
        res = cds_classifier.classify_source(ddl, "C_SalesOrderManage")
        assert res.classification == CDSClassificationType.CONSUMPTION
        assert res.is_basic is False
        assert cds_classifier.is_basic(ddl) is False

    def test_parameterized_basic_view_strictly_excluded(self):
        ddl = """
        @VDM.viewType: #BASIC
        define view entity I_CurrencyExchange
          with parameters P_DisplayCurrency : waers
          as select from tcurr {
            key kurst as RateType
        }
        """
        res = cds_classifier.classify_source(ddl, "I_CurrencyExchange")
        assert res.classification == CDSClassificationType.PARAMETERIZED
        assert res.is_parameterized is True
        assert res.is_basic is False
        assert cds_classifier.is_basic(ddl) is False

    def test_unknown_view_type_without_evidence_excluded(self):
        ddl = """
        define view entity Z_CustomUnclassified as select from ztable {
            key id as ID
        }
        """
        res = cds_classifier.classify_source(ddl, "Z_CustomUnclassified")
        assert res.classification == CDSClassificationType.UNKNOWN
        assert res.is_basic is False
        assert cds_classifier.is_basic(ddl) is False


# ==============================================================================
# PART 3: EXTRACTION-FIRST DISCOVERY TESTS
# ==============================================================================
class TestExtractionFirstDiscovery:

    @pytest.mark.asyncio
    async def test_existing_extraction_enabled_basic_view_recommended_directly(self):
        service = CDSDiscoveryService()
        mock_sap_client = MagicMock()
        mock_sap_client.get_extraction_metadata_records = AsyncMock(return_value={
            "success": True,
            "is_complete": True,
            "records": {
                "I_SALESORDER": {
                    "view_name": "I_SalesOrder",
                    "description": "Sales Order Header",
                    "is_sap_released": True,
                    "delta_cdc_supported": True,
                    "extraction_status": "VERIFIED_ENABLED",
                }
            }
        })

        res = await service.run_discovery(
            requirement_text="Extract sales orders",
            required_fields=[
                {"name": "SalesOrder", "field_type": "DIRECT"},
                {"name": "SoldToParty", "field_type": "DIRECT"},
            ],
            sap_client=mock_sap_client,
            use_cache=False,
        )

        sol = res["solution_summary"]
        assert sol["solution_type"] == "EXISTING_EXTRACTION_ENABLED_CDS"
        assert sol["requires_create"] is False
        assert sol["do_not_generate_new_code"] is True
        assert sol["do_not_create_new_object"] is True
        assert res["workflow_status"] == WorkflowStatus.EXISTING_EXTRACTION_ENABLED_CDS.value

    @pytest.mark.asyncio
    async def test_basic_view_with_extraction_disabled_never_recommended_as_existing_extraction(self):
        service = CDSDiscoveryService()
        mock_sap_client = MagicMock()
        # In this mock, I_CUSTOMER is confirmed NOT extraction-enabled
        mock_sap_client.get_extraction_metadata_records = AsyncMock(return_value={
            "success": True,
            "is_complete": True,
            "records": {}
        })

        res = await service.run_discovery(
            requirement_text="Customer basic view analysis",
            required_fields=[
                {"name": "Customer", "field_type": "DIRECT"},
                {"name": "CustomerName", "field_type": "DIRECT"},
            ],
            sap_client=mock_sap_client,
            use_cache=False,
        )

        sol = res["solution_summary"]
        # Never recommend non-extraction view as EXISTING_EXTRACTION_ENABLED_CDS
        assert sol["solution_type"] != "EXISTING_EXTRACTION_ENABLED_CDS"
        # Instead, it routes to custom extraction wrapper generation
        assert sol["solution_type"] in ("SINGLE_CDS_WRAPPER_REQUIRED", "CUSTOM_WRAPPER_REQUIRED")
        assert sol.get("custom_wrapper_required") is True
        assert sol["requires_create"] is True
        assert res["workflow_status"] == WorkflowStatus.BASIC_CDS_DISCOVERY_COMPLETED.value


# ==============================================================================
# PART 4: FALLBACK SEARCH & WRAPPER SOURCE DISCOVERY TESTS
# ==============================================================================
class TestFallbackSearchWrapperSource:

    @pytest.mark.asyncio
    async def test_non_extraction_basic_view_accepted_as_wrapper_source(self):
        service = CDSDiscoveryService()
        mock_sap_client = MagicMock()
        mock_sap_client.get_extraction_metadata_records = AsyncMock(return_value={
            "success": True,
            "is_complete": True,
            "records": {}
        })

        res = await service.run_discovery(
            requirement_text="Sales order and customer analysis",
            required_fields=[
                {"name": "SalesOrder", "field_type": "DIRECT"},
                {"name": "CustomerName", "field_type": "DIRECT"},
            ],
            sap_client=mock_sap_client,
            use_cache=False,
        )

        assert res["workflow_status"] == WorkflowStatus.BASIC_CDS_DISCOVERY_COMPLETED.value
        cands = res.get("single_cds_candidates", [])
        assert len(cands) > 0
        for c in cands:
            assert c.get("classification") == "BASIC"
            assert c.get("is_basic") is True
            assert c.get("is_parameterized") is False


# ==============================================================================
# PART 5 & 6: HITL JOIN CONFIGURATION & APPROVAL GATE TESTS
# ==============================================================================
class TestJoinConfigurationAndApprovalGate:

    def test_load_all_fields_for_views(self):
        fields = join_validator.get_all_fields_for_views(["I_SalesOrder", "I_Customer"])
        assert "I_SALESORDER" in fields
        assert "I_CUSTOMER" in fields
        assert len(fields["I_SALESORDER"]) > 0
        so_fields = {f["field_name"] for f in fields["I_SALESORDER"]}
        assert "SalesOrder" in so_fields or "SALESORDER" in {f.upper() for f in so_fields}

    def test_join_validation_detects_cartesian_risk(self):
        # Two views with NO conditions proposed
        rel = JoinRelationship(
            left_cds="I_SalesOrder",
            right_cds="I_Customer",
            join_type="LEFT OUTER JOIN",
            conditions=[],  # No join conditions -> Cartesian danger!
        )
        res = join_validator.validate_join_configuration(
            selected_views=["I_SalesOrder", "I_Customer"],
            relationships=[rel],
        )
        assert res.is_valid is False
        error_codes = {e.code for e in res.errors}
        assert "CARTESIAN_PRODUCT_RISK" in error_codes

    def test_join_approval_gate_sets_status(self):
        rel = JoinRelationship(
            left_cds="I_SalesOrder",
            right_cds="I_Customer",
            join_type="LEFT OUTER JOIN",
            conditions=[
                JoinCondition(left_field="SoldToParty", right_field="Customer")
            ],
            relationship_evidence_type="SAP_VERIFIED",
        )
        res = join_validator.approve_join_configuration(
            selected_views=["I_SalesOrder", "I_Customer"],
            relationships=[rel],
        )
        assert res.is_valid is True
        assert res.is_approved is True
        assert res.status == WorkflowStatus.JOIN_CONFIGURATION_APPROVED


# ==============================================================================
# PART 7 & 8: IN-MEMORY VALIDATION & EXTRACTION ANNOTATION TESTS
# ==============================================================================
class TestInMemoryValidation:

    def test_analytics_extraction_annotation_enforced(self):
        code_without_extraction = """
        @AccessControl.authorizationCheck: #NOT_REQUIRED
        @EndUserText.label: 'Test View'
        define view entity ZC_Test as select from I_SalesOrder {
            key SalesOrder
        }
        """
        ctx = {"selected_views": ["I_SalesOrder"]}
        res = CDSGenerationValidator.investigate_and_validate_in_memory(code_without_extraction, ctx)
        assert res["valid"] is False
        error_rules = {e["rule"] for e in res["structured_errors"]}
        assert "EXTRACTION_ANNOTATION" in error_rules
        assert res["sap_source_validation_status"] == "SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED"
        assert res["sap_writes_performed"] is False

    def test_valid_code_with_extraction_annotation_passes_statically(self):
        valid_code = """
        @AccessControl.authorizationCheck: #NOT_REQUIRED
        @EndUserText.label: 'Sales Extraction Wrapper'
        @Analytics.dataExtraction.enabled: true
        define view entity ZC_SalesExtract as select from I_SalesOrder {
            key SalesOrder,
            SoldToParty
        }
        """
        ctx = {
            "selected_views": ["I_SalesOrder"],
            "mappings": [
                {"business_field": "SalesOrder", "selected_cds": "I_SalesOrder", "actual_field": "SalesOrder", "is_key": True},
                {"business_field": "SoldToParty", "selected_cds": "I_SalesOrder", "actual_field": "SoldToParty"},
            ],
        }
        res = CDSGenerationValidator.investigate_and_validate_in_memory(valid_code, ctx)
        assert res["valid"] is True
        assert res["status"] == "STATIC_VALIDATION_PASSED"
        assert res["sap_source_validation_status"] == "SAP_SOURCE_ONLY_VALIDATION_UNSUPPORTED"
        assert res["sap_writes_performed"] is False


# ==============================================================================
# PART 9: LLM VALIDATION CORRECTION LOOP TESTS
# ==============================================================================
class TestLLMCorrectionLoop:

    @pytest.mark.asyncio
    async def test_correction_loop_halts_after_max_attempts(self):
        gemini_svc = GeminiService()
        # Mock gemini to return invalid code repeatedly missing the extraction annotation
        invalid_code = """
        @EndUserText.label: 'Invalid'
        define view entity ZC_Bad as select from I_SalesOrder {
            key SalesOrder
        }
        """
        gen_mock = AsyncMock(return_value={
            "result": {"code": invalid_code},
            "valid": False,
        })
        regen_mock = AsyncMock(return_value={
            "result": {"code": invalid_code},
        })
        with patch.object(gemini_svc, "generate_cds_code", gen_mock), \
             patch.object(gemini_svc, "regenerate_code", regen_mock):
            res = await gemini_svc.generate_wrapper_with_correction_loop(
                requirement="Extract sales",
                selected_views=["I_SalesOrder"],
                mappings=[{"business_field": "SalesOrder", "cds_view": "I_SalesOrder", "actual_field": "SalesOrder"}],
                join_paths=[],
                max_attempts=3,
            )
            assert res["attempts"] == 3
            assert res["validation"]["valid"] is False
            assert res["status"] == "FAILED_VALIDATION"


# ==============================================================================
# PART 10 & 11: POST-ACTIVATION EXTRACTION VERIFICATION TESTS
# ==============================================================================
class TestPostActivationExtractionConfirmation:

    @pytest.mark.asyncio
    async def test_activation_result_contains_post_activation_status(self):
        from src.services.review_service import get_review_store
        from src.models.domain import (
            CDSReviewPackage,
            DecisionAction,
            ReviewStatus,
            ReviewAuditEvent,
            BusinessRequirement,
            Decision,
            utc_now,
        )

        store = get_review_store()
        # Create an approved package
        pkg_id = "REV-TEST-POST-ACT-001"
        req = BusinessRequirement(
            title="Extract Sales Orders",
            description="Requirement to extract sales orders and sold-to parties for analytics",
            submitted_by="test_user",
        )
        dec = Decision(
            requirement_id=req.id,
            action=DecisionAction.CREATE,
            confidence_score=1.0,
            fit_percentage=100.0,
            rationale="New wrapper",
            recommended_view="ZC_POSTACTTEST",
        )
        pkg = CDSReviewPackage(
            review_id=pkg_id,
            requirement_id=req.id,
            original_requirement=req,
            decision=dec,
            decision_action=DecisionAction.CREATE,
            decision_confidence=1.0,
            decision_rationale="New wrapper",
            status=ReviewStatus.APPROVED,
            reviewer="gatekeeper",
            object_name="ZC_POSTACTTEST",
            ddl_source="""
            @AccessControl.authorizationCheck: #NOT_REQUIRED
            @Analytics.dataExtraction.enabled: true
            define view entity ZC_POSTACTTEST as select from I_SalesOrder {
                key SalesOrder
            }
            """,
            transport_request="S4HK900123",
            created_at=utc_now(),
        )
        store._packages[pkg_id] = pkg

        # Mock latest_sap_checkrun to satisfy checkrun gate
        from src.models.domain import SAPReviewCheckrunResult
        from src.sap.adt_client import SAPADTClient
        pkg.latest_sap_checkrun = SAPReviewCheckrunResult(
            review_id=pkg_id,
            sap_object_name="ZC_POSTACTTEST",
            success=True,
            blocking_errors=0,
            warnings_count=0,
            checkrun_status="PASSED",
            source_hash=SAPADTClient.compute_source_hash(pkg.ddl_source),
        )

        mock_connector = MagicMock()
        from src.models.domain import SAPActivationResult
        mock_connector.activate_cds_in_sap = AsyncMock(return_value=SAPActivationResult(
            success=True,
            review_id=pkg_id,
            sap_object_name="ZC_POSTACTTEST",
            activation_status="ACTIVE",
            sap_write_performed=False,
        ))

        act_res = await store.execute_sap_activation(
            review_id=pkg_id,
            transport_request="S4HK900123",
            connector=mock_connector,
        )

        assert act_res.success is True
        assert act_res.post_activation_status == "EXTRACTION_ENABLED_VERIFIED"


class TestTableLineageStrictBasic:
    """Tests that search by table (table lineage) strictly returns non-parameterized BASIC views only."""

    @pytest.mark.asyncio
    async def test_single_table_lineage_returns_strictly_basic_views(self):
        service = CDSDiscoveryService()
        res = await service.get_table_lineage("VBAK")

        assert res["basic_cds_enforced"] is True
        assert res["classification_filter"] == "BASIC_ONLY"
        assert res["vdm_view_type"] == "BASIC"
        assert res["total_matches"] > 0
        assert len(res["sources"]) > 0

        # Verify all sources are strictly BASIC and non-parameterized
        for s in res["sources"]:
            assert s.get("classification") == "BASIC", f"Expected BASIC, got {s.get('classification')} for {s['cds_name']}"
            assert s.get("is_basic") is True
            assert s.get("is_parameterized") is False
            assert s.get("vdm_view_type") == "BASIC"
            assert "Verified @VDM.viewType: #BASIC" in s.get("classification_evidence", "")

        # Verify all joins are strictly BASIC and non-parameterized
        for j in res.get("joins", []):
            assert j.get("classification") == "BASIC", f"Expected BASIC, got {j.get('classification')} for {j['cds_name']}"
            assert j.get("is_basic") is True
            assert j.get("is_parameterized") is False
            assert j.get("vdm_view_type") == "BASIC"

        # Verify all associations are strictly BASIC and non-parameterized
        for a in res.get("associations", []):
            assert a.get("classification") == "BASIC", f"Expected BASIC, got {a.get('classification')} for {a['cds_name']}"
            assert a.get("is_basic") is True
            assert a.get("is_parameterized") is False
            assert a.get("vdm_view_type") == "BASIC"

        # Cross-verify: None of the views are COMPOSITE, CONSUMPTION, PARAMETERIZED, or UNKNOWN
        all_returned_names = {s["cds_name"] for s in res["sources"]} | {j["cds_name"] for j in res["joins"]} | {a["cds_name"] for a in res["associations"]}
        with service._get_db() as conn:
            c = conn.cursor()
            placeholders = ",".join("?" for _ in all_returned_names)
            c.execute(f"SELECT DISTINCT vdm_view_type, is_parameterized FROM cds_views WHERE cds_name IN ({placeholders})", list(all_returned_names))
            db_types = c.fetchall()
            for row in db_types:
                assert row["vdm_view_type"] == "BASIC", f"Non-BASIC view found in table lineage: {row['vdm_view_type']}"
                assert not row["is_parameterized"], f"Parameterized view found in table lineage: {row['is_parameterized']}"

    @pytest.mark.asyncio
    async def test_multi_table_lineage_returns_strictly_basic_views(self):
        service = CDSDiscoveryService()
        res = await service.get_table_lineage("VBAK, VBAP")

        assert res["is_multi_table"] is True
        assert res["basic_cds_enforced"] is True
        assert res["classification_filter"] == "BASIC_ONLY"
        assert res["total_combined_matches"] > 0

        # Verify all combined views are strictly BASIC and non-parameterized
        for cv in res["combined_views"]:
            assert cv.get("classification") == "BASIC", f"Expected BASIC, got {cv.get('classification')} for {cv['cds_name']}"
            assert cv.get("is_basic") is True
            assert cv.get("is_parameterized") is False
            assert cv.get("vdm_view_type") == "BASIC"
            assert "Verified @VDM.viewType: #BASIC" in cv.get("classification_evidence", "")

    @pytest.mark.asyncio
    async def test_empty_table_lineage_payload_has_basic_enforcement_metadata(self):
        service = CDSDiscoveryService()
        res = await service.get_table_lineage("")

        assert res["basic_cds_enforced"] is True
        assert res["classification_filter"] == "BASIC_ONLY"
        assert res["vdm_view_type"] == "BASIC"
        assert res["total_matches"] == 0
        assert res["sources"] == []


# ==============================================================================
# PART 11: DOMAIN-BASED DISCOVERY AND CUSTOM WRAPPER DETERMINATION TESTS
# ==============================================================================
class TestDomainBasedDiscoveryAndWrapperRequirement:

    @pytest.mark.asyncio
    async def test_single_domain_filtering_excludes_other_domains(self):
        service = CDSDiscoveryService()
        mock_sap_client = MagicMock()
        mock_sap_client.get_extraction_metadata_records = AsyncMock(return_value={
            "success": True,
            "is_complete": True,
            "records": {}
        })

        res = await service.run_discovery(
            requirement_text="Sales and distribution orders",
            required_fields=[
                {"name": "SalesOrder", "field_type": "DIRECT"},
                {"name": "SoldToParty", "field_type": "DIRECT"},
            ],
            selected_domains=["Sales"],
            sap_client=mock_sap_client,
            use_cache=False,
        )

        assert "candidates" in res
        assert len(res["candidates"]) > 0
        for cand in res["candidates"]:
            dom = cand.get("domain", "")
            app_comp = cand.get("application_component", "")
            assert service._check_domain_affinity(dom, app_comp, ["Sales"]) is True, (
                f"Candidate {cand['cds_name']} with domain '{dom}' and app_comp '{app_comp}' does not match 'Sales'"
            )

    @pytest.mark.asyncio
    async def test_multi_domain_discovery_combines_and_deduplicates(self):
        service = CDSDiscoveryService()
        mock_sap_client = MagicMock()
        mock_sap_client.get_extraction_metadata_records = AsyncMock(return_value={
            "success": True,
            "is_complete": True,
            "records": {}
        })

        res = await service.run_discovery(
            requirement_text="Cross-domain sales and accounting journal",
            required_fields=[
                {"name": "SalesOrder", "field_type": "DIRECT"},
                {"name": "CompanyCode", "field_type": "DIRECT"},
            ],
            selected_domains=["Sales", "Finance"],
            sap_client=mock_sap_client,
            use_cache=False,
        )

        assert "candidates" in res
        cand_names = [c["cds_name"].upper() for c in res["candidates"]]
        # Ensure no duplicates
        assert len(cand_names) == len(set(cand_names))

        # Every candidate must match at least one of the selected domains
        for cand in res["candidates"]:
            dom = cand.get("domain", "")
            app_comp = cand.get("application_component", "")
            assert service._check_domain_affinity(dom, app_comp, ["Sales", "Finance"]) is True, (
                f"Candidate {cand['cds_name']} with domain '{dom}' does not match Sales or Finance"
            )

    @pytest.mark.asyncio
    async def test_outcome_a_existing_extraction_available(self):
        service = CDSDiscoveryService()
        mock_sap_client = MagicMock()
        mock_sap_client.get_extraction_metadata_records = AsyncMock(return_value={
            "success": True,
            "is_complete": True,
            "records": {
                "I_SALESORDER": {
                    "view_name": "I_SalesOrder",
                    "description": "Sales Order Header",
                    "is_sap_released": True,
                    "delta_cdc_supported": True,
                    "extraction_status": "VERIFIED_ENABLED",
                }
            }
        })

        res = await service.run_discovery(
            requirement_text="Sales orders extraction",
            required_fields=[
                {"name": "SalesOrder", "field_type": "DIRECT"},
                {"name": "SoldToParty", "field_type": "DIRECT"},
            ],
            selected_domains=["Sales"],
            sap_client=mock_sap_client,
            use_cache=False,
        )

        sol = res["solution_summary"]
        assert sol["solution_type"] == "EXISTING_EXTRACTION_ENABLED_CDS"
        assert sol["headline"] == "EXISTING EXTRACTION-ENABLED CDS AVAILABLE"
        assert sol["custom_wrapper_required"] is False
        assert sol["requires_create"] is False
        assert sol["do_not_generate_new_code"] is True

    @pytest.mark.asyncio
    async def test_outcome_b_custom_wrapper_required(self):
        service = CDSDiscoveryService()
        mock_sap_client = MagicMock()
        # No views in extraction catalog
        mock_sap_client.get_extraction_metadata_records = AsyncMock(return_value={
            "success": True,
            "is_complete": True,
            "records": {}
        })

        res = await service.run_discovery(
            requirement_text="Sales order and customer analysis without extraction view",
            required_fields=[
                {"name": "SalesOrder", "field_type": "DIRECT"},
                {"name": "SoldToParty", "field_type": "DIRECT"},
            ],
            selected_domains=["Sales"],
            sap_client=mock_sap_client,
            use_cache=False,
        )

        sol = res["solution_summary"]
        assert sol["solution_type"] == "CUSTOM_WRAPPER_REQUIRED"
        assert sol["headline"] == "CUSTOM WRAPPER REQUIRED"
        assert sol["custom_wrapper_required"] is True
        assert sol["requires_create"] is True
        assert sol["do_not_generate_new_code"] is False
        assert len(sol["eligible_basic_source_views"]) > 0
        assert "insufficient_extraction_explanation" in sol


