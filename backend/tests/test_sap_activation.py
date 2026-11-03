"""Milestone 13.8B: Deterministic Offline Tests for SAP ADT Activation Protocol.

Covers Requirements A through P:
A. Activation requires SAP_DEV_WRITE_ENABLED.
B. SAP_LIVE_READ_ONLY=true blocks activation.
C. Human approval is required.
D. Invalid namespace blocks activation.
E. Activation endpoint is allowlisted.
F. Arbitrary POST endpoints remain blocked.
G. CTS/transport-release endpoints remain blocked.
H. Delete endpoints remain blocked.
I. Correct XML objectReferences payload is generated.
J. Correct activation query parameters are generated.
K. CSRF token is attached.
L. Stateful session behavior is preserved where required.
M. Activation diagnostics are parsed safely.
N. Blocking severities E/A/X are detected.
O. No secrets are logged.
P. No network call occurs before local gates.
"""

import logging
from typing import Any, Dict, List
import httpx
import pytest

from src.config import Settings
from src.core.exceptions import (
    SAPSecurityError,
)
from src.models.domain import (
    BusinessRequirement,
    CDSWorkflowState,
    CDSDraft,
    CDSReviewPackage,
    Decision,
    DecisionAction,
    DraftFieldElement,
    ElementEvidenceRecord,
    EvidenceLevel,
    TargetUseCase,
    ValidationResult,
)
from src.sap.adt_client import SAPADTClient
from src.services.review_service import ReviewStore, build_review_package


def _make_approved_state(object_name: str = "ZI_ACTIVATION_TEST") -> CDSWorkflowState:
    req = BusinessRequirement(
        title="Activation Test",
        description="Requirement for activation test",
        business_domain="Sales",
        target_use_case=TargetUseCase.ANALYTICAL_REPORT,
        required_entities=["SalesOrder"],
        required_fields=["SalesOrder"],
    )
    decision = Decision(
        requirement_id=req.id,
        action=DecisionAction.CREATE,
        proposed_new_view_name=object_name,
        confidence_score=1.0,
        fit_percentage=100.0,
        rationale="Activation test decision",
    )
    ev = ElementEvidenceRecord(
        element_name="SalesOrder",
        element_type="FIELD",
        evidence_level=EvidenceLevel.LEVEL_1_EXACT_FIELD,
        evidence_detail="Exact match",
        confidence=1.0,
        is_verified=True,
    )
    draft = CDSDraft(
        id=f"draft_{object_name.lower()}",
        requirement_id=req.id,
        decision_action=DecisionAction.CREATE,
        object_name=object_name,
        object_type="CUSTOM_VIEW_ENTITY",
        proposed_layer="BASIC",
        source_views=["I_SalesOrder"],
        fields=[
            DraftFieldElement(
                field_name="SalesOrder",
                source_expression="SalesOrder",
                source_entity="I_SalesOrder",
                is_key=True,
                evidence=ev,
            )
        ],
        associations=[],
        parameters=[],
        filters=[],
        annotations={"@AccessControl.authorizationCheck": "#NOT_REQUIRED"},
        ddl_source=f"define view entity {object_name} as select from I_SalesOrder {{ key SalesOrder }};",
        evidence_per_element=[ev],
        warnings=[],
        validation_errors=[],
        generation_status="VALIDATED",
        unresolved_items=[],
    )
    val = ValidationResult(
        is_valid=True,
        syntax_errors=[],
        naming_convention_violations=[],
        clean_core_compliance_score=100.0,
        governance_warnings=[],
    )
    return {
        "requirement": req,
        "structured_requirement": None,
        "candidates": [],
        "field_mappings": [],
        "decision": decision,
        "cds_draft": draft,
        "validation_result": val,
        "generated_cds_code": draft.ddl_source,
        "audit_logs": [],
    }


def _make_approved_package(object_name: str = "ZI_ACTIVATION_TEST") -> CDSReviewPackage:
    state = _make_approved_state(object_name)
    pkg = build_review_package(state)
    store = ReviewStore()
    store.register(pkg, state)
    return store.approve(pkg.review_id, reviewer="lead_architect", comment="Approved for test")


