import csv
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from policy_coder.proposal_validation import (
    AdditionalSearchRequest,
    CaseProposal,
    CaseSummary,
    DimensionProposal,
    Passage,
    ProposalBatch,
    RESEARCHER_CUTOFF_EVIDENCE,
    VersionEvidenceLocation,
)

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "version_checklist.csv"
SOURCE_PATH = ROOT / "Coder_2_Complete" / "proposals_75_HCoder2_fixed.json"
OUTPUT_PATH = ROOT / "Coder_2_Complete" / "proposals_75_HCoder2_final.json"
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
IDENTITY_COLUMNS = (
    "case_id",
    "dimension",
    "source_id",
    "document_title",
    "evidence_type",
    "text",
)
VISUAL_REVIEW_NOTE = "Visually verified – order matches PDF"

CitationKey = tuple[str, str, str, str, str, str]
PassageKey = tuple[str, str, str, str, str]
Location = dict[str, int | str]


def _citation_key(
    case_id: str,
    dimension: str,
    passage: dict[str, Any],
) -> CitationKey:
    return (
        case_id,
        dimension,
        str(passage.get("source_id", "")),
        str(passage.get("document_title", "")),
        str(passage.get("evidence_type", "")),
        str(passage.get("text", "")),
    )


def _passage_key(case_id: str, passage: dict[str, Any]) -> PassageKey:
    return (
        case_id,
        str(passage.get("source_id", "")),
        str(passage.get("document_title", "")),
        str(passage.get("evidence_type", "")),
        str(passage.get("text", "")),
    )


def _operative_citation_keys(data: dict[str, Any]) -> set[CitationKey]:
    keys: set[CitationKey] = set()
    for proposal in data.get("proposals", []):
        case_id = str(proposal.get("case_id", ""))
        for dimension in proposal.get("dimensions", []):
            dimension_id = str(dimension.get("dimension", ""))
            for collection_name in ("documents_checked", "evidence"):
                for passage in dimension.get(collection_name, []):
                    if passage.get("operative_at_cutoff") is True:
                        keys.add(_citation_key(case_id, dimension_id, passage))
    return keys


def _read_location(row: dict[str, str]) -> Location:
    try:
        printed_page = int(row["printed_bundle_page"])
        viewer_page = int(row["viewer_page"])
    except ValueError as exc:
        raise ValueError("CSV page numbers must be positive integers.") from exc

    line_start = row["line_start"]
    line_end = row["line_end"]
    if printed_page < 1 or viewer_page < 1:
        raise ValueError("CSV page numbers must be positive integers.")
    if not re.fullmatch(r"L[0-9]{3}", line_start):
        raise ValueError(f"Invalid line_start value: {line_start!r}")
    if not re.fullmatch(r"L[0-9]{3}", line_end):
        raise ValueError(f"Invalid line_end value: {line_end!r}")

    return {
        "printed_bundle_page": printed_page,
        "viewer_page": viewer_page,
        "line_start": line_start,
        "line_end": line_end,
    }


def _read_csv_updates(csv_path: Path) -> dict[CitationKey, Location]:
    updates: dict[CitationKey, Location] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames != list(CSV_COLUMNS):
            raise ValueError(
                "CSV headers do not match the required checklist columns: " + ", ".join(CSV_COLUMNS)
            )

        for row_number, raw_row in enumerate(reader, start=2):
            if None in raw_row or any(raw_row.get(field) is None for field in CSV_COLUMNS):
                raise ValueError(f"CSV row {row_number} has missing or extra columns.")
            row = {field: raw_row[field] for field in CSV_COLUMNS}
            if any(not row[field].strip() for field in IDENTITY_COLUMNS):
                raise ValueError(f"CSV row {row_number} is missing a citation identity field.")

            key: CitationKey = (
                row["case_id"],
                row["dimension"],
                row["source_id"],
                row["document_title"],
                row["evidence_type"],
                row["text"],
            )
            if key in updates:
                raise ValueError(f"CSV contains a duplicate citation at row {row_number}.")
            location = _read_location(row)
            updates[key] = location

    return updates


