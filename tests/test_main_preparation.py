from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from policy_coder import main_preparation as main
from policy_coder.preparation import Checkpoint


def _checkpoint(tmp_path: Path) -> tuple[Path, Checkpoint]:
    root = tmp_path
    output = root / "data" / "preparation" / main.OUTPUT_DIR_NAME
    output.mkdir(parents=True)
    checkpoint = Checkpoint(
        output / ".main_dataset_checkpoint.sqlite3",
        "signature",
        {},
        {},
        plan_version=main.MAIN_PLAN_VERSION,
    )
    return root, checkpoint


def test_search_plan_covers_all_fifteen_scenarios_and_retrieval_routes() -> None:
    assert main.SCENARIO_IDS == tuple(f"S{number}" for number in range(1, 16))
    for scenario_id in main.SCENARIO_IDS:
        plan = main._search_plan(scenario_id)
        assert len(plan) >= 7
        categories = {category for category, _ in plan}
        assert any("actor" in category or "role" in category for category in categories)
        assert any(
            "permission" in category
            or "authority" in category
            or "consent" in category
            for category in categories
        )
        assert any(
            "restriction" in category
            or "limit" in category
            or "collection_and_use_limits" in category
            for category in categories
        )
        assert "procedure" in categories
        assert "review_route" in categories
        assert all(query.strip() for _, query in plan)


def test_return_schemas_keep_proposals_separate_from_human_confirmed_coding() -> None:
    proposal_schema = main._proposal_schema()
    proposal = proposal_schema["properties"]["proposals"]["items"]
    assert proposal["properties"]["human_confirmed"] == {"const": False}
    assert proposal["properties"]["coder_id"]["const"].startswith("Antigravity AI proposal")
    score = proposal["properties"]["dimensions"]["items"]["properties"]["score"]
    assert {"type": "null"} in score["oneOf"]
    assert "requests" in main._additional_request_file_schema()["properties"]


def test_additional_search_requests_reject_unmatched_case_ids() -> None:
    request = {
        "request_id": "extra-1",
        "university": "Curtin",
        "case_id": "ECU-S1",
        "scenario_id": "S1",
        "query": "delegated authority",
        "category": "permission_authority",
        "rationale": "Check the actual delegation rule.",
    }
    with pytest.raises(ValueError, match="case_id does not match"):
        main._validate_additional_requests({"schema_version": "1.0", "requests": [request]})


def test_candidate_keeps_verified_page_and_existing_line_location() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    hit = connection.execute("SELECT 3 AS viewer_page").fetchall()
    page = {
        "mapping_status": "verified",
        "source_id": "N001",
        "document_title": "Test policy",
        "printed_bundle_page": 17,
        "line_text_json": {"L001": "Policy provision for students."},
        "extraction_flags": ["ambiguous_table", "scrambled_reading_order"],
    }
    candidates = main._candidate_rows_for_query(
        "Curtin",
        "S1",
        "S1-Q01",
        "actor_and_activity",
        "policy provision",
        hit,
        {("Curtin", 3): page},
    )
    assert len(candidates) == 1
    assert candidates[0]["candidate_text"] == "Policy provision for students."
    assert candidates[0]["printed_bundle_page"] == 17
    assert candidates[0]["line_start"] == candidates[0]["line_end"] == "L001"
    assert "scrambled_reading_order" in candidates[0]["page_review_flags"]

    page["mapping_status"] = "failed"
    assert main._candidate_rows_for_query(
        "Curtin",
        "S1",
        "S1-Q01",
        "actor_and_activity",
        "policy provision",
        hit,
        {("Curtin", 3): page},
    ) == []
    connection.close()


def test_index_map_preserves_labels_from_already_normalised_rows() -> None:
    rows = [{
        "university": "ECU",
        "source_id": "N001",
        "document_title": "Test policy",
        "viewer_page": 9,
        "printed_bundle_page": 149,
        "visible_line_labels": ["L001", "L002"],
        "line_text_json": {"L001": "An operative policy provision."},
        "extraction_flags": [],
        "mapping_status": "verified",
    }]
    indexed = main._index_maps(rows)
    assert indexed[("ECU", 9)]["visible_line_labels"] == ["L001", "L002"]
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    hits = connection.execute("SELECT 9 AS viewer_page").fetchall()
    candidates = main._candidate_rows_for_query(
        "ECU", "S1", "S1-Q01", "actor_and_activity", "operative policy",
        hits, indexed
    )
    assert candidates[0]["candidate_text"] == "An operative policy provision."
    assert candidates[0]["printed_bundle_page"] == 149
    connection.close()


def test_progress_eta_uses_a_robust_task_duration_estimate(tmp_path: Path) -> None:
    root, checkpoint = _checkpoint(tmp_path)
    for task_id in ("done-1", "done-2", "done-3", "pending-1", "pending-2"):
        checkpoint.add_task(task_id, "test_task", task_id)
    for index, seconds in enumerate((1, 1, 100000), start=1):
        start = datetime(2026, 11, 15, tzinfo=timezone.utc)
        finish = start + timedelta(seconds=seconds)
        checkpoint.connection.execute(
            """INSERT INTO task_attempts
            (task_id, attempt_number, started_at, finished_at, outcome)
            VALUES (?, 1, ?, ?, 'completed')""",
            (
                f"done-{index}",
                start.isoformat(),
                finish.isoformat(),
            ),
        )
        checkpoint.connection.execute(
            "UPDATE tasks SET status='completed' WHERE task_id=?",
            (f"done-{index}",),
        )
    checkpoint.connection.commit()

    snapshot = main._progress_snapshot(checkpoint, time.time())
    assert snapshot["eta_seconds"] == 2
    checkpoint.close()