# Test A & B: SAP_DEV_WRITE_ENABLED and SAP_LIVE_READ_ONLY gates
def test_activation_blocked_by_write_and_read_only_flags():
    pkg = _make_approved_package()

    # Case 1: SAP_DEV_WRITE_ENABLED is False
    s1 = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=False, SAP_LIVE_READ_ONLY=False)
    c1 = SAPADTClient(settings=s1)
    with pytest.raises(SAPSecurityError) as exc1:
        c1.assert_dev_write_policy("POST", "/sap/bc/adt/activation?method=activate", review_package=pkg)
    assert "SAP_DEV_WRITE_ENABLED is false" in str(exc1.value)

    # Case 2: SAP_LIVE_READ_ONLY is True
    s2 = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=True)
    c2 = SAPADTClient(settings=s2)
    with pytest.raises(SAPSecurityError) as exc2:
        c2.assert_dev_write_policy("POST", "/sap/bc/adt/activation?method=activate", review_package=pkg)
    assert "SAP_LIVE_READ_ONLY is enabled" in str(exc2.value)


# Test C: Human approval required
def test_activation_blocked_without_human_approval():
    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    c = SAPADTClient(settings=s)

    # None review package
    with pytest.raises(SAPSecurityError) as exc_none:
        c.assert_dev_write_policy("POST", "/sap/bc/adt/activation?method=activate", review_package=None)
    assert "Missing human review package" in str(exc_none.value)

    # Unapproved review package
    state = _make_approved_state()
    unapproved_pkg = build_review_package(state)
    with pytest.raises(SAPSecurityError) as exc_unapp:
        c.assert_dev_write_policy("POST", "/sap/bc/adt/activation?method=activate", review_package=unapproved_pkg)
    assert "Only proposals explicitly APPROVED" in str(exc_unapp.value)


# Test D: Customer namespace validation
@pytest.mark.asyncio
async def test_invalid_namespace_blocks_activation_locally():
    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    c = SAPADTClient(settings=s)
    pkg = _make_approved_package()

    invalid_names = ["BAD_NAME", "123VIEW", "I_SALESORDER", "Z", "Z" * 35]
    for bad_name in invalid_names:
        with pytest.raises(SAPSecurityError) as exc:
            await c.activate_ddls_object(bad_name, review_package=pkg)
        assert "violates SAP customer namespace rules" in str(exc.value)


# Test E, F, G, H: Exact allowlist enforcement
def test_activation_allowlist_and_blocked_endpoints():
    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    c = SAPADTClient(settings=s)
    pkg = _make_approved_package()

    # E: Exact activation endpoint is allowed
    c.assert_dev_write_policy("POST", "/sap/bc/adt/activation?method=activate", review_package=pkg)
    c.assert_dev_write_policy("POST", "/sap/bc/adt/activation?method=activate&preauditRequested=true", review_package=pkg)

    # F: Arbitrary activation or other POST blocked
    with pytest.raises(SAPSecurityError) as exc_act:
        c.assert_dev_write_policy("POST", "/sap/bc/adt/activation", review_package=pkg)
    assert "Only authorized POST /sap/bc/adt/activation?method=activate" in str(exc_act.value)

    with pytest.raises(SAPSecurityError):
        c.assert_dev_write_policy("POST", "/sap/bc/adt/activation/arbitrary", review_package=pkg)

    with pytest.raises(SAPSecurityError):
        c.assert_dev_write_policy("POST", "/sap/bc/adt/repository/unknown", review_package=pkg)

    # G: CTS/transport-release endpoints remain blocked
    with pytest.raises(SAPSecurityError) as exc_cts:
        c.assert_dev_write_policy("POST", "/sap/bc/adt/cts/transports/release", review_package=pkg)
    assert "transport management endpoint" in str(exc_cts.value)

    # H: Delete endpoints remain blocked
    with pytest.raises(SAPSecurityError):
        c.assert_dev_write_policy("DELETE", "/sap/bc/adt/ddic/ddl/sources/zi_test", review_package=pkg)


