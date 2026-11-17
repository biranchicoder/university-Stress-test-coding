from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from policy_coder.core import case_outcome, indexed_quote
from policy_coder.main_preparation import (
    DIMENSIONS,
    OUTPUT_DIR_NAME,
    SCENARIO_IDS,
    UNIVERSITY_ORDER,
    _display_university,
    _dimension_names,
)
from policy_coder.preparation import _atomic_replace, _readonly_database

Dimension = Literal["D1", "D2", "D3", "D4", "D5", "D6"]
EvidenceType = Literal["Exact quotation", "Paraphrase"]
EvidenceRole = Literal["MAIN", "SUPPORTING"]
ZeroBasis = Literal[
    "not_applicable",
    "no_applicable_provision_found",
    "no_decision_maker_identified",
    "direct_unresolved_contradiction",
    "no_actionable_route",
    "no_relevant_safeguards",
    "no_relevant_mechanism",
]

ZERO_BASIS_BY_DIMENSION = {
    "D1": "no_applicable_provision_found",
    "D2": "no_decision_maker_identified",
    "D3": "direct_unresolved_contradiction",
    "D4": "no_actionable_route",
    "D5": "no_relevant_safeguards",
    "D6": "no_relevant_mechanism",
}

RESEARCHER_CUTOFF_EVIDENCE = (
    "Treated as operative at 2026-11-12 cut-off per researcher instruction "
    "(Source_Register retrieval date: 12 November 2026)."
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionEvidenceLocation(StrictModel):
    source_id: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    printed_bundle_page: int = Field(ge=1, strict=True)
    viewer_page: int = Field(ge=1, strict=True)
    line_start: str = Field(pattern=r"^L[0-9]{3}$")
    line_end: str = Field(pattern=r"^L[0-9]{3}$")
    evidence_type: EvidenceType
    text: str = Field(min_length=1)


class Passage(StrictModel):
    source_id: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    printed_bundle_page: int = Field(ge=1, strict=True)
    viewer_page: int = Field(ge=1, strict=True)
    line_start: str = Field(pattern=r"^L[0-9]{3}$")
    line_end: str = Field(pattern=r"^L[0-9]{3}$")
    evidence_type: EvidenceType
    text: str = Field(min_length=1)
    evidence_role: EvidenceRole
    operative_at_cutoff: bool
    version_evidence: str
    version_evidence_location: VersionEvidenceLocation | None
    visual_reading_order_checked: bool
    visual_review_note: str
    candidate_id: str | None = None
    query_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def version_evidence_is_located(self) -> "Passage":
        # This recorded rule is the only allowed unlocated version-evidence case.
        researcher_rule_applied = self.version_evidence == RESEARCHER_CUTOFF_EVIDENCE
        if self.operative_at_cutoff and (
            not self.version_evidence.strip()
            or (self.version_evidence_location is None and not researcher_rule_applied)
        ):
            raise ValueError(
                "operative_at_cutoff=true requires version_evidence and its own cited location, "
                "unless the exact researcher cutoff rule is recorded"
            )
        if (
            self.version_evidence.strip()
            and self.version_evidence_location is None
            and not researcher_rule_applied
        ):
            raise ValueError("version_evidence requires its own source/page/line location")
        return self


class DimensionProposal(StrictModel):
    dimension: Dimension
    score: int | None = Field(ge=0, le=3, strict=True)
    reason: str
    search_terms_used: list[str]
    documents_checked: list[Passage]
    evidence: list[Passage]
    zero_score_basis: ZeroBasis
    uncertainty_notes: str


class CaseSummary(StrictModel):
    mode: Literal["Prescribing", "Delegating", "None"]
    review_route: Literal["Yes", "No", "Uncertain"]
    review_route_explanation: str
    review_route_evidence: Passage | None
    critical_gap: Literal["Yes", "No", "Uncertain"]
    critical_risk_criterion: str
    critical_gap_explanation: str
    evidence_status: Literal["Sufficient", "Insufficient"]
    insufficient_evidence_reason: str
    rule_direction: Literal["Permission", "Prohibition", "Conditional", "Unclear"]
    conflict_confirmed: bool
    conflict_basis: str
    case_note: str
    supporting_passages: list[Passage]
    search_terms_used: list[str]
    documents_checked: list[Passage]
    uncertainty_notes: str


class AdditionalSearchRequest(StrictModel):
    request_id: str = Field(min_length=1)
    university: str
    case_id: str
    scenario_id: str
    query: str = Field(min_length=1)
    category: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    requested_by: str = ""

    @model_validator(mode="after")
    def case_id_matches(self) -> "AdditionalSearchRequest":
        if self.case_id != f"{self.university}-{self.scenario_id}":
            raise ValueError("case_id must match university and scenario_id")
        if self.university not in UNIVERSITY_ORDER or self.scenario_id not in SCENARIO_IDS:
            raise ValueError("unknown university or scenario")
        return self


class CaseProposal(StrictModel):
    university: str
    case_id: str
    scenario_id: str
    coding_date: date
    protocol_version: Literal["1.1"]
    coder_id: Literal[
        "Antigravity AI proposal — awaiting human verification",
        "HCoder 1",
        "HCoder 2",
    ]
    human_confirmed: bool
    dimensions: list[DimensionProposal] = Field(min_length=6, max_length=6)
    case_summary: CaseSummary
    additional_search_requests: list[AdditionalSearchRequest]

    @model_validator(mode="after")
    def case_fields_match(self) -> "CaseProposal":
        if self.university not in UNIVERSITY_ORDER or self.scenario_id not in SCENARIO_IDS:
            raise ValueError("unknown university or scenario")
        if self.case_id != f"{self.university}-{self.scenario_id}":
            raise ValueError("case_id must match university and scenario_id")
        dimensions = [item.dimension for item in self.dimensions]
        if len(set(dimensions)) != 6 or set(dimensions) != set(DIMENSIONS):
            raise ValueError("dimensions must contain D1 through D6 exactly once")
        for request in self.additional_search_requests:
            if request.university != self.university or request.case_id != self.case_id:
                raise ValueError("additional search request must belong to its parent case")
        return self


class ProposalBatch(StrictModel):
    schema_version: Literal["1.0"]
    proposals: list[CaseProposal] = Field(min_length=75, max_length=75)

    @model_validator(mode="after")
    def contains_exact_case_set(self) -> "ProposalBatch":
        actual = [(row.university, row.scenario_id) for row in self.proposals]
        expected = {
            (university, scenario_id)
            for university in UNIVERSITY_ORDER
            for scenario_id in SCENARIO_IDS
        }
        if len(set(actual)) != 75 or set(actual) != expected:
            raise ValueError(
                "proposals must contain each of the 75 university/scenario cases exactly once"
            )
        return self


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _index_pages(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    pages: dict[tuple[str, int], dict[str, Any]] = {}
    for row in _read_csv(path):
        viewer_page = int(row["viewer_page"])
        printed = row["printed_bundle_page"]
        row["viewer_page"] = viewer_page
        row["printed_bundle_page"] = int(printed) if printed.isdigit() else None
        row["line_text_json"] = json.loads(row["line_text_json"] or "{}")
        row["extraction_flags"] = [
            flag.strip() for flag in row["extraction_flags"].split(";") if flag.strip()
        ]
        pages[(row["university"], viewer_page)] = row
    return pages


def _case_search_activity(
    handoff_dir: Path,
) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], list[str]]]:
    terms: dict[tuple[str, str], set[str]] = {}
    no_hits: dict[tuple[str, str], list[str]] = {}
    files = [handoff_dir / "search_log.csv"]
    files.extend(sorted(handoff_dir.glob("additional_search_results_*.csv")))
    for path in files:
        if not path.is_file():
            continue
        for row in _read_csv(path):
            if not row.get("university") or not row.get("scenario_id") or not row.get("query"):
                continue
            key = (row["university"], row["scenario_id"])
            terms.setdefault(key, set()).add(row["query"])
            if row.get("result_count") == "0":
                no_hits.setdefault(key, []).append(row["query"])
    return terms, no_hits