def test_checkpoint_plan_migration_invalidates_only_dependent_outputs(tmp_path: Path) -> None:
    root, checkpoint = _checkpoint(tmp_path)
    for task_id, task_type in (
        ("search:ECU:S1:01", "scenario_search"),
        ("handoff:ECU:S1", "ai_handoff"),
        ("output:search_log.csv", "search_log_export"),
        ("output:preparation_validation_report.txt", "final_report_export"),
        ("table_review:ECU:0001", "table_reading_order_review"),
    ):
        checkpoint.add_task(task_id, task_type, task_id)
        attempt = checkpoint.begin_attempt(task_id)
        checkpoint.complete(task_id, attempt, {"saved": task_id})
    checkpoint.set_metadata("plan_version", "1")
    checkpoint_path = checkpoint.path
    checkpoint.close()

    main._migrate_main_checkpoint(checkpoint_path, "signature")
    migrated = Checkpoint(
        checkpoint_path, "signature", {}, {}, plan_version=main.MAIN_PLAN_VERSION
    )
    assert migrated.task("search:ECU:S1:01")["status"] == "pending"
    assert migrated.result("search:ECU:S1:01") == {"saved": "search:ECU:S1:01"}
    assert migrated.task("handoff:ECU:S1")["status"] == "pending"
    assert migrated.task("output:search_log.csv")["status"] == "pending"
    assert migrated.task("output:preparation_validation_report.txt")["status"] == "pending"
    assert migrated.task("table_review:ECU:0001")["status"] == "completed"
    migrated.close()


def test_handoff_migration_preserves_searches_and_candidate_exports(tmp_path: Path) -> None:
    root, checkpoint = _checkpoint(tmp_path)
    for task_id, task_type in (
        ("search:ECU:S1:01", "scenario_search"),
        ("handoff:ECU:S1", "ai_handoff"),
        ("output:evidence_candidates.csv", "candidate_export"),
        ("output:preparation_validation_report.txt", "final_report_export"),
    ):
        checkpoint.add_task(task_id, task_type, task_id)
        attempt = checkpoint.begin_attempt(task_id)
        checkpoint.complete(task_id, attempt, {"saved": task_id})
    checkpoint.set_metadata("plan_version", "3")
    checkpoint_path = checkpoint.path
    checkpoint.close()

    main._migrate_main_checkpoint(checkpoint_path, "signature")
    migrated = Checkpoint(
        checkpoint_path, "signature", {}, {}, plan_version=main.MAIN_PLAN_VERSION
    )
    assert migrated.task("search:ECU:S1:01")["status"] == "completed"
    assert migrated.task("output:evidence_candidates.csv")["status"] == "completed"
    assert migrated.task("handoff:ECU:S1")["status"] == "pending"
    assert migrated.task("output:preparation_validation_report.txt")["status"] == "pending"
    migrated.close()


def test_link_gap_filter_does_not_mislabel_unknown_completeness() -> None:
    assert not main._has_reportable_link_gap({
        "unresolved_link_references": "",
        "missing_linked_instrument_status":
            "no_explicit_missing_link_detected; linked-document_completeness_not_established",
    })
    assert main._has_reportable_link_gap({
        "unresolved_link_references": "",
        "missing_linked_instrument_status": "explicitly_missing: N014",
    })
    assert main._has_reportable_link_gap({
        "unresolved_link_references": "N014",
        "missing_linked_instrument_status": "unknown",
    })


def test_checkpointed_output_can_be_safely_regenerated(tmp_path: Path) -> None:
    root, checkpoint = _checkpoint(tmp_path)
    task_id = "output:test.txt"
    checkpoint.add_task(task_id, "test_export", "test")
    path = root / "test.txt"
    started = time.time()
    main._write_task_output(root, checkpoint, task_id, path, b"old", started)
    assert path.read_bytes() == b"old"
    checkpoint.connection.execute(
        "UPDATE tasks SET status='pending' WHERE task_id=?", (task_id,)
    )
    checkpoint.connection.commit()

    main._write_task_output(root, checkpoint, task_id, path, b"new", started)
    assert path.read_bytes() == b"new"
    assert checkpoint.result(task_id)["sha256"] == main._sha256_bytes(b"new")
    checkpoint.close()


def test_completed_task_is_reused_after_restart(tmp_path: Path) -> None:
    root, checkpoint = _checkpoint(tmp_path)
    checkpoint.add_task("search:test", "scenario_search", "Curtin/S1")
    started = time.time()
    calls = 0

    def operation() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"result_count": 1}

    assert main._run_task(checkpoint, "search:test", operation, root, started) == {
        "result_count": 1
    }
    assert main._run_task(
        checkpoint,
        "search:test",
        lambda: (_ for _ in ()).throw(AssertionError("completed work reran")),
        root,
        started,
    ) == {"result_count": 1}
    assert calls == 1
    checkpoint.close()


def test_temporary_task_failures_retry_three_times_then_complete(tmp_path: Path) -> None:
    root, checkpoint = _checkpoint(tmp_path)
    checkpoint.add_task("search:test", "scenario_search", "Curtin/S1")
    failures_left = 3

    def operation() -> dict[str, bool]:
        nonlocal failures_left
        if failures_left:
            failures_left -= 1
            raise OSError("temporary read failure")
        return {"completed": True}

    assert main._run_task(
        checkpoint, "search:test", operation, root, time.time()
    ) == {"completed": True}
    task = checkpoint.task("search:test")
    assert task["status"] == "completed"
    assert task["attempts"] == 4
    outcomes = checkpoint.connection.execute(
        "SELECT outcome FROM task_attempts ORDER BY attempt_number"
    ).fetchall()
    assert [row["outcome"] for row in outcomes] == [
        "retry",
        "retry",
        "retry",
        "completed",
    ]
    checkpoint.close()