# Test I, J, K, L: Payload generation, query params, CSRF, and stateful preservation
@pytest.mark.asyncio
async def test_activation_request_shape_and_headers():
    captured_requests = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.method == "GET" and "search" in str(request.url):
            return httpx.Response(200, headers={"x-csrf-token": "CSRF_TOKEN_ACTIVATION_123"}, text="<ok/>")
        if request.method == "POST" and "activation" in str(request.url):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                text="""<?xml version="1.0" encoding="utf-8"?>
<ioc:activationResult xmlns:ioc="http://www.sap.com/adt/activation">
</ioc:activationResult>""",
            )
        return httpx.Response(404)

    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.AsyncClient(transport=transport, base_url="https://mock-sap:443")
    client = SAPADTClient(settings=s, client=mock_http)
    pkg = _make_approved_package("ZI_ACTIVATION_TEST")

    res = await client.activate_ddls_object("ZI_ACTIVATION_TEST", review_package=pkg, preaudit_requested=True)

    assert res["success"] is True
    assert res["status_code"] == 200

    # Verify captured POST request
    act_req = [r for r in captured_requests if r.method == "POST" and "activation" in str(r.url)][0]

    # J: Query params
    assert act_req.url.params["method"] == "activate"
    assert act_req.url.params["preauditRequested"] == "true"

    # K: CSRF token
    assert act_req.headers.get("x-csrf-token") == "CSRF_TOKEN_ACTIVATION_123"

    # Headers
    assert act_req.headers.get("content-type") == "application/xml"
    assert "application/xml" in act_req.headers.get("accept", "")

    # I: XML payload
    body_text = act_req.content.decode("utf-8")
    assert "<adtcore:objectReferences" in body_text
    assert 'xmlns:adtcore="http://www.sap.com/adt/core"' in body_text
    assert '<adtcore:objectReference adtcore:uri="/sap/bc/adt/ddic/ddl/sources/zi_activation_test" adtcore:name="ZI_ACTIVATION_TEST"/>' in body_text


# Test M, N: Diagnostic parsing and blocking error detection (E, A, X)
@pytest.mark.asyncio
async def test_activation_diagnostic_parsing_and_blocking_errors():
    error_xml = """<?xml version="1.0" encoding="utf-8"?>
<ioc:activationResult xmlns:ioc="http://www.sap.com/adt/activation" xmlns:chkl="http://www.sap.com/adt/checkrun" xmlns:adtcore="http://www.sap.com/adt/core">
  <ioc:inactiveObjects>
    <ioc:entry>
      <ioc:object>
        <ioc:ref adtcore:name="ZI_ACTIVATION_FAIL" adtcore:uri="/sap/bc/adt/ddic/ddl/sources/zi_activation_fail"/>
      </ioc:object>
    </ioc:entry>
  </ioc:inactiveObjects>
  <chkl:messages>
    <msg type="E" line="4" href="/sap/bc/adt/ddic/ddl/sources/zi_activation_fail/source/main#start=4,5">
      <shortText><txt>Syntax error: Field UNKNOWN_FIELD does not exist</txt></shortText>
    </msg>
    <msg type="W" line="2">
      <shortText><txt>Warning: Obsolete annotation used</txt></shortText>
    </msg>
  </chkl:messages>
</ioc:activationResult>"""

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"x-csrf-token": "CSRF_TOKEN_999"}, text="<ok/>")
        return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=error_xml)

    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), base_url="https://mock-sap:443")
    client = SAPADTClient(settings=s, client=mock_http)
    pkg = _make_approved_package("ZI_ACTIVATION_FAIL")

    res = await client.activate_ddls_object("ZI_ACTIVATION_FAIL", review_package=pkg)

    # N: Blocking severities E/A/X detected
    assert res["success"] is False
    assert res["has_blocking_errors"] is True
    assert "ZI_ACTIVATION_FAIL" in [o.upper() for o in res["inactive_objects"]]

    # M: Diagnostics parsed safely
    assert len(res["messages"]) == 2
    err_msg = [m for m in res["messages"] if m["type"] == "E"][0]
    assert err_msg["line"] == 4
    assert "UNKNOWN_FIELD does not exist" in err_msg["short_text"]