def _normalised_text(value: str) -> str:
    return " ".join(value.split())


def _validate_location(
    location: Passage | VersionEvidenceLocation,
    university: str,
    pages: dict[tuple[str, int], dict[str, Any]],
    errors: list[str],
    warnings: list[str],
    label: str,
) -> dict[str, Any] | None:
    page = pages.get((university, location.viewer_page))
    if page is None:
        errors.append(
            f"{label}: viewer page {location.viewer_page} is absent from the prepared index."
        )
        return None
    if page.get("mapping_status") != "verified":
        errors.append(
            f"{label}: page mapping status is {page.get('mapping_status')!r}, not verified."
        )
    if page.get("source_id") != location.source_id:
        errors.append(
            f"{label}: source ID {location.source_id} does not match the page footer "
            f"{page.get('source_id')!r}."
        )
    if page.get("document_title") != location.document_title:
        errors.append(f"{label}: document title does not match the source register.")
    if page.get("printed_bundle_page") != location.printed_bundle_page:
        errors.append(
            f"{label}: printed bundle page does not match the PDF footer; viewer page is not a substitute."
        )
    lines = page.get("line_text_json", {})
    if location.line_start not in lines or location.line_end not in lines:
        errors.append(f"{label}: line range must use existing visible L-labels.")
        return page
    first = int(location.line_start[1:])
    last = int(location.line_end[1:])
    if first > last:
        errors.append(f"{label}: line range start is after its end.")
        return page
    if any(f"L{number:03d}" not in lines for number in range(first, last + 1)):
        errors.append(f"{label}: line range crosses an absent visible L-label.")
        return page
    expected = indexed_quote(lines, location.line_start, location.line_end)
    if location.evidence_type == "Exact quotation" and location.text != expected:
        errors.append(f"{label}: exact quotation differs from the verified indexed line range.")
    flags = set(page.get("extraction_flags", []))
    if flags.intersection({"ambiguous_table", "scrambled_reading_order"}):
        raw_page_text = _normalised_text(str(page.get("page_text", "")))
        quoted_text = _normalised_text(location.text)
        if location.evidence_type == "Exact quotation" and quoted_text not in raw_page_text:
            errors.append(
                f"{label}: quotation is not a contiguous passage in extracted page text; table reading order is unsafe."
            )
        if isinstance(location, Passage) and not location.visual_reading_order_checked:
            warnings.append(
                f"{label}: ambiguous/scrambled table requires a human visual check against the original PDF."
            )
        else:
            warnings.append(
                f"{label}: AI visual review does not constitute human confirmation; verify the original PDF image."
            )
    return page


