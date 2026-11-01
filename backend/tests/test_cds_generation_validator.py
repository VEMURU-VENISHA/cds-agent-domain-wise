"""Comprehensive Generic Test Suite for CDS DDL Generation Validator.

Tests all required validation rules generically across multiple business domains
(Sales, Procurement, Financials, Inventory) to ensure zero metadata hallucination,
zero semantic substitution, 100% required field coverage, and strict human approval gating.
"""

import pytest
from src.services.cds_generation_validator import CDSGenerationValidator, CDSDDLParser, validate_source_completeness



def test_A_valid_single_cds():
    """Test A: Valid single-CDS generation passes with 100% coverage."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Purchase Order View'
define view entity ZC_PURCHASEORDER
  as select from I_PurchaseOrderAPI01
{
  key PurchaseOrder,
  CompanyCode,
  Supplier,
  CreatedByUser
}"""
    context = {
        "artifact_name": "ZC_PURCHASEORDER",
        "selected_views": ["I_PurchaseOrderAPI01"],
        "mappings": [
            {"business_field": "Purchase Order", "actual_field": "PurchaseOrder", "selected_cds": "I_PurchaseOrderAPI01"},
            {"business_field": "Company Code", "actual_field": "CompanyCode", "selected_cds": "I_PurchaseOrderAPI01"},
            {"business_field": "Supplier", "actual_field": "Supplier", "selected_cds": "I_PurchaseOrderAPI01"},
            {"business_field": "Created By", "actual_field": "CreatedByUser", "selected_cds": "I_PurchaseOrderAPI01"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is True
    assert res["status"] == "GENERATION_VALIDATED"
    assert res["coverage"] == 100.0
    assert len(res["errors"]) == 0


def test_B_valid_multi_cds():
    """Test B: Valid multi-CDS generation with 3 views passes validation."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Multi CDS Invoice Analysis'
define view entity ZC_INVOICEANALYSIS
  as select from I_RO_SAFTSALESINVOICECUSTOMER as _Saft
  association [1..1] to I_CUSTOMER as _Cust
    on $projection.Customer = _Cust.Customer
  association [0..*] to I_JOURNALENTRYITEM as _Item
    on $projection.Customer = _Item.Customer
{
  key _Saft.SalesInvoice as SalesInvoice,
  _Saft.Customer as Customer,
  _Cust.CustomerName as CustomerName,
  _Item.AmountInCompanyCodeCurrency as TotalAmount
}"""
    context = {
        "artifact_name": "ZC_INVOICEANALYSIS",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER", "I_JOURNALENTRYITEM"],
        "mappings": [
            {"business_field": "Sales Invoice", "actual_field": "SalesInvoice", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "Customer Name", "actual_field": "CustomerName", "selected_cds": "I_CUSTOMER"},
            {"business_field": "Total Amount", "actual_field": "AmountInCompanyCodeCurrency", "selected_cds": "I_JOURNALENTRYITEM"},
        ],
        "join_paths": [
            {
                "source_cds": "I_RO_SAFTSALESINVOICECUSTOMER",
                "source_field": "Customer",
                "target_cds": "I_CUSTOMER",
                "target_field": "Customer",
                "cardinality": "1..1",
            },
            {
                "source_cds": "I_RO_SAFTSALESINVOICECUSTOMER",
                "source_field": "Customer",
                "target_cds": "I_JOURNALENTRYITEM",
                "target_field": "Customer",
                "cardinality": "0..*",
            },
        ],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is True
    assert res["status"] == "GENERATION_VALIDATED"
    assert res["coverage"] == 100.0


def test_C_failing_example_unverified_association():
    """Test C: Specifically tests the failing example from prompt.
    Generated DDL has association [1..*] to I_CUSTOMER as _Item on $projection.Customer = _Item.Customer,
    when verified cardinality is 1..1 or relationship is unverified.
    Must be blocked!
    """
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Sales Order Analysis'
define view entity ZC_SALESORDERANALYSIS
  as select from I_RO_SAFTSALESINVOICECUSTOMER
  association [1..*] to I_CUSTOMER as _Item
    on $projection.Customer = _Item.Customer
{
  key SalesInvoice,
  Customer
}"""
    context = {
        "artifact_name": "ZC_SALESORDERANALYSIS",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER"],
        "mappings": [
            {"business_field": "Sales Invoice", "actual_field": "SalesInvoice", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
        ],
        "join_paths": [
            {
                "source_cds": "I_RO_SAFTSALESINVOICECUSTOMER",
                "source_field": "Customer",
                "target_cds": "I_CUSTOMER",
                "target_field": "Customer",
                "cardinality": "1..1",
            }
        ],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert res["status"] == "GENERATION_VALIDATION_FAILED"
    assert any(e.get("type") == "WRONG_CARDINALITY" or "cardinality" in str(e).lower() for e in res["structured_errors"])


def test_D_invented_on_condition_rejected():
    """Test D: Invented ON condition not present in verified_join_paths is rejected."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Invoice View'
define view entity ZC_INVOICE
  as select from I_BillingDocument as _Inv
  left outer join I_Customer as _Cust
    on _Inv.CreatedByUser = _Cust.CreatedByUser
{
  key _Inv.BillingDocument,
  _Cust.Customer
}"""
    context = {
        "artifact_name": "ZC_INVOICE",
        "selected_views": ["I_BillingDocument", "I_Customer"],
        "mappings": [
            {"business_field": "Billing Document", "actual_field": "BillingDocument", "selected_cds": "I_BillingDocument"},
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_Customer"},
        ],
        "join_paths": [
            {
                "source_cds": "I_BillingDocument",
                "source_field": "PayerParty",
                "target_cds": "I_Customer",
                "target_field": "Customer",
            }
        ],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "UNVERIFIED_JOIN" for e in res["structured_errors"])


def test_E_wrong_cardinality_rejected():
    """Test E: Mismatch in join/association cardinality is rejected."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Test View'
define view entity ZC_CARD
  as select from I_SalesOrder as _Header
  association [1..*] to I_Customer as _Customer
    on $projection.SoldToParty = _Customer.Customer
{
  key _Header.SalesOrder,
  _Header.SoldToParty
}"""
    context = {
        "artifact_name": "ZC_CARD",
        "selected_views": ["I_SalesOrder", "I_Customer"],
        "mappings": [
            {"business_field": "Sales Order", "actual_field": "SalesOrder", "selected_cds": "I_SalesOrder"},
            {"business_field": "Sold To Party", "actual_field": "SoldToParty", "selected_cds": "I_SalesOrder"},
        ],
        "join_paths": [
            {
                "source_cds": "I_SalesOrder",
                "source_field": "SoldToParty",
                "target_cds": "I_Customer",
                "target_field": "Customer",
                "cardinality": "1..1",
            }
        ],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "WRONG_CARDINALITY" for e in res["structured_errors"])


def test_F_missing_selected_cds_rejected():
    """Test F: Omission of required source CDS view is rejected."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Single Only'
define view entity ZC_MULTI
  as select from I_RO_SAFTSALESINVOICECUSTOMER
{
  key SalesInvoice,
  Customer
}"""
    context = {
        "artifact_name": "ZC_MULTI",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_JOURNALENTRYITEM"],
        "mappings": [
            {"business_field": "Sales Invoice", "actual_field": "SalesInvoice", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "Amount", "actual_field": "AmountInCompanyCodeCurrency", "selected_cds": "I_JOURNALENTRYITEM"},
        ],
        "join_paths": [
            {
                "source_cds": "I_RO_SAFTSALESINVOICECUSTOMER",
                "source_field": "Customer",
                "target_cds": "I_JOURNALENTRYITEM",
                "target_field": "Customer",
            }
        ],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") in ("MISSING_SELECTED_SOURCE", "FIELD_SOURCE_MISMATCH") for e in res["structured_errors"])


def test_G_extra_unverified_cds_rejected():
    """Test G: Referencing an unselected CDS view is rejected."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Extra View'
define view entity ZC_EXTRA
  as select from I_SalesOrder
  left outer join I_UNSELECTED_TABLE as _Unsel
    on I_SalesOrder.SalesOrder = _Unsel.SalesOrder
{
  key I_SalesOrder.SalesOrder
}"""
    context = {
        "artifact_name": "ZC_EXTRA",
        "selected_views": ["I_SalesOrder"],
        "mappings": [
            {"business_field": "Sales Order", "actual_field": "SalesOrder", "selected_cds": "I_SalesOrder"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "UNVERIFIED_CDS" for e in res["structured_errors"])


def test_H_field_source_mismatch_rejected():
    """Test H: Mapping specifies field from I_JOURNALENTRYITEM but DDL lacks I_JOURNALENTRYITEM."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Saft Invoice'
define view entity ZC_SAFTITEM
  as select from I_RO_SAFTSALESINVOICECUSTOMER
{
  key SalesInvoice,
  Customer
}"""
    context = {
        "artifact_name": "ZC_SAFTITEM",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER"],
        "mappings": [
            {"business_field": "Sales Invoice", "actual_field": "SalesInvoice", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "Net Amount", "actual_field": "AmountInBalanceTransacCrcy", "selected_cds": "I_JOURNALENTRYITEM"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "FIELD_SOURCE_MISMATCH" for e in res["structured_errors"])


def test_I_semantic_substitution_rejected():
    """Test I: Replacing Material with Product is rejected."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Inventory'
define view entity ZC_INVENTORY
  as select from I_MaterialStock
{
  key Product as Material,
  Plant
}"""
    context = {
        "artifact_name": "ZC_INVENTORY",
        "selected_views": ["I_MaterialStock"],
        "mappings": [
            {"business_field": "Material", "actual_field": "Material", "selected_cds": "I_MaterialStock"},
            {"business_field": "Plant", "actual_field": "Plant", "selected_cds": "I_MaterialStock"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "SEMANTIC_SUBSTITUTION" for e in res["structured_errors"])


def test_J_missing_required_field_rejected():
    """Test J: Required mapping omitted from DDL select list."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Missing Field'
define view entity ZC_MISSING
  as select from I_JournalEntryItem
{
  key AccountingDocument,
  CompanyCode
}"""
    context = {
        "artifact_name": "ZC_MISSING",
        "selected_views": ["I_JournalEntryItem"],
        "mappings": [
            {"business_field": "Accounting Document", "actual_field": "AccountingDocument", "selected_cds": "I_JournalEntryItem"},
            {"business_field": "Company Code", "actual_field": "CompanyCode", "selected_cds": "I_JournalEntryItem"},
            {"business_field": "Fiscal Year", "actual_field": "FiscalYear", "selected_cds": "I_JournalEntryItem"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "MISSING_REQUIRED_FIELD" for e in res["structured_errors"])


def test_L_invented_field_rejected():
    """Test L: Referencing an unverified field in select list."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Supplier'
define view entity ZC_SUPPLIER
  as select from I_Supplier
{
  key Supplier,
  SupplierName,
  UnverifiedColumn
}"""
    context = {
        "artifact_name": "ZC_SUPPLIER",
        "selected_views": ["I_Supplier"],
        "mappings": [
            {"business_field": "Supplier", "actual_field": "Supplier", "selected_cds": "I_Supplier"},
            {"business_field": "Supplier Name", "actual_field": "SupplierName", "selected_cds": "I_Supplier"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "UNVERIFIED_FIELD" for e in res["structured_errors"])


def test_N_invalid_kpi_rejected():
    """Test N: KPI refers to target field missing from DDL."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'KPI View'
define view entity ZC_KPI
  as select from I_SalesOrder
{
  key SalesOrder
}"""
    context = {
        "artifact_name": "ZC_KPI",
        "selected_views": ["I_SalesOrder"],
        "mappings": [
            {"business_field": "Sales Order", "actual_field": "SalesOrder", "selected_cds": "I_SalesOrder"},
        ],
        "join_paths": [],
        "kpi": {"enabled": True, "field": "NetAmount", "formula": "SUM(NetAmount)"},
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "INVALID_KPI" for e in res["structured_errors"])


def test_O_projection_join_without_verified_relationship():
    """Test O: $projection ON condition without verified relationship graph entry must fail."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Guessing Join'
define view entity ZC_GUESS
  as select from I_SalesOrder as _Header
  association [1..1] to I_Plant as _Plant
    on $projection.SalesOrganization = _Plant.Plant
{
  key _Header.SalesOrder,
  _Header.SalesOrganization
}"""
    context = {
        "artifact_name": "ZC_GUESS",
        "selected_views": ["I_SalesOrder", "I_Plant"],
        "mappings": [
            {"business_field": "Sales Order", "actual_field": "SalesOrder", "selected_cds": "I_SalesOrder"},
            {"business_field": "Sales Org", "actual_field": "SalesOrganization", "selected_cds": "I_SalesOrder"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "UNVERIFIED_JOIN" for e in res["structured_errors"])


def test_P_case_insensitive_duplicate_handling():
    """Test P: Duplicate view entries with case variation are canonicalized."""
    ddl = """@AccessControl.authorizationCheck: #NOT_REQUIRED
@EndUserText.label: 'Case Test'
define view entity ZC_CASE
  as select from I_CUSTOMER
{
  key Customer,
  CustomerName
}"""
    context = {
        "artifact_name": "ZC_CASE",
        "selected_views": ["I_Customer", "I_CUSTOMER", "i_customer"],
        "mappings": [
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_CUSTOMER"},
            {"business_field": "Customer Name", "actual_field": "CustomerName", "selected_cds": "I_CUSTOMER"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is True
    assert res["coverage"] == 100.0


def test_U_annotation_policy_validation():
    """Test U: Missing mandatory annotations is rejected."""
    ddl = """define view entity ZC_NOANN
  as select from I_Customer
{
  key Customer
}"""
    context = {
        "artifact_name": "ZC_NOANN",
        "selected_views": ["I_Customer"],
        "mappings": [
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_Customer"},
        ],
        "join_paths": [],
    }
    res = CDSGenerationValidator.validate_generation(ddl, context)
    assert res["valid"] is False
    assert any(e.get("type") == "INVALID_ANNOTATION" for e in res["structured_errors"])


def test_step9_rejects_ddl_missing_selected_source():
    generated_ddl = "@AccessControl.authorizationCheck: #NOT_REQUIRED\ndefine view entity ZC_SalesOrderAnalysis\n  as select from I_RO_SAFTSALESINVOICECUSTOMER\n  association [0..1] to I_CUSTOMER\n    on $projection.Customer = _CUSTOMER.Customer {}"
    selected_views = ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER", "I_JOURNALENTRYITEM"]
    result = validate_source_completeness(generated_ddl, selected_views)
    assert result["status"] == "GENERATION_VALIDATION_FAILED"
    assert "I_JOURNALENTRYITEM" in result["missing_sources"]
    assert result["approval_enabled"] is False


def test_step9_passes_when_all_sources_present():
    generated_ddl = "@AccessControl.authorizationCheck: #NOT_REQUIRED\ndefine view entity ZC_SalesOrderAnalysis\n  as select from I_RO_SAFTSALESINVOICECUSTOMER\n  association [0..1] to I_CUSTOMER\n    on $projection.Customer = _CUSTOMER.Customer\n  association [0..1] to I_JOURNALENTRYITEM\n    on $projection.CompanyCode = _JOURNALENTRYITEM.CompanyCode {}"
    selected_views = ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER", "I_JOURNALENTRYITEM"]
    result = validate_source_completeness(generated_ddl, selected_views)
    assert result["status"] == "GENERATION_VALIDATED"
    assert result["approval_enabled"] is True


def test_step9_false_positive_impossible():
    generated_ddl = "define view entity ZC_SalesOrderAnalysis\n  as select from I_RO_SAFTSALESINVOICECUSTOMER\n  association [0..1] to I_CUSTOMER {}"
    selected_views = ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER", "I_JOURNALENTRYITEM"]
    result = validate_source_completeness(generated_ddl, selected_views)
    assert result["status"] == "GENERATION_VALIDATION_FAILED"
    assert result["approval_enabled"] is False


def test_unverified_join_rejection_with_misleading_alias():
    """Verifies that unverified joins and misleading association aliases are rejected."""
    generated_ddl = (
        "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
        "@EndUserText.label: 'ZC_SalesOrderAnalysis'\n"
        "define view entity ZC_SalesOrderAnalysis\n"
        "  as select from I_RO_SAFTSALESINVOICECUSTOMER as _RO_SAFTSALESINVOICECUSTOMER\n"
        "  association [1..*] to I_JOURNALENTRYITEM as _CustomerText_Of\n"
        "    on _RO_SAFTSALESINVOICECUSTOMER.CUSTOMER = _CustomerText_Of.Customer {\n"
        "  _RO_SAFTSALESINVOICECUSTOMER.SalesOrder\n"
        "}\n"
    )
    verified_context = {
        "artifact_name": "ZC_SALESORDERANALYSIS",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_JOURNALENTRYITEM"],
        "mappings": [{"business_field": "SalesOrder", "actual_field": "SalesOrder", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"}],
        "join_paths": [],  # No verified join path exists between these entities
    }
    result = CDSGenerationValidator.validate_generation(generated_ddl, verified_context)
    assert result["valid"] is False
    assert result["status"] == "GENERATION_VALIDATION_FAILED"
    assert any(err.get("type") == "UNVERIFIED_JOIN" for err in result["structured_errors"])


def test_source_path_unavailable_error_when_no_join_path():
    """Verifies that missing direct join path produces SOURCE_PATH_UNAVAILABLE warning (non-blocking)."""
    generated_ddl = (
        "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
        "@EndUserText.label: 'ZC_SalesOrderAnalysis'\n"
        "define view entity ZC_SalesOrderAnalysis\n"
        "  as select from I_RO_SAFTSALESINVOICECUSTOMER as _RO\n"
        "  association [0..1] to I_CUSTOMER as _CUSTOMER\n"
        "    on $projection.Customer = _CUSTOMER.Customer {\n"
        "  key _RO.SalesOrder as SalesOrder,\n"
        "  _CUSTOMER.Customer as Customer,\n"
        "  _CUSTOMER._JournalEntryItem.PostingDate as PostingDate\n"
        "}\n"
    )
    verified_context = {
        "artifact_name": "ZC_SALESORDERANALYSIS",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER", "I_JOURNALENTRYITEM"],
        "mappings": [
            {"business_field": "SalesOrder", "actual_field": "SalesOrder", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_CUSTOMER"},
            {"business_field": "PostingDate", "actual_field": "PostingDate", "selected_cds": "I_JOURNALENTRYITEM"},
        ],
        "join_paths": [
            {"source_cds": "I_RO_SAFTSALESINVOICECUSTOMER", "target_cds": "I_CUSTOMER", "source_field": "Customer", "target_field": "Customer", "cardinality": "0..1"}
        ],
    }
    result = CDSGenerationValidator.validate_generation(generated_ddl, verified_context)
    assert result["valid"] is True
    assert result["approval_enabled"] is True
    assert any("SOURCE_PATH_UNAVAILABLE" in warn for warn in result.get("warnings", []))


def test_unconnected_view_not_generated_and_source_path_unavailable():
    """Selected: I_RO_SAFTSALESINVOICECUSTOMER, I_CUSTOMER, I_JOURNALENTRYITEM.
    No verified join_path for I_JOURNALENTRYITEM.
    Expected: I_JOURNALENTRYITEM must NOT appear as direct association.
    Validation emits SOURCE_PATH_UNAVAILABLE warning if its fields are required.
    """
    # DDL generated correctly omitting unconnected I_JOURNALENTRYITEM
    generated_ddl = (
        "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
        "@EndUserText.label: 'ZC_SalesOrderAnalysis'\n"
        "define view entity ZC_SalesOrderAnalysis\n"
        "  as select from I_RO_SAFTSALESINVOICECUSTOMER as _ROSAFTSALESINVOICECUSTOMER\n"
        "  association [0..1] to I_CUSTOMER as _CUSTOMER\n"
        "    on $projection.Customer = _CUSTOMER.Customer {\n"
        "  key _ROSAFTSALESINVOICECUSTOMER.Customer as Customer,\n"
        "  _ROSAFTSALESINVOICECUSTOMER.SalesOrder as SalesOrder\n"
        "}\n"
    )

    # Subcase A: Mapped fields require PostingDate from I_JOURNALENTRYITEM -> FAILS with MISSING_REQUIRED_FIELD, warning for SOURCE_PATH_UNAVAILABLE
    verified_context_failing = {
        "artifact_name": "ZC_SALESORDERANALYSIS",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER", "I_JOURNALENTRYITEM"],
        "mappings": [
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "SalesOrder", "actual_field": "SalesOrder", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "PostingDate", "actual_field": "PostingDate", "selected_cds": "I_JOURNALENTRYITEM"},
        ],
        "join_paths": [
            {"source_cds": "I_RO_SAFTSALESINVOICECUSTOMER", "target_cds": "I_CUSTOMER", "source_field": "Customer", "target_field": "Customer", "cardinality": "0..1"}
        ],
    }
    res_fail = CDSGenerationValidator.validate_generation(generated_ddl, verified_context_failing)
    assert res_fail["valid"] is False
    assert res_fail["status"] == "GENERATION_VALIDATION_FAILED"
    assert any("MISSING_REQUIRED_FIELD" in err for err in res_fail["errors"])
    assert any("SOURCE_PATH_UNAVAILABLE" in warn for warn in res_fail.get("warnings", []))

    # Subcase B: Mapped fields require ONLY I_RO_SAFTSALESINVOICECUSTOMER & I_CUSTOMER -> MUST PASS
    verified_context_passing = {
        "artifact_name": "ZC_SALESORDERANALYSIS",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER", "I_JOURNALENTRYITEM"],
        "mappings": [
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "SalesOrder", "actual_field": "SalesOrder", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
        ],
        "join_paths": [
            {"source_cds": "I_RO_SAFTSALESINVOICECUSTOMER", "target_cds": "I_CUSTOMER", "source_field": "Customer", "target_field": "Customer", "cardinality": "0..1"}
        ],
    }
    res_pass = CDSGenerationValidator.validate_generation(generated_ddl, verified_context_passing)
    assert res_pass["valid"] is True
    assert res_pass["status"] == "GENERATION_VALIDATED"
    assert res_pass["approval_enabled"] is True


def test_full_selected_cds_metadata_generation_flow():
    """Proves that passing full selected CDS metadata context produces grounded DDL using exact mapped fields and verified join paths."""
    full_metadata = {
        "I_RO_SAFTSALESINVOICECUSTOMER": {
            "cds_name": "I_RO_SAFTSALESINVOICECUSTOMER",
            "description": "SAFT Sales Invoice Customer View",
            "fields": [
                {"fieldname": "Customer", "field_label": "Customer", "data_type": "CHAR", "is_key": True},
                {"fieldname": "SalesOrder", "field_label": "Sales Order", "data_type": "CHAR", "is_key": True},
                {"fieldname": "CompanyCode", "field_label": "Company Code", "data_type": "CHAR", "is_key": False},
            ],
            "keys": ["Customer", "SalesOrder"],
        },
        "I_CUSTOMER": {
            "cds_name": "I_CUSTOMER",
            "description": "Customer Master View",
            "fields": [
                {"fieldname": "Customer", "field_label": "Customer Number", "data_type": "CHAR", "is_key": True},
                {"fieldname": "CustomerName", "field_label": "Customer Name", "data_type": "CHAR", "is_key": False},
            ],
            "keys": ["Customer"],
        }
    }
    
    generated_ddl = (
        "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
        "@EndUserText.label: 'ZC_SalesOrderAnalysis'\n"
        "define view entity ZC_SalesOrderAnalysis\n"
        "  as select from I_RO_SAFTSALESINVOICECUSTOMER as _ROSAFTSALESINVOICECUSTOMER\n"
        "  association [0..1] to I_CUSTOMER as _CUSTOMER\n"
        "    on $projection.Customer = _CUSTOMER.Customer {\n"
        "  key _ROSAFTSALESINVOICECUSTOMER.Customer as Customer,\n"
        "  _ROSAFTSALESINVOICECUSTOMER.SalesOrder as SalesOrder,\n"
        "  _CUSTOMER.CustomerName as CustomerName\n"
        "}\n"
    )
    
    verified_context = {
        "artifact_name": "ZC_SALESORDERANALYSIS",
        "selected_views": ["I_RO_SAFTSALESINVOICECUSTOMER", "I_CUSTOMER"],
        "selected_views_full_metadata": full_metadata,
        "mappings": [
            {"business_field": "Customer", "actual_field": "Customer", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "SalesOrder", "actual_field": "SalesOrder", "selected_cds": "I_RO_SAFTSALESINVOICECUSTOMER"},
            {"business_field": "CustomerName", "actual_field": "CustomerName", "selected_cds": "I_CUSTOMER"},
        ],
        "join_paths": [
            {"source_cds": "I_RO_SAFTSALESINVOICECUSTOMER", "target_cds": "I_CUSTOMER", "source_field": "Customer", "target_field": "Customer", "cardinality": "0..1"}
        ],
    }
    
    result = CDSGenerationValidator.validate_generation(generated_ddl, verified_context)
    assert result["valid"] is True
    assert result["status"] == "GENERATION_VALIDATED"
    assert result["approval_enabled"] is True
    assert len(result["errors"]) == 0