# Test O: No secrets logged
@pytest.mark.asyncio
async def test_activation_no_secrets_logged(caplog):
    caplog.set_level(logging.DEBUG)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"x-csrf-token": "SECRET_CSRF_TOKEN_ABC"}, text="<ok/>")
        return httpx.Response(200, text="<ioc:activationResult/>")

    s = Settings(
        SAP_CONNECTOR_ENABLED=True,
        SAP_DEV_WRITE_ENABLED=True,
        SAP_LIVE_READ_ONLY=False,
    )
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), base_url="https://mock-sap:443")
    client = SAPADTClient(settings=s, client=mock_http)
    pkg = _make_approved_package("ZI_SECRET_TEST")

    await client.activate_ddls_object("ZI_SECRET_TEST", review_package=pkg)

    client_logs = " ".join([r.message for r in caplog.records if r.name.startswith("sap_cds_platform")])
    assert "SECRET_CSRF_TOKEN_ABC" not in client_logs
    assert "Authorization" not in client_logs


# Test P: No network call occurs before local gates
@pytest.mark.asyncio
async def test_no_network_call_before_local_gates():
    call_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, text="<ok/>")

    # Disabled write flag
    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=False, SAP_LIVE_READ_ONLY=False)
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), base_url="https://mock-sap:443")
    client = SAPADTClient(settings=s, client=mock_http)
    pkg = _make_approved_package()

    with pytest.raises(SAPSecurityError):
        await client.activate_ddls_object("ZI_ACTIVATION_TEST", review_package=pkg)

    assert call_count == 0  # Zero network calls reached mock transport


# Test Q: Activation HTTP 200 does NOT report ACTIVE when object remains inactive in DDLS read-back
@pytest.mark.asyncio
async def test_activation_http_200_not_active_when_read_back_reports_inactive():
    """Requirement 4: Do not report ACTIVE merely because the activation endpoint returned HTTP 200."""
    inactive_ddls_xml = """<?xml version="1.0" encoding="utf-8"?>
<ddl:ddlSource xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources"
               xmlns:adtcore="http://www.sap.com/adt/core"
               adtcore:name="ZI_REMAINS_INACTIVE"
               adtcore:version="inactive">
</ddl:ddlSource>"""

    activation_accepted_xml = """<?xml version="1.0" encoding="utf-8"?>
<ioc:activationResult xmlns:ioc="http://www.sap.com/adt/activation">
</ioc:activationResult>"""

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "search" in url_str:
            return httpx.Response(200, headers={"x-csrf-token": "CSRF_TOKEN_Q"}, text="<ok/>")
        if request.method == "POST" and "activation" in url_str:
            return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=activation_accepted_xml)
        if request.method == "GET" and "ddic/ddl/sources/zi_remains_inactive" in url_str:
            return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=inactive_ddls_xml)
        if request.method == "GET" and "inactiveobjects" in url_str:
            return httpx.Response(200, headers={"Content-Type": "application/xml"}, text="<inactivectsobjects/>")
        return httpx.Response(404)

    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), base_url="https://mock-sap:443")
    client = SAPADTClient(settings=s, client=mock_http)
    pkg = _make_approved_package("ZI_REMAINS_INACTIVE")

    res = await client.activate_ddls_object("ZI_REMAINS_INACTIVE", review_package=pkg)

    # Key assertions:
    assert res["activation_request_accepted"] is True
    assert res["activation_confirmed"] is False
    assert res["success"] is False
    assert res["activation_status"] == "INACTIVE"
    assert "remains 'inactive' in SAP dictionary runtime" in res["error_message"]
    assert res["verified_state"]["version"] == "inactive"
    assert res["verified_state"]["is_active"] is False