def _validate_passage(
    passage: Passage,
    university: str,
    pages: dict[tuple[str, int], dict[str, Any]],
    errors: list[str],
    warnings: list[str],
    label: str,
) -> None:
    _validate_location(passage, university, pages, errors, warnings, label)
    if passage.version_evidence_location is not None:
        version_page = _validate_location(
            passage.version_evidence_location,
            university,
            pages,
            errors,
            warnings,
            f"{label} version evidence",
        )
        if version_page is not None:
            version_text = _normalised_text(passage.version_evidence)
            location_text = _normalised_text(passage.version_evidence_location.text)
            indexed_text = _normalised_text(
                indexed_quote(
                    version_page["line_text_json"],
                    passage.version_evidence_location.line_start,
                    passage.version_evidence_location.line_end,
                )
            )
            if version_text and (
                version_text not in location_text or version_text not in indexed_text
            ):
                errors.append(
                    f"{label}: recorded version evidence is not present at its cited indexed location."
                )


def _semantic_validation(
    proposal: CaseProposal,
    pages: dict[tuple[str, int], dict[str, Any]],
    actual_search_terms: dict[tuple[str, str], set[str]],
) -> tuple[list[str], list[str], str]:
    errors: list[str] = []
    warnings: list[str] = []
    key = (proposal.university, proposal.scenario_id)
    actual_terms = actual_search_terms.get(key, set())
    dimensions = {item.dimension: item for item in proposal.dimensions}
    for dimension in proposal.dimensions:
        label = f"{proposal.case_id} {dimension.dimension}"
        if not dimension.reason.strip():
            errors.append(f"{label}: reason is required.")
        for term in dimension.search_terms_used:
            if term not in actual_terms:
                errors.append(
                    f"{label}: search term was not found in actual search logs: {term!r}."
                )
        for index, passage in enumerate(dimension.documents_checked, 1):
            _validate_passage(
                passage,
                proposal.university,
                pages,
                errors,
                warnings,
                f"{label} document checked #{index}",
            )
        for index, passage in enumerate(dimension.evidence, 1):
            _validate_passage(
                passage,
                proposal.university,
                pages,
                errors,
                warnings,
                f"{label} evidence #{index}",
            )
        if dimension.score == 0:
            if dimension.zero_score_basis != ZERO_BASIS_BY_DIMENSION[dimension.dimension]:
                errors.append(
                    f"{label}: zero_score_basis does not match the supplied codebook's score-0 definition."
                )
            if not dimension.search_terms_used or not (
                dimension.documents_checked or dimension.evidence
            ):
                errors.append(
                    f"{label}: score 0 requires actual searches and documented sources checked; absence alone is not zero."
                )
        elif dimension.zero_score_basis != "not_applicable":
            errors.append(
                f"{label}: nonzero or blank score must use zero_score_basis='not_applicable'."
            )

    summary = proposal.case_summary
    summary_label = f"{proposal.case_id} case summary"
    for term in summary.search_terms_used:
        if term not in actual_terms:
            errors.append(
                f"{summary_label}: search term was not found in actual search logs: {term!r}."
            )
    for index, passage in enumerate(summary.documents_checked, 1):
        _validate_passage(
            passage,
            proposal.university,
            pages,
            errors,
            warnings,
            f"{summary_label} document checked #{index}",
        )
    for index, passage in enumerate(summary.supporting_passages, 1):
        _validate_passage(
            passage,
            proposal.university,
            pages,
            errors,
            warnings,
            f"{summary_label} supporting passage #{index}",
        )
    if summary.review_route_evidence is not None:
        _validate_passage(
            summary.review_route_evidence,
            proposal.university,
            pages,
            errors,
            warnings,
            f"{summary_label} review route evidence",
        )
    if summary.review_route == "Yes" and summary.review_route_evidence is None:
        errors.append(f"{summary_label}: Review route=Yes requires a cited supporting passage.")
    if summary.review_route == "Yes" and not summary.review_route_explanation.strip():
        errors.append(
            f"{summary_label}: Review route=Yes requires an explanation of the decision reviewed."
        )
    if (
        summary.critical_gap in {"Yes", "Uncertain"}
        and not summary.critical_gap_explanation.strip()
    ):
        errors.append(f"{summary_label}: critical gap explanation is required.")
    if (
        summary.evidence_status == "Insufficient"
        and not summary.insufficient_evidence_reason.strip()
    ):
        errors.append(f"{summary_label}: insufficient-evidence reason is required.")
    sentences = [item for item in re.split(r"(?<=[.!?])\s+", summary.case_note.strip()) if item]
    if len(sentences) not in {2, 3}:
        errors.append(f"{summary_label}: case note must contain two or three sentences.")
    if summary.critical_risk_criterion.strip() == "":
        errors.append(f"{summary_label}: Protocol v1.1 critical-risk criterion is required.")
    if (
        summary.evidence_status == "Sufficient"
        and not any(dimension.evidence for dimension in proposal.dimensions)
        and not summary.supporting_passages
    ):
        errors.append(
            f"{summary_label}: evidence status Sufficient requires at least one cited passage."
        )

    if summary.evidence_status == "Insufficient" and any(
        dimension.score == 0 for dimension in proposal.dimensions
    ):
        errors.append(
            f"{summary_label}: insufficient evidence conflicts with a zero score; missing material is not zero."
        )
    d3 = dimensions["D3"]
    conflict_valid = False
    if d3.score == 0:
        operative = [
            passage
            for passage in d3.evidence
            if passage.operative_at_cutoff and passage.version_evidence_location is not None
        ]
        unique_locations = {
            (
                passage.source_id,
                passage.viewer_page,
                passage.line_start,
                passage.line_end,
            )
            for passage in operative
        }
        if len(unique_locations) < 2:
            errors.append(
                f"{proposal.case_id} D3: score 0 requires two distinct incompatible provisions, each verified operative at cut-off."
            )
        if not summary.conflict_basis.strip():
            errors.append(f"{summary_label}: D3=0 requires an unresolved-conflict explanation.")
        if not summary.conflict_confirmed:
            errors.append(
                f"{summary_label}: D3=0 requires confirmation that the cited provisions conflict."
            )
        conflict_valid = (
            len(unique_locations) >= 2
            and bool(summary.conflict_basis.strip())
            and summary.conflict_confirmed
        )
    elif summary.conflict_basis.strip() or summary.conflict_confirmed:
        warnings.append(
            f"{summary_label}: conflict basis is present although D3 is not 0; check for contradiction."
        )

    if summary.mode == "None" and summary.review_route == "Yes":
        warnings.append(f"{summary_label}: mode None conflicts with review route Yes; verify.")
    if (
        summary.rule_direction == "Unclear"
        and dimensions["D1"].score == 3
        and dimensions["D4"].score == 3
    ):
        warnings.append(
            f"{summary_label}: rule direction Unclear conflicts with D1=3 and D4=3; verify."
        )
    if summary.critical_gap == "Yes" and summary.evidence_status == "Insufficient":
        warnings.append(
            f"{summary_label}: critical gap asserted while evidence is insufficient; ensure it is not inferred from absence."
        )
    outcome_summary = {
        "evidence_status": summary.evidence_status,
        "critical_gap": summary.critical_gap,
        "mode": summary.mode,
        "review_route": summary.review_route,
        "rule_direction": summary.rule_direction,
        "conflict_explanation": summary.conflict_basis,
    }
    outcome = case_outcome(
        {dimension: item.score for dimension, item in dimensions.items()},
        outcome_summary,
        conflict_valid=conflict_valid,
    )
    return errors, warnings, outcome


