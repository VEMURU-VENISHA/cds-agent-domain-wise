import unittest
from unittest.mock import AsyncMock, patch

from src.api.routes import requirements


class MappingExtractionResultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_true_and_false_do_not_query_wrapper_registry(self):
        requirements.discovery_svc.get_verified_component_extraction_status = AsyncMock(
            side_effect=[{"sap_metadata_status": "TRUE"}, {"sap_metadata_status": "FALSE"}]
        )
        with patch(
            "src.services.wrapper_registry.get_views_wrapper_mapping_details", new=AsyncMock()
        ) as registry:
            result = await requirements.get_views_wrapper_mapping_endpoint(
                requirements.ViewsWrapperMappingPayload(selected_views=["I_ENABLED", "I_DISABLED"])
            )

        self.assertEqual([row["data_extraction_status"] for row in result["views"]], ["TRUE", "FALSE"])
        self.assertEqual([row["wrapper_cds_view"] for row in result["views"]], ["Not Required", "Not Required"])
        self.assertFalse(any(row["can_create_wrapper"] for row in result["views"]))
        registry.assert_not_awaited()

    async def test_unknown_view_returns_actual_registered_wrapper(self):
        requirements.discovery_svc.get_verified_component_extraction_status = AsyncMock(
            return_value={"sap_metadata_status": "UNKNOWN"}
        )
        with patch(
            "src.services.wrapper_registry.get_views_wrapper_mapping_details",
            new=AsyncMock(return_value=[{
                "source_cds_view": "I_SOURCE",
                "description": "Source label",
                "has_wrapper": True,
                "wrapper_cds_view": "ZC_PERSISTED_WRAPPER",
                "mappings": [],
            }]),
        ) as registry:
            result = await requirements.get_views_wrapper_mapping_endpoint(
                requirements.ViewsWrapperMappingPayload(selected_views=["I_SOURCE"])
            )

        row = result["views"][0]
        self.assertEqual(row["data_extraction_status"], "UNKNOWN")
        self.assertEqual(row["wrapper_cds_view"], "ZC_PERSISTED_WRAPPER")
        self.assertFalse(row["can_create_wrapper"])
        registry.assert_awaited_once()

    async def test_unknown_view_without_wrapper_remains_eligible_for_creation(self):
        requirements.discovery_svc.get_verified_component_extraction_status = AsyncMock(
            return_value={"sap_metadata_status": "UNKNOWN"}
        )
        with patch(
            "src.services.wrapper_registry.get_views_wrapper_mapping_details",
            new=AsyncMock(return_value=[{
                "source_cds_view": "I_SOURCE",
                "description": "Source label",
                "has_wrapper": False,
                "wrapper_cds_view": "Not Created",
                "mappings": [],
            }]),
        ):
            result = await requirements.get_views_wrapper_mapping_endpoint(
                requirements.ViewsWrapperMappingPayload(selected_views=["I_SOURCE"])
            )

        row = result["views"][0]
        self.assertEqual(row["wrapper_cds_view"], "Not Created")
        self.assertTrue(row["can_create_wrapper"])


if __name__ == "__main__":
    unittest.main()
