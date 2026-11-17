from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

import policy_coder.preparation as preparation
from policy_coder.indexer import build_index
from test_core import make_test_package


def _indexed_root(root: Path) -> Path:
    for university in preparation.UNIVERSITIES:
        make_test_package(root, university)
    build_index(root)
    return root


def test_interruption_resumes_from_last_completed_pdf_page(tmp_path, monkeypatch) -> None:
    root = _indexed_root(tmp_path)
    with pytest.raises(preparation.SimulatedInterruption):
        preparation.run_preparation(root, interrupt_after_pages=2)

    checkpoint_path = root / "data" / "preparation" / ".preparation_checkpoint.sqlite3"
    checkpoint = sqlite3.connect(checkpoint_path)
    completed_before_restart = checkpoint.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_type='pdf_page' AND status='completed'"
    ).fetchone()[0]
    assert completed_before_restart == 2
    checkpoint.close()

    calls = []
    original = preparation._page_verification

    def count_page_verification(*args, **kwargs):
        calls.append((args[2], args[3]))
        return original(*args, **kwargs)

    monkeypatch.setattr(preparation, "_page_verification", count_page_verification)
    result = preparation.run_preparation(root)

    assert result["status"] == "complete"
    assert result["page_count"] == 5
    assert len(calls) == 3
    checkpoint = sqlite3.connect(checkpoint_path)
    completed_after_restart = checkpoint.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_type='pdf_page' AND status='completed'"
    ).fetchone()[0]
    assert completed_after_restart == 5
    checkpoint.close()
    report = (root / "data" / "preparation" / "preparation_validation_report.txt").read_text(
        encoding="utf-8"
    )
    assert "Overall status: complete" in report
    assert "Pending: 0" in report


def test_transient_page_failures_retry_three_times_then_complete(tmp_path, monkeypatch) -> None:
    root = _indexed_root(tmp_path)
    original = preparation._page_verification
    failures_left = 3

    def transient_failure(*args, **kwargs):
        nonlocal failures_left
        if args[2] == "Curtin" and args[3] == 1 and failures_left:
            failures_left -= 1
            raise OSError("temporary read interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(preparation, "_page_verification", transient_failure)
    result = preparation.run_preparation(root)

    assert result["status"] == "complete"
    checkpoint = sqlite3.connect(
        root / "data" / "preparation" / ".preparation_checkpoint.sqlite3"
    )
    task = checkpoint.execute(
        "SELECT status, attempts FROM tasks WHERE task_id='pdf_page:Curtin:0001'"
    ).fetchone()
    outcomes = checkpoint.execute(
        """SELECT outcome FROM task_attempts
        WHERE task_id='pdf_page:Curtin:0001' ORDER BY attempt_number"""
    ).fetchall()
    checkpoint.close()
    assert task == ("completed", 4)
    assert [row[0] for row in outcomes] == ["retry", "retry", "retry", "completed"]


def test_persistent_page_failure_is_logged_and_other_tasks_continue(tmp_path, monkeypatch) -> None:
    root = _indexed_root(tmp_path)
    original = preparation._page_verification

    def persistent_failure(*args, **kwargs):
        if args[2] == "Curtin" and args[3] == 1:
            raise OSError("persistent damaged page")
        return original(*args, **kwargs)

    monkeypatch.setattr(preparation, "_page_verification", persistent_failure)
    result = preparation.run_preparation(root)

    assert result["status"] == "incomplete"
    assert result["verified_pages"] == 4
    assert result["failed_pages"] == 1
    checkpoint = sqlite3.connect(
        root / "data" / "preparation" / ".preparation_checkpoint.sqlite3"
    )
    failed = checkpoint.execute(
        "SELECT status, attempts, error_text FROM tasks WHERE task_id='pdf_page:Curtin:0001'"
    ).fetchone()
    other = checkpoint.execute(
        "SELECT status FROM tasks WHERE task_id='pdf_page:ECU:0001'"
    ).fetchone()[0]
    checkpoint.close()
    assert failed[0:2] == ("failed", 4)
    assert "persistent damaged page" in failed[2]
    assert other == "completed"

    with (root / "data" / "preparation" / "policy_index.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    failed_page = next(
        row for row in rows if row["university"] == "Curtin" and row["viewer_page"] == "1"
    )
    assert "preparation_task_failed" in failed_page["extraction_flags"]
    report = (root / "data" / "preparation" / "preparation_validation_report.txt").read_text(
        encoding="utf-8"
    )
    assert "pdf_page:Curtin:0001" in report
    assert "Pending: 0" in report


def test_completed_outputs_are_never_overwritten(tmp_path) -> None:
    root = _indexed_root(tmp_path)
    preparation.run_preparation(root)
    output = root / "data" / "preparation" / "policy_index.csv"
    original_content = output.read_bytes()
    changed_content = original_content + b"\nexternal-change\n"
    output.write_bytes(changed_content)

    result = preparation.run_preparation(root)

    assert result["status"] == "incomplete"
    assert output.read_bytes() == changed_content
    checkpoint = sqlite3.connect(
        root / "data" / "preparation" / ".preparation_checkpoint.sqlite3"
    )
    status = checkpoint.execute(
        "SELECT status, error_text FROM tasks WHERE task_id='output:policy_index.csv'"
    ).fetchone()
    checkpoint.close()
    assert status[0] == "failed"
    assert "not overwritten" in status[1]