def _passage_text(
    passage: Passage,
    university: str,
    pages: dict[tuple[str, int], dict[str, Any]],
    *,
    heading: str | None = None,
) -> list[str]:
    page = pages[(university, passage.viewer_page)]
    flags = set(page.get("extraction_flags", []))
    lines = [heading] if heading else []
    lines.extend(
        [
            f"Source ID: {passage.source_id}",
            f"Document title: {passage.document_title}",
            f"Printed bundle page: {passage.printed_bundle_page}",
            f"Line numbers: {passage.line_start}–{passage.line_end}",
            f"Evidence type: {passage.evidence_type}",
        ]
    )
    if passage.evidence_type == "Exact quotation":
        lines.extend(["Exact quotation:", passage.text])
    else:
        lines.extend(["Evidence:", passage.text])
    lines.append(
        "Operative at policy cut-off: "
        + (
            "Verified with recorded version evidence"
            if passage.operative_at_cutoff
            else "Unverified"
        )
    )
    if passage.version_evidence:
        location = passage.version_evidence_location
        lines.append("Recorded version evidence: " + passage.version_evidence)
        if location is not None:
            lines.extend(
                [
                    f"Version evidence source ID: {location.source_id}",
                    f"Version evidence document title: {location.document_title}",
                    f"Version evidence printed bundle page: {location.printed_bundle_page}",
                    f"Version evidence line numbers: {location.line_start}–{location.line_end}",
                ]
            )
    if flags.intersection({"ambiguous_table", "scrambled_reading_order"}):
        lines.append(
            "PDF reading order: AI visual review reported; human verification still required."
        )
    return lines