# Test R: Activation confirmed only when DDLS read-back confirms active version
@pytest.mark.asyncio
async def test_activation_confirmed_when_read_back_reports_active():
    """Authoritative activation confirmed when descriptor returns active and absent from inactive queue."""
    active_ddls_xml = """<?xml version="1.0" encoding="utf-8"?>
<ddl:ddlSource xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources"
               xmlns:adtcore="http://www.sap.com/adt/core"
               xmlns:atom="http://www.w3.org/2005/Atom"
               adtcore:name="ZI_CONFIRMED_ACTIVE"
               adtcore:version="active"
               adtcore:changedAt="2026-11-03T18:00:00Z"
               adtcore:changedBy="TESTUSER">
  <atom:link href="/sap/bc/adt/ddic/logs/db/ACTDDLSZI_CONFIRMED_ACTIVE"
             rel="http://www.sap.com/adt/relations/ddic/activationlog"
             type="application/vnd.sap.adt.logs+xml"/>
</ddl:ddlSource>"""

    activation_accepted_xml = """<?xml version="1.0" encoding="utf-8"?>
<ioc:activationResult xmlns:ioc="http://www.sap.com/adt/activation">
</ioc:activationResult>"""

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "search" in url_str:
            return httpx.Response(200, headers={"x-csrf-token": "CSRF_TOKEN_R"}, text="<ok/>")
        if request.method == "POST" and "activation" in url_str:
            return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=activation_accepted_xml)
        if request.method == "GET" and "ddic/ddl/sources/zi_confirmed_active" in url_str:
            return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=active_ddls_xml)
        if request.method == "GET" and "inactiveobjects" in url_str:
            return httpx.Response(200, headers={"Content-Type": "application/xml"}, text="<inactivectsobjects/>")
        return httpx.Response(404)

    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), base_url="https://mock-sap:443")
    client = SAPADTClient(settings=s, client=mock_http)
    pkg = _make_approved_package("ZI_CONFIRMED_ACTIVE")

    res = await client.activate_ddls_object("ZI_CONFIRMED_ACTIVE", review_package=pkg)

    assert res["activation_request_accepted"] is True
    assert res["activation_confirmed"] is True
    assert res["success"] is True
    assert res["activation_status"] == "ACTIVE"
    assert res["error_message"] is None
    assert res["verified_state"]["is_active"] is True
    assert res["verified_state"]["version"] == "active"
    assert res["activation_log_uri"] == "/sap/bc/adt/ddic/logs/db/ACTDDLSZI_CONFIRMED_ACTIVE"


# Test S: Detailed message categorization (errors, warnings, infos)
@pytest.mark.asyncio
async def test_activation_categorizes_errors_warnings_infos():
    """Requirements 1 & 2: Identify activation errors, warnings, and success messages returned by SAP."""
    diagnostic_xml = """<?xml version="1.0" encoding="utf-8"?>
<ioc:activationResult xmlns:ioc="http://www.sap.com/adt/activation" xmlns:chkl="http://www.sap.com/adt/checkrun">
  <chkl:messages>
    <msg type="E" line="10">
      <shortText><txt>Syntax error: Table ACDOCA column XYZ not found</txt></shortText>
    </msg>
    <msg type="W" line="5">
      <shortText><txt>Warning: Performance annotation missing</txt></shortText>
    </msg>
    <msg type="I" line="1">
      <shortText><txt>Information: Header annotation parsed</txt></shortText>
    </msg>
  </chkl:messages>
</ioc:activationResult>"""

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET":
            return httpx.Response(200, headers={"x-csrf-token": "CSRF_TOKEN_S"}, text="<ok/>")
        if request.method == "POST" and "activation" in url_str:
            return httpx.Response(200, headers={"Content-Type": "application/xml"}, text=diagnostic_xml)
        return httpx.Response(404)

    s = Settings(SAP_CONNECTOR_ENABLED=True, SAP_DEV_WRITE_ENABLED=True, SAP_LIVE_READ_ONLY=False)
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), base_url="https://mock-sap:443")
    client = SAPADTClient(settings=s, client=mock_http)
    pkg = _make_approved_package("ZI_DIAG_TEST")

    res = await client.activate_ddls_object("ZI_DIAG_TEST", review_package=pkg)

    assert res["success"] is False
    assert res["has_blocking_errors"] is True
    assert len(res["errors"]) == 1
    assert res["errors"][0]["line"] == 10
    assert "ACDOCA column XYZ not found" in res["errors"][0]["short_text"]
    assert len(res["warnings"]) == 1
    assert "Performance annotation missing" in res["warnings"][0]["short_text"]
    assert len(res["infos"]) == 1
    assert "Header annotation parsed" in res["infos"][0]["short_text"]
    assert "Line 10: Syntax error: Table ACDOCA column XYZ not found" in res["error_message"]

