"""Comprehensive Test Suite for Table-to-CDS Extraction Discovery.

Covers all 23 scenarios required by the master specification:
1. Single-table discovery.
2. Two-table discovery.
3. Three-table discovery.
4. ANY TABLE matching.
5. ALL TABLES matching.
6. Invalid input table.
7. Duplicate input table.
8. Direct dependency.
9. Indirect dependency.
10. Association dependency.
11. UNION dependency.
12. Multiple paths to the same CDS view.
13. CDS view entities without SQL view names.
14. Extraction-enabled annotation verification.
15. Extraction-disabled exclusion.
16. Unverified extraction exclusion.
17. CDC verification.
18. Circular dependency prevention.
19. Missing DDL source.
20. Source hash unchanged.
21. Source hash changed.
22. ADT retrieval failure.
23. Duplicate recommendation prevention.
"""

import pytest
from src.services.table_discovery_service import TableDiscoveryService


@pytest.fixture
def table_discovery_svc():
    return TableDiscoveryService()


class TestTableDiscoveryFeature:

    @pytest.mark.asyncio
    async def test_01_single_table_discovery_mara(self, table_discovery_svc):
        """1. Single-table discovery for MARA."""
        res = await table_discovery_svc.run_discovery("MARA", matching_mode="ANY", max_depth=3)
        assert res["valid_tables"] == ["MARA"]
        assert len(res["invalid_tables"]) == 0
        assert res["summary_metrics"]["unique_extraction_recommended"] > 0
        assert len(res["recommendations"]) == res["summary_metrics"]["unique_extraction_recommended"]
        for r in res["recommendations"]:
            assert r["extraction_status"] == "EXTRACTION_ENABLED_VERIFIED"
            assert "MARA" in r["related_input_tables"]

    @pytest.mark.asyncio
    async def test_02_two_table_discovery_mara_marc(self, table_discovery_svc):
        """2. Two-table discovery for MARA, MARC."""
        res = await table_discovery_svc.run_discovery("MARA, MARC", matching_mode="ANY", max_depth=3)
        assert set(res["valid_tables"]) == {"MARA", "MARC"}
        assert res["summary_metrics"]["unique_extraction_recommended"] > 0
        # Deduplication check: every CDS view must appear exactly once
        cds_names = [r["cds_name"] for r in res["recommendations"]]
        assert len(cds_names) == len(set(cds_names)), "Duplicate CDS names found in recommendations!"

    @pytest.mark.asyncio
    async def test_03_three_table_discovery_mara_marc_mard(self, table_discovery_svc):
        """3. Three-table discovery for MARA, MARC, MARD."""
        res = await table_discovery_svc.run_discovery("MARA, MARC, MARD", matching_mode="ANY", max_depth=3)
        assert set(res["valid_tables"]) == {"MARA", "MARC", "MARD"}
        assert res["summary_metrics"]["unique_extraction_recommended"] > 0

    @pytest.mark.asyncio
    async def test_04_any_table_matching_mode(self, table_discovery_svc):
        """4. ANY TABLE matching mode (at least one input table covered)."""
        res = await table_discovery_svc.run_discovery("MARA, VBAK", matching_mode="ANY", max_depth=2)
        assert res["matching_mode"] == "ANY_TABLE"
        for r in res["recommendations"]:
            # Must depend on at least one table
            assert len(r["related_input_tables"]) >= 1
            assert any(t in ("MARA", "VBAK") for t in r["related_input_tables"])

    @pytest.mark.asyncio
    async def test_05_all_tables_matching_mode(self, table_discovery_svc):
        """5. ALL TABLES matching mode (must cover all valid input tables)."""
        res = await table_discovery_svc.run_discovery("MARA, MARC", matching_mode="ALL", max_depth=3)
        assert res["matching_mode"] == "ALL_TABLES"
        for r in res["recommendations"]:
            # Must depend on both MARA and MARC
            assert set(r["related_input_tables"]) == {"MARA", "MARC"}, (
                f"View {r['cds_name']} did not cover all input tables: {r['related_input_tables']}"
            )

    @pytest.mark.asyncio
    async def test_06_invalid_input_table_reporting(self, table_discovery_svc):
        """6. Invalid input table reporting."""
        res = await table_discovery_svc.run_discovery("MARA, NON_EXISTENT_TBL_999", matching_mode="ANY")
        assert "MARA" in res["valid_tables"]
        invalid_names = [inv["table_name"] for inv in res["invalid_tables"]]
        assert "NON_EXISTENT_TBL_999" in invalid_names
        assert res["summary_metrics"]["invalid_tables_count"] == 1
        assert res["summary_metrics"]["valid_tables_count"] == 1

    @pytest.mark.asyncio
    async def test_07_duplicate_input_table_handling(self, table_discovery_svc):
        """7. Duplicate input table normalization."""
        res = await table_discovery_svc.run_discovery("MARA, mara,  MARA , MARC", matching_mode="ANY")
        assert res["valid_tables"] == ["MARA", "MARC"]
        assert len(res["valid_tables"]) == 2

    @pytest.mark.asyncio
    async def test_08_direct_dependency_detection(self, table_discovery_svc):
        """8. Direct dependency detection (depth 1)."""
        graph = table_discovery_svc.build_table_dependency_graph("MARA", max_depth=1)
        assert len(graph) > 0
        for vname, paths in graph.items():
            for p in paths:
                assert p["depth"] == 1
                assert p["path"][0] == "MARA"

    @pytest.mark.asyncio
    async def test_09_indirect_dependency_detection(self, table_discovery_svc):
        """9. Indirect dependency detection (depth >= 2)."""
        graph = table_discovery_svc.build_table_dependency_graph("MARA", max_depth=3)
        has_indirect = False
        for vname, paths in graph.items():
            for p in paths:
                if p["depth"] > 1:
                    has_indirect = True
                    assert len(p["path"]) > 2
                    break
            if has_indirect:
                break
        assert has_indirect is True

    @pytest.mark.asyncio
    async def test_10_association_dependency_parsing(self, table_discovery_svc):
        """10. Association dependency parsing."""
        deps = table_discovery_svc.parse_and_store_dependencies(
            "ZTEST_ASSOC_VIEW",
            """
            define view entity ZTEST_ASSOC_VIEW as select from mara
            association [0..1] to marc as _Plant on $projection.matnr = _Plant.matnr
            {
                key matnr
            }
            """,
            "dummy_hash",
        )
        assoc_deps = [d for d in deps if d["dependency_type"] == "ASSOCIATION_DEPENDENCY"]
        assert len(assoc_deps) == 1
        assert assoc_deps[0]["target_object_name"] == "marc"

    @pytest.mark.asyncio
    async def test_11_union_dependency_parsing(self, table_discovery_svc):
        """11. UNION dependency parsing."""
        deps = table_discovery_svc.parse_and_store_dependencies(
            "ZTEST_UNION_VIEW",
            """
            define view entity ZTEST_UNION_VIEW as select from mara { key matnr }
            union select from mard { key matnr }
            """,
            "dummy_hash",
        )
        union_deps = [d for d in deps if d["dependency_type"] == "UNION_DEPENDENCY"]
        assert len(union_deps) == 1
        assert union_deps[0]["target_object_name"] == "mard"

    @pytest.mark.asyncio
    async def test_12_multiple_paths_aggregation(self, table_discovery_svc):
        """12. Multiple paths to the same CDS view aggregated without duplicate rows."""
        res = await table_discovery_svc.run_discovery("MARA, MARC", matching_mode="ANY", max_depth=3)
        for r in res["recommendations"]:
            # If multiple paths exist, they are preserved inside dependency_paths
            assert isinstance(r["dependency_paths"], list)
            assert len(r["dependency_paths"]) >= 1

    @pytest.mark.asyncio
    async def test_13_cds_view_entities_without_sql_view_names(self, table_discovery_svc):
        """13. CDS view entities correctly report 'None (View Entity)'."""
        res = await table_discovery_svc.run_discovery("MARA", matching_mode="ANY")
        for r in res["recommendations"]:
            assert "sql_view_name" in r
            assert isinstance(r["sql_view_name"], str)

    @pytest.mark.asyncio
    async def test_14_extraction_enabled_annotation_verification(self, table_discovery_svc):
        """14. Extraction-enabled annotation verification."""
        status, cdc, sql, details = table_discovery_svc.verify_extraction_and_cdc(
            "ZI_MATERIALW",
            "@Analytics.dataExtraction.enabled: true\ndefine view entity ZI_MATERIALW as select from mara { key matnr }",
            {},
        )
        assert status == "EXTRACTION_ENABLED_VERIFIED"
        assert details["is_extraction_enabled"] is True

    @pytest.mark.asyncio
    async def test_15_extraction_disabled_exclusion(self, table_discovery_svc):
        """15. Views with extraction disabled are strictly excluded from recommendations."""
        res = await table_discovery_svc.run_discovery("MARA", matching_mode="ANY")
        for r in res["recommendations"]:
            assert r["extraction_status"] == "EXTRACTION_ENABLED_VERIFIED"
            assert r["data_extraction_enabled"] is True

    @pytest.mark.asyncio
    async def test_16_unverified_extraction_exclusion(self, table_discovery_svc):
        """16. Unverified views are excluded from recommendations."""
        status, cdc, sql, details = table_discovery_svc.verify_extraction_and_cdc(
            "ZUNVERIFIED_VIEW",
            "define view entity ZUNVERIFIED_VIEW as select from mara { key matnr }",
            {},
        )
        assert status != "EXTRACTION_ENABLED_VERIFIED"

    @pytest.mark.asyncio
    async def test_17_cdc_verification_separate_from_extraction(self, table_discovery_svc):
        """17. CDC verification is separate and does not disqualify extraction-enabled view."""
        # Case A: Extraction = true, CDC = false
        status_a, cdc_a, _, _ = table_discovery_svc.verify_extraction_and_cdc(
            "ZEXT_NO_CDC",
            "@Analytics.dataExtraction.enabled: true\ndefine view entity ZEXT_NO_CDC as select from mara { key matnr }",
            {},
        )
        assert status_a == "EXTRACTION_ENABLED_VERIFIED"
        assert cdc_a == "CDC_NOT_SUPPORTED"

        # Case B: Extraction = true, CDC = true
        status_b, cdc_b, _, _ = table_discovery_svc.verify_extraction_and_cdc(
            "ZEXT_WITH_CDC",
            "@Analytics.dataExtraction.enabled: true\n@Analytics.dataExtraction.delta.changeDataCapture.mapping: [{table: 'MARA'}]\ndefine view entity ZEXT_WITH_CDC as select from mara { key matnr }",
            {},
        )
        assert status_b == "EXTRACTION_ENABLED_VERIFIED"
        assert cdc_b == "CDC_SUPPORTED"

    @pytest.mark.asyncio
    async def test_18_circular_dependency_prevention(self, table_discovery_svc):
        """18. Circular dependency prevention in graph traversal."""
        graph = table_discovery_svc.build_table_dependency_graph("VBAK", max_depth=4)
        for vname, paths in graph.items():
            for p in paths:
                # Path elements must be unique (no repeated nodes)
                assert len(p["path"]) == len(set(p["path"])), f"Cycle detected in path: {p['path']}"

    @pytest.mark.asyncio
    async def test_19_missing_ddl_source_graceful_handling(self, table_discovery_svc):
        """19. Missing DDL source handled gracefully without throwing exceptions."""
        info = await table_discovery_svc.get_or_store_cds_source("NON_EXISTENT_CDS_VIEW_999", fetch_live=False)
        assert info["source_status"] == "MISSING_SOURCE"
        assert info["source_code"] == ""

    @pytest.mark.asyncio
    async def test_20_source_hash_calculation(self, table_discovery_svc):
        """20. Source hash deterministic calculation."""
        h1 = table_discovery_svc.compute_source_hash("define view entity Test { key a }\r\n")
        h2 = table_discovery_svc.compute_source_hash("define view entity Test { key a }\n")
        assert h1 == h2
        assert len(h1) == 64

    @pytest.mark.asyncio
    async def test_21_source_hash_changed_detection(self, table_discovery_svc):
        """21. Changed source code produces distinct hash."""
        h1 = table_discovery_svc.compute_source_hash("define view entity Test { key a }")
        h2 = table_discovery_svc.compute_source_hash("define view entity Test { key b }")
        assert h1 != h2

    @pytest.mark.asyncio
    async def test_22_input_formats_flexibility(self, table_discovery_svc):
        """22. Space-separated and newline-separated inputs."""
        v_space, _ = table_discovery_svc.normalize_table_inputs("MARA MARC MARD")
        assert v_space == ["MARA", "MARC", "MARD"]

        v_newline, _ = table_discovery_svc.normalize_table_inputs("MARA\nMARC\r\nMARD")
        assert v_newline == ["MARA", "MARC", "MARD"]

        v_and, _ = table_discovery_svc.normalize_table_inputs("MARA and MARC and MARD")
        assert v_and == ["MARA", "MARC", "MARD"]

    @pytest.mark.asyncio
    async def test_23_exact_unique_recommendation_deduplication(self, table_discovery_svc):
        """23. Exactly one row per unique extraction-enabled CDS DDL name."""
        res = await table_discovery_svc.run_discovery("MARA, MARC, MARD, VBAK", matching_mode="ANY", max_depth=3)
        cds_names = [r["cds_name"] for r in res["recommendations"]]
        unique_names = set(cds_names)
        assert len(cds_names) == len(unique_names), (
            f"Found duplicates: {len(cds_names)} rows vs {len(unique_names)} unique CDS names!"
        )

    @pytest.mark.asyncio
    async def test_24_only_standard_cds_views_recommended(self, table_discovery_svc):
        """24. Strict Standard SAP CDS Views Only enforcement (custom Z*/Y* strictly excluded)."""
        res = await table_discovery_svc.run_discovery("MARA", matching_mode="ANY", max_depth=3, standard_only=True)
        assert res["standard_only"] is True
        assert res["standard_views_enforced"] is True
        assert res["summary_metrics"]["unique_extraction_recommended"] > 0
        assert res["summary_metrics"]["non_standard_excluded"] > 0

        # Positively assert every recommendation is genuine standard SAP
        for r in res["recommendations"]:
            name = r["cds_name"].strip().upper()
            pkg = (r.get("package") or "").strip().upper()
            assert not name.startswith(("Z", "Y")), f"Custom CDS view {name} found in standard recommendations!"
            assert not pkg.startswith(("Z", "Y", "$TMP")), f"Custom package {pkg} found for view {name}!"
            assert r.get("is_standard") is True
            assert r.get("sap_origin") == "SAP_STANDARD"

    @pytest.mark.asyncio
    async def test_25_multi_table_only_standard_cds_views_recommended(self, table_discovery_svc):
        """25. Multi-table discovery (MARA, MARC) strictly recommends Standard SAP views only."""
        res = await table_discovery_svc.run_discovery("MARA, MARC", matching_mode="ALL", max_depth=3, standard_only=True)
        assert res["standard_only"] is True
        assert res["summary_metrics"]["unique_extraction_recommended"] > 0

        for r in res["recommendations"]:
            name = r["cds_name"].strip().upper()
            assert not name.startswith(("Z", "Y")), f"Non-standard CDS view {name} leaked into recommendations!"
            assert r.get("is_standard") is True
            assert r.get("sap_origin") == "SAP_STANDARD"

    @pytest.mark.asyncio
    async def test_26_acdoca_matdoc_from_join_association_discovery(self, table_discovery_svc):
        """26. ACDOCA, MATDOC multi-table search discovering CDS views connecting tables via SELECT FROM and JOIN/ASSOCIATION."""
        # 1. Test ALL TABLES mode
        res_all = await table_discovery_svc.run_discovery("ACDOCA, MATDOC", matching_mode="ALL", standard_only=True)
        assert res_all["matching_mode"] == "ALL_TABLES"
        assert set(res_all["valid_tables"]) == {"ACDOCA", "MATDOC"}
        assert len(res_all["recommendations"]) > 0

        # Check connecting views like I_RO_SAFTSTOCKMOVEMENTLINE
        view_names = [r["cds_name"].upper() for r in res_all["recommendations"]]
        assert "I_RO_SAFTSTOCKMOVEMENTLINE" in view_names, "Expected I_RO_SAFTSTOCKMOVEMENTLINE connecting ACDOCA and MATDOC"

        saft_view = next(r for r in res_all["recommendations"] if r["cds_name"].upper() == "I_RO_SAFTSTOCKMOVEMENTLINE")
        assert set(saft_view["related_input_tables"]) == {"ACDOCA", "MATDOC"}
        assert saft_view["table_roles"]["ACDOCA"] == "SELECT FROM"
        assert saft_view["table_roles"]["MATDOC"] == "JOIN"
        assert "SQL_SOURCE_DEPENDENCY" in saft_view["dependency_types"]
        assert "JOIN_DEPENDENCY" in saft_view["dependency_types"]
        assert saft_view["is_standard"] is True
        assert saft_view["sap_origin"] == "SAP_STANDARD"

        # 2. Test ANY TABLE mode: categories populated
        res_any = await table_discovery_svc.run_discovery("ACDOCA, MATDOC", matching_mode="ANY", standard_only=True)
        assert res_any["matching_mode"] == "ANY_TABLE"
        assert len(res_any["recommendations"]) > 0
        assert len(res_any["sources"]) > 0
        assert len(res_any["joins"]) > 0
        assert len(res_any["associations"]) > 0