def _format_checked_documents(passages: list[Passage]) -> str:
    if not passages:
        return "No documents recorded as checked."
    unique: dict[tuple[str, int, str, str], Passage] = {}
    for passage in passages:
        key = (
            passage.source_id,
            passage.viewer_page,
            passage.line_start,
            passage.line_end,
        )
        unique.setdefault(key, passage)
    return "\n".join(
        f"{passage.source_id} — {passage.document_title}, printed bundle page "
        f"{passage.printed_bundle_page}, lines {passage.line_start}–{passage.line_end}."
        for passage in unique.values()
    )


def _negative_notes(
    proposal: CaseProposal,
    no_hit_queries: dict[tuple[str, str], list[str]],
    uncertainty: str,
) -> list[str]:
    notes: list[str] = []
    no_hits = no_hit_queries.get((proposal.university, proposal.scenario_id), [])
    if no_hits:
        notes.append(
            "Actual searches with no indexed hits:\n" + "\n".join(f"- {query}" for query in no_hits)
        )
    else:
        notes.append("No logged query returned zero index hits.")
    if uncertainty.strip():
        notes.append(uncertainty.strip())
    else:
        notes.append("No additional uncertainty or access limitation was recorded.")
    return notes


def _render_passages(
    passages: list[Passage],
    university: str,
    pages: dict[tuple[str, int], dict[str, Any]],
    role: str,
) -> list[str]:
    selected = [passage for passage in passages if passage.evidence_role == role]
    lines: list[str] = []
    for passage in selected:
        if lines:
            lines.append("")
        lines.extend(_passage_text(passage, university, pages))
    return lines


