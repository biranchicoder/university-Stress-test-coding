import json
from pathlib import Path
from typing import Any, Iterator

from policy_coder.proposal_validation import ProposalBatch, RESEARCHER_CUTOFF_EVIDENCE

FINAL_JSON_PATH = (
    Path(__file__).parent.parent / "Coder_2_Complete" / "proposals_75_HCoder2_final.json"
)
DISALLOWED_KEYS = {"pdf_layout_note", "calculated_outcome"}
VISUAL_REVIEW_NOTE = "Visually verified – order matches PDF"


def _load_final_json() -> dict[str, Any]:
    with FINAL_JSON_PATH.open("r", encoding="utf-8") as json_file:
        data: object = json.load(json_file)
    if not isinstance(data, dict):
        raise TypeError("Final proposal JSON must contain a top-level object.")
    return {str(key): value for key, value in data.items()}


def _passages(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if "operative_at_cutoff" in value:
            yield value
        for child in value.values():
            yield from _passages(child)
    elif isinstance(value, list):
        for child in value:
            yield from _passages(child)


def test_final_json_validates_against_proposal_batch() -> None:
    ProposalBatch.model_validate(_load_final_json())


def test_passages_have_no_disallowed_keys() -> None:
    passages = list(_passages(_load_final_json()))
    assert passages
    for passage in passages:
        assert DISALLOWED_KEYS.isdisjoint(passage)


def test_every_passage_has_visual_review_fields() -> None:
    passages = list(_passages(_load_final_json()))
    assert passages
    for passage in passages:
        assert passage["visual_reading_order_checked"] is True
        assert passage["visual_review_note"] == VISUAL_REVIEW_NOTE


def test_researcher_rule_passages_do_not_keep_placeholder_locations() -> None:
    passages = list(_passages(_load_final_json()))
    for passage in passages:
        if passage["version_evidence"] == RESEARCHER_CUTOFF_EVIDENCE:
            assert passage["version_evidence_location"] is None
