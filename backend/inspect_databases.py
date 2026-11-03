import sqlite3
import os

files = [
    "platform_data.db",
    "platform_dev.db",
    "data/sap_cds.db"
]

for file in files:
    print("\n" + "=" * 70)
    print("DATABASE:", file)
    print("=" * 70)

    if not os.path.exists(file):
        print("FILE NOT FOUND")
        continue

    print("SIZE:", os.path.getsize(file), "bytes")

    conn = sqlite3.connect(file)
    cursor = conn.cursor()

    tables = cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table'
        ORDER BY name
    """).fetchall()

    print("\nTABLES:")
    for table in tables:
        print("-", table[0])

    for table in ["cds_sources", "cds_objects", "cds_dependencies"]:
        print(f"\nSCHEMA: {table}")

        try:
            columns = cursor.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()

            if columns:
                for column in columns:
                    print(column)
            else:
                print("TABLE NOT FOUND")

        except Exception as error:
            print("ERROR:", error)

    conn.close()

print("\nDATABASE INSPECTION COMPLETED")