def _render_dimension(
    proposal: CaseProposal,
    dimension: DimensionProposal,
    scenario_title: str,
    dimension_name: str,
    pages: dict[tuple[str, int], dict[str, Any]],
    actual_terms: dict[tuple[str, str], set[str]],
    no_hit_queries: dict[tuple[str, str], list[str]],
) -> list[str]:
    case_key = (proposal.university, proposal.scenario_id)
    searches = sorted(actual_terms.get(case_key, set()))
    lines = [
        f"University: {_display_university(proposal.university)}",
        f"Case ID: {proposal.case_id}",
        f"Scenario: {proposal.scenario_id} — {scenario_title}",
        f"Dimension: {dimension.dimension} — {dimension_name}",
        f"Coder ID: {proposal.coder_id}",
        f"Coding date: {proposal.coding_date.isoformat()}",
        "Record status: AI proposal — awaiting human verification",
        "Previous score, if revised: Not applicable — proposal only; no human revision",
        "",
        f"My score: {'' if dimension.score is None else dimension.score}",
        "",
        "Search terms used:",
        "\n".join(searches) if searches else "No searches logged.",
        "",
        "Documents checked:",
        _format_checked_documents(dimension.documents_checked),
        "",
        "MAIN EVIDENCE",
    ]
    main_passages = _render_passages(dimension.evidence, proposal.university, pages, "MAIN")
    lines.extend(main_passages if main_passages else ["No main evidence passage proposed."])
    lines.extend(["", "SUPPORTING EVIDENCE"])
    support_passages = _render_passages(
        dimension.evidence, proposal.university, pages, "SUPPORTING"
    )
    lines.extend(
        support_passages if support_passages else ["No supporting evidence passage proposed."]
    )
    lines.extend(
        [
            "",
            "WHY THE EVIDENCE FITS MY SCORE",
            dimension.reason,
            "",
            "NEGATIVE SEARCH / ACCESS / UNCERTAINTY NOTES",
            *_negative_notes(proposal, no_hit_queries, dimension.uncertainty_notes),
            "",
        ]
    )
    return lines


def _render_case_summary(
    proposal: CaseProposal,
    scenario_title: str,
    dimensions: dict[str, DimensionProposal],
    pages: dict[tuple[str, int], dict[str, Any]],
    protocol: dict[str, Any],
    actual_terms: dict[tuple[str, str], set[str]],
    no_hit_queries: dict[tuple[str, str], list[str]],
) -> list[str]:
    summary = proposal.case_summary
    searches = sorted(actual_terms.get((proposal.university, proposal.scenario_id), set()))
    if summary.review_route_evidence is not None:
        route_passage = _passage_text(summary.review_route_evidence, proposal.university, pages)
        route_heading = "Supporting source ID, bundle page and lines:"
        route_fields = [route_heading, *route_passage]
    else:
        route_fields = ["Supporting source ID, bundle page and lines:", "Not applicable."]
    risk = protocol["critical_risk_criteria"][proposal.scenario_id]
    critical_risk = (
        f"{risk}\nProtocol v1.1 was adopted after preliminary review on "
        f"{protocol.get('date_saved', '2026-11-15')}; it was not a criterion agreed before the original coding."
    )
    lines = [
        f"University: {_display_university(proposal.university)}",
        f"Case ID: {proposal.case_id}",
        f"Scenario: {proposal.scenario_id} — {scenario_title}",
        "",
        f"Mode: {summary.mode}",
        f"Review route: {summary.review_route}",
        *route_fields,
        "Brief explanation of which decision the route reviews:",
        summary.review_route_explanation or "Not recorded.",
        "",
        f"Critical gap: {summary.critical_gap}",
        "Relevant pre-agreed critical risk:",
        critical_risk,
        "Specific gap and supporting evidence/search record:",
        summary.critical_gap_explanation or "Not recorded.",
    ]
    if summary.supporting_passages:
        lines.extend(["Supporting passages:"])
        for passage in summary.supporting_passages:
            lines.extend(_passage_text(passage, proposal.university, pages))
            lines.append("")
    lines.extend(
        [
            "",
            f"Evidence status: {summary.evidence_status}",
            "Reason if insufficient:",
            summary.insufficient_evidence_reason or "Not applicable.",
            "",
            f"Rule direction: {summary.rule_direction}",
            "",
            "Conflict basis:",
        ]
    )
    if dimensions["D3"].score == 0:
        lines.extend(
            _render_passages(dimensions["D3"].evidence, proposal.university, pages, "MAIN")
        )
        lines.extend(
            _render_passages(dimensions["D3"].evidence, proposal.university, pages, "SUPPORTING")
        )
        lines.append(summary.conflict_basis)
    else:
        lines.append(summary.conflict_basis or "Not applicable.")
    lines.extend(
        [
            "",
            "Case note:",
            summary.case_note or "Not recorded.",
            "Calculated outcome: Leave to the workbook formula.",
            "",
            "Case searches actually logged:",
            "\n".join(searches) if searches else "No searches logged.",
            "Documents checked:",
            _format_checked_documents(summary.documents_checked),
            "Negative search / access / uncertainty notes:",
            *_negative_notes(proposal, no_hit_queries, summary.uncertainty_notes),
            "",
        ]
    )
    return lines


