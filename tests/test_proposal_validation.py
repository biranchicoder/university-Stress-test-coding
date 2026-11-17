from __future__ import annotations

from datetime import date

import pytest

from policy_coder.proposal_validation import (
    CaseProposal,
    CaseSummary,
    DimensionProposal,
    Passage,
    RESEARCHER_CUTOFF_EVIDENCE,
    VersionEvidenceLocation,
    _semantic_validation,
)


def _location(line: str, text: str) -> VersionEvidenceLocation:
    return VersionEvidenceLocation(
        source_id="N001",
        document_title="Test policy",
        printed_bundle_page=10,
        viewer_page=1,
        line_start=line,
        line_end=line,
        evidence_type="Exact quotation",
        text=text,
    )


def _passage(
    line: str,
    text: str,
    *,
    role: str = "MAIN",
    operative: bool = False,
    version_line: str | None = None,
    version_text: str = "",
) -> Passage:
    return Passage(
        source_id="N001",
        document_title="Test policy",
        printed_bundle_page=10,
        viewer_page=1,
        line_start=line,
        line_end=line,
        evidence_type="Exact quotation",
        text=text,
        evidence_role=role,
        operative_at_cutoff=operative,
        version_evidence=version_text,
        version_evidence_location=(_location(version_line, version_text) if version_line else None),
        visual_reading_order_checked=False,
        visual_review_note="",
    )


def _d3_conflict_proposal() -> CaseProposal:
    evidence_a = _passage(
        "L001",
        "Provision A applies.",
        operative=True,
        version_line="L003",
        version_text="Version effective 2025.",
    )
    evidence_b = _passage(
        "L002",
        "Provision B prohibits it.",
        role="SUPPORTING",
        operative=True,
        version_line="L004",
        version_text="Version effective 2025.",
    )
    dimensions = [
        DimensionProposal(
            dimension=dimension,
            score=0 if dimension == "D3" else 1,
            reason="The recorded text supports this proposal.",
            search_terms_used=["student assessment"],
            documents_checked=[],
            evidence=[evidence_a, evidence_b] if dimension == "D3" else [],
            zero_score_basis=(
                "direct_unresolved_contradiction" if dimension == "D3" else "not_applicable"
            ),
            uncertainty_notes="",
        )
        for dimension in ("D1", "D2", "D3", "D4", "D5", "D6")
    ]
    return CaseProposal(
        university="Curtin",
        case_id="Curtin-S1",
        scenario_id="S1",
        coding_date=date(2026, 11, 15),
        protocol_version="1.1",
        coder_id="Antigravity AI proposal — awaiting human verification",
        human_confirmed=False,
        dimensions=dimensions,
        case_summary=CaseSummary(
            mode="Delegating",
            review_route="No",
            review_route_explanation="",
            review_route_evidence=None,
            critical_gap="No",
            critical_risk_criterion="A supplied critical-risk criterion.",
            critical_gap_explanation="",
            evidence_status="Sufficient",
            insufficient_evidence_reason="",
            rule_direction="Conditional",
            conflict_confirmed=True,
            conflict_basis="The two operative provisions cannot both apply.",
            case_note="The provisions appear incompatible. No hierarchy resolves them.",
            supporting_passages=[],
            search_terms_used=["student assessment"],
            documents_checked=[],
            uncertainty_notes="",
        ),
        additional_search_requests=[],
    )


def _pages() -> dict[tuple[str, int], dict[str, object]]:
    lines = {
        "L001": "Provision A applies.",
        "L002": "Provision B prohibits it.",
        "L003": "Version effective 2025.",
        "L004": "Version effective 2025.",
    }
    return {
        ("Curtin", 1): {
            "mapping_status": "verified",
            "source_id": "N001",
            "document_title": "Test policy",
            "printed_bundle_page": 10,
            "line_text_json": lines,
            "page_text": "\n".join(lines.values()),
            "extraction_flags": [],
        }
    }


def test_d3_zero_requires_two_distinct_cited_operative_provisions() -> None:
    proposal = _d3_conflict_proposal()
    errors, warnings, outcome = _semantic_validation(
        proposal,
        _pages(),
        {("Curtin", "S1"): {"student assessment"}},
    )
    assert errors == []
    assert outcome == "Documentary conflict"

    only_one = proposal.model_copy(
        update={
            "dimensions": [
                (
                    dimension.model_copy(update={"evidence": dimension.evidence[:1]})
                    if dimension.dimension == "D3"
                    else dimension
                )
                for dimension in proposal.dimensions
            ]
        }
    )
    errors, _, _ = _semantic_validation(
        only_one,
        _pages(),
        {("Curtin", "S1"): {"student assessment"}},
    )
    assert any("two distinct incompatible provisions" in error for error in errors)


def test_operative_at_cutoff_requires_version_evidence_location() -> None:
    with pytest.raises(ValueError, match="version_evidence"):
        _passage("L001", "Provision A applies.", operative=True)


def test_researcher_cutoff_rule_allows_unlocated_version_evidence() -> None:
    passage = _passage(
        "L001",
        "A webpage passage.",
        operative=True,
        version_text=RESEARCHER_CUTOFF_EVIDENCE,
    )

    assert passage.version_evidence_location is None


def test_exact_quote_and_printed_page_must_match_index() -> None:
    proposal = _d3_conflict_proposal()
    bad_passage = proposal.dimensions[0].model_copy(
        update={"evidence": [_passage("L001", "Invented quotation.")]}
    )
    changed = proposal.model_copy(
        update={
            "dimensions": [
                bad_passage if item.dimension == "D1" else item for item in proposal.dimensions
            ]
        }
    )
    errors, _, _ = _semantic_validation(
        changed,
        _pages(),
        {("Curtin", "S1"): {"student assessment"}},
    )
    assert any("exact quotation differs" in error for error in errors)
