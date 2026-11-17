import csv
import json
from pathlib import Path
import time
from typing import Any

SOURCE_PATH = Path(__file__).parent / "HCoder_1_Complete" / "HCoder_1_75_cases.json"
OUTPUT_PATH = Path(__file__).parent / "version_checklist.csv"
CSV_COLUMNS = (
    "case_id",
    "dimension",
    "source_id",
    "document_title",
    "printed_bundle_page",
    "viewer_page",
    "line_start",
    "line_end",
    "evidence_type",
    "text",
)


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{max(0, seconds):.2f}s"

    total_seconds = int(seconds)
    minutes, remainder = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {remainder}s"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{total_seconds}s"


def generate_checklist(source_path: Path, output_path: Path) -> int:
    with source_path.open("r", encoding="utf-8") as source_file:
        data: dict[str, Any] = json.load(source_file)

    cases = data.get("cases", [])
    total_cases = len(cases)
    start_time = time.monotonic()
    row_count = 0
    written_passages: set[tuple[str, ...]] = set()
    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(CSV_COLUMNS)

        for case_number, case in enumerate(cases, start=1):
            for dimension in case.get("dimensions", []):
                for collection_name in ("documents_checked", "evidence"):
                    for passage in dimension.get(collection_name, []):
                        if passage.get("operative_at_cutoff") is True:
                            passage_key = (
                                str(case.get("case_id", "")),
                                str(dimension.get("dimension", "")),
                                str(passage.get("source_id", "")),
                                str(passage.get("document_title", "")),
                                str(passage.get("evidence_type", "")),
                                str(passage.get("text", "")),
                            )
                            if passage_key in written_passages:
                                continue
                            written_passages.add(passage_key)
                            writer.writerow(
                                (
                                    passage_key[0],
                                    passage_key[1],
                                    passage.get("source_id", ""),
                                    passage.get("document_title", ""),
                                    passage.get("printed_bundle_page", ""),
                                    passage.get("viewer_page", ""),
                                    passage.get("line_start", ""),
                                    passage.get("line_end", ""),
                                    passage.get("evidence_type", ""),
                                    passage.get("text", ""),
                                )
                            )
                            row_count += 1

            elapsed = time.monotonic() - start_time
            percentage = case_number / total_cases * 100
            remaining = elapsed / case_number * (total_cases - case_number)
            print(
                f"Progress: {percentage:5.1f}% "
                f"({case_number}/{total_cases} cases) | "
                f"elapsed {format_duration(elapsed)} | "
                f"remaining ~{format_duration(remaining)}",
                flush=True,
            )

    if total_cases == 0:
        print("Progress: 100.0% (0/0 cases) | elapsed 0s | remaining ~0s")

    return row_count


def main() -> None:
    row_count = generate_checklist(SOURCE_PATH, OUTPUT_PATH)
    print(f"Wrote {row_count} rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
