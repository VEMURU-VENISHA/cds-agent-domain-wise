import sqlite3

db = "data/sap_cds.db"

tables = [
    "cds_dependencies",
    "cds_associations",
    "cds_joins",
    "cds_fields",
    "cds_calculations",
    "cds_parameters",
    "cds_conditions",
    "evidence",
    "validation_results",
    "indexed_hashes"
]

conn = sqlite3.connect(db)
cursor = conn.cursor()

for table in tables:
    print("\n" + "=" * 65)
    print("TABLE:", table)
    print("=" * 65)

    columns = cursor.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    for column in columns:
        print(column)

    if table in ["cds_dependencies", "cds_joins", "cds_associations"]:
        count = cursor.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        print("RECORD COUNT:", count)

conn.close()