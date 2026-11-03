from pathlib import Path

file = Path("src/services/table_discovery_service.py")
lines = file.read_text(encoding="utf-8").splitlines()

ranges = [
    (90, 235),
    (520, 610),
    (1325, 1410)
]

for start, end in ranges:
    print("\n" + "=" * 70)
    print(f"LINES {start}-{end}")
    print("=" * 70)

    for number in range(start, min(end, len(lines)) + 1):
        print(f"{number}: {lines[number - 1]}")