def _apply_csv_updates(data: dict[str, Any], updates: dict[CitationKey, Location]) -> int:
    expected_keys = _operative_citation_keys(data)
    if set(updates) != expected_keys:
        missing = len(expected_keys - set(updates))
        unexpected = len(set(updates) - expected_keys)
        raise ValueError(
            f"CSV citations do not match the proposal JSON "
            f"({missing} missing, {unexpected} unexpected)."
        )

    updates_by_passage: dict[PassageKey, Location] = {}
    for citation_key, location in updates.items():
        passage_key = (
            citation_key[0],
            citation_key[2],
            citation_key[3],
            citation_key[4],
            citation_key[5],
        )
        previous = updates_by_passage.get(passage_key)
        if previous is not None and previous != location:
            raise ValueError(
                f"CSV has conflicting locations for the same passage in case {passage_key[0]}."
            )
        updates_by_passage[passage_key] = location

    updated_keys: set[PassageKey] = set()

    def update_nested(value: Any, case_id: str) -> None:
        if isinstance(value, dict):
            if value.get("operative_at_cutoff") is True:
                passage_key = _passage_key(case_id, value)
                location = updates_by_passage.get(passage_key)
                if location is not None:
                    value.update(location)
                    updated_keys.add(passage_key)
            for child in value.values():
                update_nested(child, case_id)
        elif isinstance(value, list):
            for child in value:
                update_nested(child, case_id)

    for proposal in data.get("proposals", []):
        update_nested(proposal, str(proposal.get("case_id", "")))

    if updated_keys != set(updates_by_passage):
        raise ValueError("One or more CSV citations did not match a passage in the proposal JSON.")

    return len(updates)


def _allowed_fields(value: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key in model.model_fields}


def _clean_passage(value: dict[str, Any]) -> dict[str, Any]:
    passage = _allowed_fields(value, Passage)
    location = passage.get("version_evidence_location")
    if passage.get("version_evidence") == RESEARCHER_CUTOFF_EVIDENCE:
        passage["version_evidence_location"] = None
    elif isinstance(location, dict):
        passage["version_evidence_location"] = _allowed_fields(location, VersionEvidenceLocation)
    passage["visual_reading_order_checked"] = True
    passage["visual_review_note"] = VISUAL_REVIEW_NOTE
    return passage


def _clean_dimension(value: dict[str, Any]) -> dict[str, Any]:
    dimension = _allowed_fields(value, DimensionProposal)
    for field in ("documents_checked", "evidence"):
        dimension[field] = [_clean_passage(passage) for passage in dimension.get(field, [])]
    return dimension


def _clean_summary(
    value: dict[str, Any],
    proposal_id: str,
    known_passages: dict[PassageKey, dict[str, Any]],
) -> dict[str, Any]:
    summary = _allowed_fields(value, CaseSummary)

    def clean_summary_passage(raw_passage: dict[str, Any]) -> dict[str, Any]:
        passage = _clean_passage(raw_passage)
        canonical = known_passages.get(_passage_key(proposal_id, raw_passage))
        if canonical is not None:
            for field in ("version_evidence", "version_evidence_location"):
                if not passage.get(field) and canonical.get(field):
                    passage[field] = canonical[field]
        return passage

    for field in ("supporting_passages", "documents_checked"):
        summary[field] = [clean_summary_passage(passage) for passage in summary.get(field, [])]
    route_evidence = summary.get("review_route_evidence")
    if isinstance(route_evidence, dict):
        summary["review_route_evidence"] = clean_summary_passage(route_evidence)
    return summary


def _clean_proposal_batch(data: dict[str, Any]) -> dict[str, Any]:
    batch = _allowed_fields(data, ProposalBatch)
    proposals = []
    for raw_proposal in batch.get("proposals", []):
        proposal = _allowed_fields(raw_proposal, CaseProposal)
        proposal["dimensions"] = [
            _clean_dimension(dimension) for dimension in proposal.get("dimensions", [])
        ]
        proposal_id = str(proposal.get("case_id", ""))
        known_passages: dict[PassageKey, dict[str, Any]] = {}
        for dimension in proposal["dimensions"]:
            for field in ("documents_checked", "evidence"):
                for passage in dimension.get(field, []):
                    if passage.get("version_evidence_location") is not None:
                        known_passages.setdefault(_passage_key(proposal_id, passage), passage)
        case_summary = proposal.get("case_summary")
        if isinstance(case_summary, dict):
            proposal["case_summary"] = _clean_summary(case_summary, proposal_id, known_passages)
        proposal["additional_search_requests"] = [
            _allowed_fields(request, AdditionalSearchRequest)
            for request in proposal.get("additional_search_requests", [])
        ]
        proposals.append(proposal)
    batch["proposals"] = proposals
    return batch


def finalize_proposals(
    csv_path: Path = CSV_PATH,
    source_path: Path = SOURCE_PATH,
    output_path: Path = OUTPUT_PATH,
) -> int:
    with source_path.open("r", encoding="utf-8") as source_file:
        data: dict[str, Any] = json.load(source_file)

    updates = _read_csv_updates(csv_path)
    updated_count = _apply_csv_updates(data, updates)
    cleaned_data = _clean_proposal_batch(data)
    batch = ProposalBatch.model_validate(cleaned_data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as output_file:
        json.dump(batch.model_dump(mode="json"), output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")

    return updated_count


def main() -> None:
    try:
        updated_count = finalize_proposals()
    except ValidationError as exc:
        raise SystemExit(
            "ProposalBatch validation failed; no final JSON was written. "
            "Correct the source proposal's missing or invalid required citations and retry.\n"
            f"{exc}"
        ) from exc
    print(f"Updated {updated_count} CSV rows and wrote final JSON to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
