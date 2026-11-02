import asyncio
import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

from src.services.table_discovery_service import TableDiscoveryService

async def test_multi():
    svc = TableDiscoveryService()
    print("Testing ACDOCA, MATDOC in mode='ALL'...")
    res_all = await svc.run_discovery(tables_input=["ACDOCA", "MATDOC"], matching_mode="ALL", force_refresh_ext=False)
    print("ALL mode status: Success! Count:", len(res_all.get('recommendations', [])))
    
    print("\nTesting ACDOCA, MATDOC in mode='ANY'...")
    res_any = await svc.run_discovery(tables_input=["ACDOCA", "MATDOC"], matching_mode="ANY", force_refresh_ext=False)
    print("ANY mode status: Success! Count:", len(res_any.get('recommendations', [])))

if __name__ == "__main__":
    asyncio.run(test_multi())
