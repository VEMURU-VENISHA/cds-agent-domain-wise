"""Tests for wrapper activation endpoints in requirements.py.

Verifies:
1. 5 distinct statuses in response:
   - object_shell_created
   - source_uploaded
   - source_verified
   - activation_request_accepted
   - activation_confirmed
2. Preflight existence check before activation.
3. Read-only wrapper status query endpoint.
"""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from pydantic import BaseModel

from src.api.routes.requirements import (
    activate_wrapper_in_sap_endpoint,
    get_wrapper_status_endpoint,
    ActivateWrapperInSAPPayload,
)


@pytest.mark.asyncio
async def test_activate_wrapper_preflight_blocks_when_object_missing():
    payload = ActivateWrapperInSAPPayload(
        wrapper_cds_name="ZNOT_EXISTING",
        transport_request="S3XK900001",
        confirmed=True,
    )
    with patch("src.sap.adt_client.SAPADTClient.get_ddls_activation_state", new_callable=AsyncMock) as mock_state:
        mock_state.return_value = {"exists": False}
        with pytest.raises(HTTPException) as exc_info:
            await activate_wrapper_in_sap_endpoint(payload)
        assert exc_info.value.status_code == 404
        assert "does not exist in SAP repository" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_activate_wrapper_returns_5_distinct_statuses():
    payload = ActivateWrapperInSAPPayload(
        wrapper_cds_name="ZC_TEST_WRAPPER",
        transport_request="S3XK900001",
        confirmed=True,
    )
    with patch("src.sap.adt_client.SAPADTClient.get_ddls_activation_state", new_callable=AsyncMock) as mock_state, \
         patch("src.sap.adt_client.SAPADTClient.activate_ddls_object", new_callable=AsyncMock) as mock_activate:
        mock_state.return_value = {
            "exists": True,
            "version": "inactive",
            "is_active": False,
        }
        mock_activate.return_value = {
            "success": True,
            "activation_request_accepted": True,
            "activation_confirmed": True,
            "activation_status": "ACTIVE",
            "status_code": 200,
            "errors": [],
            "warnings": [],
            "infos": [],
            "messages": [],
            "has_blocking_errors": False,
            "error_message": None,
            "activation_log_uri": "/sap/bc/adt/ddic/logs/db/ACTDDLSZC_TEST_WRAPPER",
            "verified_state": {"version": "active", "is_active": True},
        }

        res = await activate_wrapper_in_sap_endpoint(payload)

        # Confirm all 5 distinct statuses are present:
        assert res["object_shell_created"] is True
        assert res["source_uploaded"] is True
        assert res["source_verified"] is True
        assert res["activation_request_accepted"] is True
        assert res["activation_confirmed"] is True
        assert res["activation_status"] == "ACTIVE"
        assert res["activation_log_uri"] == "/sap/bc/adt/ddic/logs/db/ACTDDLSZC_TEST_WRAPPER"


@pytest.mark.asyncio
async def test_get_wrapper_status_read_only():
    with patch("src.sap.adt_client.SAPADTClient.get_ddls_activation_state", new_callable=AsyncMock) as mock_state, \
         patch("src.sap.adt_client.SAPADTClient.get_ddl_source_etag", new_callable=AsyncMock) as mock_etag:
        mock_state.return_value = {
            "exists": True,
            "version": "active",
            "is_active": True,
            "in_inactive_queue": False,
            "activation_log_uri": "/sap/bc/adt/ddic/logs/db/ACTDDLSZC_DATAWRAPPER1",
            "changed_at": "2026-11-03T12:37:56Z",
            "changed_by": "AIENAPARTHIP",
        }
        mock_etag.return_value = "202611031237560011"

        res = await get_wrapper_status_endpoint("ZC_DATAWRAPPER1")

        assert res["exists"] is True
        assert res["is_active"] is True
        assert res["activation_status"] == "ACTIVE"
        assert res["activation_confirmed"] is True
        assert res["source_etag"] == "202611031237560011"
        assert res["object_shell_created"] is True
        assert res["source_uploaded"] is True
        assert res["source_verified"] is True
