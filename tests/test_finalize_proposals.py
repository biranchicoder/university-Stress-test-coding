import csv
from pathlib import Path

from finalize_proposals import CSV_COLUMNS, _read_csv_updates


def test_csv_reader_parses_locations_and_requires_exact_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "version_checklist.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_COLUMNS)
        writer.writerow(
            (
                "Curtin-S1",
                "D1",
                "N001",
                "AI Policy",
                12,
                13,
                "L004",
                "L005",
                "Exact quotation",
                "A test quotation.",
            )
        )

    updates = _read_csv_updates(csv_path)

    assert len(updates) == 1
    assert next(iter(updates.values())) == {
        "printed_bundle_page": 12,
        "viewer_page": 13,
        "line_start": "L004",
        "line_end": "L005",
    }