def _format_validation_report(report: dict[str, Any]) -> str:
    lines = [
        "AI PROPOSAL VALIDATION REPORT",
        f"Status: {report['status']}",
        f"Proposal SHA-256: {report['proposal_sha256']}",
        f"Validation time: {report['validated_at']}",
        f"Cases received: {report['case_count']}",
        f"Errors: {len(report['errors'])}",
        f"Warnings: {len(report['warnings'])}",
        "",
        "ERRORS:",
        *(f"- {message}" for message in report["errors"] or ["None."]),
        "",
        "WARNINGS:",
        *(f"- {message}" for message in report["warnings"] or ["None."]),
        "",
        "OUTCOME PREVIEWS (validation only; text exports leave this to workbook formulas):",
    ]
    lines.extend(
        [
            "",
            "Outcome previews, if available, are retained only in validation_report.json.",
            "",
            "EXPORT FILES:",
        ]
    )
    lines.extend(
        f"- {path}"
        for path in report.get("export_files", []) or ["None; validation errors block export."]
    )
    return "\n".join(lines) + "\n"


def validate_and_export_proposals(root: Path, proposals_path: Path) -> dict[str, Any]:
    root = root.resolve()
    handoff_dir = root / "data" / "preparation" / OUTPUT_DIR_NAME
    proposals_path = proposals_path.resolve()
    if not handoff_dir.is_dir():
        raise FileNotFoundError("Main-dataset handoff folder is missing.")
    if not proposals_path.is_file():
        raise FileNotFoundError(f"AI proposal JSON is missing: {proposals_path}")
    raw_bytes = proposals_path.read_bytes()
    proposal_sha = hashlib.sha256(raw_bytes).hexdigest()
    index_path = handoff_dir / "policy_index.csv"
    protocol_path = root / "data" / "preparation" / "protocol_v1.1_critical_risk_amendment.json"
    if not index_path.is_file() or not protocol_path.is_file():
        raise FileNotFoundError("Prepared policy index or Protocol v1.1 file is missing.")

    input_fingerprint = hashlib.sha256(
        raw_bytes
        + hashlib.sha256(index_path.read_bytes()).digest()
        + hashlib.sha256(protocol_path.read_bytes()).digest()
    ).hexdigest()
    output_dir = handoff_dir / f"validated_ai_proposals_{input_fingerprint[:12]}"
    existing_report = output_dir / "validation_report.json"
    if existing_report.is_file():
        report = json.loads(existing_report.read_text(encoding="utf-8"))
        if report.get("input_fingerprint") != input_fingerprint:
            raise FileExistsError("Existing validation output does not match these exact inputs.")
        return report

    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw_data = json.loads(raw_bytes.decode("utf-8"))
        batch = ProposalBatch.model_validate(raw_data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors.extend(
                f"Schema: {'/'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors()
            )
        else:
            errors.append(f"Schema: {type(exc).__name__}: {exc}")
        report = {
            "status": "invalid",
            "proposal_sha256": proposal_sha,
            "input_fingerprint": input_fingerprint,
            "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "case_count": 0,
            "errors": errors,
            "warnings": warnings,
            "outcome_previews": [],
            "export_files": [],
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        _atomic_replace(
            output_dir / "validation_report.json",
            (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            allow_replace=False,
        )
        _atomic_replace(
            output_dir / "validation_report.txt",
            _format_validation_report(report).encode("utf-8"),
            allow_replace=False,
        )
        return report

    pages = _index_pages(index_path)
    actual_terms, no_hit_queries = _case_search_activity(handoff_dir)
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol.get("protocol_version") != "1.1":
        raise ValueError("The supplied protocol file is not Protocol v1.1.")
    database_path = root / "data" / "policy_coding.sqlite3"
    connection = _readonly_database(database_path)
    try:
        resource_rows = connection.execute(
            "SELECT university, codebook_text, scenarios_json FROM resources"
        ).fetchall()
        resources = {
            row["university"]: {
                "codebook_text": row["codebook_text"],
                "scenarios": json.loads(row["scenarios_json"]),
            }
            for row in resource_rows
        }
        dimension_names = _dimension_names(resources["Curtin"]["codebook_text"])
        outcome_previews: list[dict[str, str]] = []
        requests: list[dict[str, str]] = []
        request_ids: set[str] = set()
        exports: dict[str, list[str]] = {university: [] for university in UNIVERSITY_ORDER}

        ordered_proposals = sorted(
            batch.proposals,
            key=lambda row: (
                UNIVERSITY_ORDER.index(row.university),
                int(row.scenario_id[1:]),
            ),
        )
        for proposal in ordered_proposals:
            expected_risk = protocol["critical_risk_criteria"].get(proposal.scenario_id)
            if proposal.case_summary.critical_risk_criterion != expected_risk:
                errors.append(
                    f"{proposal.case_id}: critical-risk criterion does not exactly match Protocol v1.1."
                )
            case_errors, case_warnings, outcome = _semantic_validation(
                proposal, pages, actual_terms
            )
            errors.extend(case_errors)
            warnings.extend(case_warnings)
            dimensions = {item.dimension: item for item in proposal.dimensions}
            outcome_previews.append({"case_id": proposal.case_id, "outcome": outcome})
            scenario = resources[proposal.university]["scenarios"][proposal.scenario_id]
            for dimension in sorted(proposal.dimensions, key=lambda item: int(item.dimension[1:])):
                exports[proposal.university].extend(
                    _render_dimension(
                        proposal,
                        dimension,
                        scenario["title"],
                        dimension_names[dimension.dimension],
                        pages,
                        actual_terms,
                        no_hit_queries,
                    )
                )
            exports[proposal.university].extend(
                _render_case_summary(
                    proposal,
                    scenario["title"],
                    dimensions,
                    pages,
                    protocol,
                    actual_terms,
                    no_hit_queries,
                )
            )
            for request in proposal.additional_search_requests:
                if request.request_id in request_ids:
                    errors.append(
                        f"{proposal.case_id}: duplicate additional-search request ID {request.request_id!r}."
                    )
                request_ids.add(request.request_id)
                requests.append(request.model_dump(mode="json"))
    finally:
        connection.close()

    export_files: list[str] = []
    status = "valid" if not errors else "invalid"
    if status == "valid":
        output_dir.mkdir(parents=True, exist_ok=True)
        for university in UNIVERSITY_ORDER:
            filename = f"AI_proposal_{university}.txt"
            path = output_dir / filename
            _atomic_replace(
                path,
                ("\n".join(exports[university]).rstrip() + "\n").encode("utf-8"),
                allow_replace=False,
            )
            export_files.append(str(path))
        if requests:
            request_path = output_dir / "additional_search_requests_returned.json"
            _atomic_replace(
                request_path,
                (
                    json.dumps(
                        {"schema_version": "1.0", "requests": requests},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
                allow_replace=False,
            )
            export_files.append(str(request_path))

    report = {
        "status": status,
        "proposal_sha256": proposal_sha,
        "input_fingerprint": input_fingerprint,
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "case_count": len(batch.proposals),
        "errors": errors,
        "warnings": warnings,
        "outcome_previews": outcome_previews,
        "export_files": export_files,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_replace(
        output_dir / "validation_report.json",
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        allow_replace=False,
    )
    _atomic_replace(
        output_dir / "validation_report.txt",
        _format_validation_report(report).encode("utf-8"),
        allow_replace=False,
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate Antigravity proposals and export separate AI-only text files."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--proposals", type=Path, required=True)
    args = parser.parse_args()
    report = validate_and_export_proposals(args.root, args.proposals)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
