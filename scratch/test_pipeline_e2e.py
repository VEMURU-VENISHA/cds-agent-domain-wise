import asyncio
import sys
import json
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

from src.services.table_discovery_service import TableDiscoveryService

async def test_e2e():
    svc = TableDiscoveryService()
    
    print("Testing MATDOC discovery...")
    res_matdoc = await svc.run_discovery(tables_input=["MATDOC"], force_refresh_ext=False)
    recs_matdoc = res_matdoc.get("recommendations", [])
    print(f"MATDOC recommendations count: {len(recs_matdoc)}")
    found_pfm = None
    found_gmd = None
    for r in recs_matdoc:
        name = r.get("cds_name", "").upper()
        print(f"  - {r.get('cds_name')} | {r.get('dependency_classification')} (D{r.get('dependency_depth')}) | FROM: {r.get('direct_from_source')} | Path: {r.get('complete_dependency_path')}")
        if name == "C_PFMGOODSMOVEMENTDOCUMENTDEX":
            found_pfm = r
        elif name == "I_GOODSMOVEMENTDOCUMENTDEX":
            found_gmd = r
            
    print(f"\nVerification Results for MATDOC:")
    print(f"C_PFMGoodsMovementDocumentDEX Found: {found_pfm is not None}")
    if found_pfm:
        print(f"  Classification: {found_pfm.get('dependency_classification')}")
        print(f"  Depth: {found_pfm.get('dependency_depth')}")
        print(f"  Direct FROM: {found_pfm.get('direct_from_source')}")
        print(f"  Complete Path: {found_pfm.get('complete_dependency_path')}")
        print(f"  Extraction: {found_pfm.get('extraction_status')}")

    print(f"I_GoodsMovementDocumentDEX Found: {found_gmd is not None}")
    if found_gmd:
        print(f"  Classification: {found_gmd.get('dependency_classification')}")
        print(f"  Depth: {found_gmd.get('dependency_depth')}")
        print(f"  Direct FROM: {found_gmd.get('direct_from_source')}")
        print(f"  Complete Path: {found_gmd.get('complete_dependency_path')}")
        print(f"  CDC: {found_gmd.get('cdc_status')}")

    print("\nTesting ACDOCA discovery...")
    res_acdoca = await svc.run_discovery(tables_input=["ACDOCA"], force_refresh_ext=False)
    recs_acdoca = res_acdoca.get("recommendations", [])
    print(f"ACDOCA recommendations count: {len(recs_acdoca)}")
    found_finstmt = None
    for r in recs_acdoca:
        name = r.get("cds_name", "").upper()
        print(f"  - {r.get('cds_name')} | {r.get('dependency_classification')} (D{r.get('dependency_depth')}) | FROM: {r.get('direct_from_source')} | Path: {r.get('complete_dependency_path')}")
        if name == "I_FINANCIALSTATEMENTCUBE":
            found_finstmt = r
            
    print(f"\nVerification Results for ACDOCA:")
    print(f"I_FinancialStatementCube Found: {found_finstmt is not None}")
    if found_finstmt:
        print(f"  Classification: {found_finstmt.get('dependency_classification')}")
        print(f"  Depth: {found_finstmt.get('dependency_depth')}")
        print(f"  Direct FROM: {found_finstmt.get('direct_from_source')}")
        print(f"  SQL View: {found_finstmt.get('sql_view_name')}")
        print(f"  Complete Path: {found_finstmt.get('complete_dependency_path')}")
        print(f"  Extraction: {found_finstmt.get('extraction_status')}")

if __name__ == "__main__":
    asyncio.run(test_e2e())
