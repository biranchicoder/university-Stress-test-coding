import csv
import json
from pathlib import Path

from generate_version_checklist import CSV_COLUMNS, generate_checklist


def test_generation_writes_each_distinct_operative_passage_once(tmp_path: Path) -> None:
    passage = {
        "source_id": "N001",
        "document_title": "AI Policy",
        "printed_bundle_page": 12,
        "viewer_page": 13,
        "line_start": "L004",
        "line_end": "L005",
        "evidence_type": "Exact quotation",
        "text": "A test quotation.",
        "operative_at_cutoff": True,
    }
    source_path = tmp_path / "source.json"
    output_path = tmp_path / "version_checklist.csv"
    source_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "Curtin-S1",
                        "dimensions": [
                            {
                                "dimension": "D1",
                                "documents_checked": [passage],
                                "evidence": [passage],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    row_count = generate_checklist(source_path, output_path)

    with output_path.open("r", encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        rows = list(reader)
    assert row_count == 1
    assert reader.fieldnames == list(CSV_COLUMNS)
    assert rows[0]["case_id"] == "Curtin-S1"
    assert rows[0]["line_start"] == "L004"
    assert rows[0]["text"] == "A test quotation."
