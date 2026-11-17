from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable

import pymupdf as fitz

from app import dimension_score_criteria, page_search
from policy_coder.core import UNIVERSITIES, indexed_quote
from policy_coder.indexer import _table_flags
from policy_coder.preparation import (
    MAX_RETRIES,
    PAGE_COLUMNS,
    Checkpoint,
    _atomic_replace,
    _csv_bytes,
    _input_signature,
    _json,
    _readonly_database,
    _sha256_bytes,
    _utc_now,
    _verify_package_inputs,
)
from policy_coder.preparation import SEARCH_PLAN as PILOT_SEARCH_PLAN


MAIN_PLAN_VERSION = "4"
POLICY_CUTOFF = "2026-11-12"
SEARCH_LIMIT = 10000
MAX_CANDIDATES_PER_QUERY = 5
SCENARIO_IDS = tuple(f"S{number}" for number in range(1, 16))
DIMENSIONS = tuple(f"D{number}" for number in range(1, 7))
OUTPUT_DIR_NAME = "main_dataset_v1"
UNIVERSITY_ORDER = tuple(UNIVERSITIES)
ANTIGRAVITY_START = """ANTIGRAVITY INSTRUCTION — POLICY EVIDENCE AND AI PROPOSALS

Use the local handoff folder data\\preparation\\main_dataset_v1. Read proposal_return_schema.json before processing the 75 AI_handoff_<University>_S1-S15.txt files. Use the supplied Human_Codebook.docx as the scoring authority and Protocol v1.1 for critical-risk criteria; disclose that Protocol v1.1 was adopted after preliminary review.

Investigate each university/scenario independently using its handoff, the full policy_index.csv, source_audit.csv, search_log.csv, candidate passages, and original PDFs under data\\raw. Candidate passages are unconfirmed, and the logged lexical searches are not exhaustive semantic review. Check surrounding provisions, relevant roles, permissions, restrictions, procedures, review routes, source authority, effective/operative version evidence, supersession, missing linked instruments, and extraction/table warnings. Use printed bundle pages and existing L-labels; never substitute viewer pages or invent line numbers. Copy exact quotations only from verified contiguous passages, label paraphrases, and repeat every source/location field for every passage.

Return one JSON object with exactly 75 proposals, one for each university/scenario, matching proposal_return_schema.json. Keep coder_id exactly “Antigravity AI proposal — awaiting human verification” and human_confirmed=false. Leave a score null when evidence is inadequate; never turn missing evidence into zero. D3=0 requires two distinct incompatible provisions, each with cited version evidence showing it was operative at the policy cut-off, conflict_confirmed=true, and a conflict explanation. If operative-at-cutoff evidence is unavailable, set operative_at_cutoff=false and leave the status unverified. Do not claim human approval, change coding records, or edit source packages/exports.

Record only searches actually executed. If more local searches are needed, include structured additional_search_requests; do not describe them as performed until Python runs and logs them. Return the JSON file without code fences, then run Python validation before any text export.
"""
PILOT_PLAN_COMPLEMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "S1": (("related_roles", "student assessment"),),
    "S13": (
        ("related_roles", "student performance"),
        ("procedure", "privacy procedure"),
        ("review_route", "privacy complaint"),
    ),
}

SEARCH_COLUMNS = (
    "run_id",
    "searched_at",
    "university",
    "scenario_id",
    "query_id",
    "category",
    "query",
    "result_rank",
    "result_count",
    "source_id",
    "document_title",
    "viewer_page",
    "printed_bundle_page",
    "visible_line_labels",
    "search_snippet",
    "review_flags",
    "result_status",
    "search_origin",
)
CANDIDATE_COLUMNS = (
    "candidate_id",
    "university",
    "scenario_id",
    "query_ids",
    "search_categories",
    "search_terms",
    "source_id",
    "document_title",
    "viewer_page",
    "printed_bundle_page",
    "line_start",
    "line_end",
    "candidate_text",
    "context_before",
    "context_after",
    "page_review_flags",
    "candidate_status",
    "human_verified",
    "rating",
)

EXTENDED_SEARCH_PLAN: dict[str, tuple[tuple[str, str], ...]] = {
    "S2": (
        ("actor_and_activity", "AI assignment"),
        ("related_roles", "student assessment"),
        ("permission_authority", "delegated authority"),
        ("restriction_boundary", "academic misconduct"),
        ("procedure", "assessment procedure"),
        ("review_route", "appeal assessment"),
        ("related_risk", "authorship disclosure"),
    ),
    "S3": (
        ("actor_and_activity", "submission account"),
        ("related_roles", "student submission"),
        ("permission_authority", "authorised account"),
        ("restriction_boundary", "prohibited impersonation"),
        ("procedure", "submission procedure"),
        ("review_route", "appeal decision"),
        ("related_risk", "account security"),
    ),
    "S4": (
        ("actor_and_activity", "lecture recording"),
        ("related_roles", "student recording"),
        ("permission_authority", "consent recording"),
        ("restriction_boundary", "recording permission"),
        ("procedure", "lecture procedure"),
        ("review_route", "privacy complaint"),
        ("related_risk", "copyright recording"),
    ),
    "S5": (
        ("actor_and_activity", "attendance proxy"),
        ("related_roles", "online attendance"),
        ("permission_authority", "authorised proxy"),
        ("restriction_boundary", "false attendance"),
        ("procedure", "attendance procedure"),
        ("review_route", "attendance appeal"),
        ("related_risk", "proxy identity"),
    ),
    "S7": (
        ("actor_and_activity", "group assessment"),
        ("related_roles", "student group"),
        ("permission_authority", "equitable access"),
        ("restriction_boundary", "group contribution"),
        ("procedure", "assessment procedure"),
        ("review_route", "group appeal"),
        ("related_risk", "academic integrity"),
    ),
    "S8": (
        ("actor_and_activity", "group workspace"),
        ("related_roles", "AI contribution"),
        ("permission_authority", "member authorisation"),
        ("restriction_boundary", "workspace privacy"),
        ("procedure", "group procedure"),
        ("review_route", "peer review"),
        ("related_risk", "data retention"),
    ),
    "S9": (
        ("actor_and_activity", "peer marks"),
        ("related_roles", "group contribution"),
        ("permission_authority", "decision authority"),
        ("restriction_boundary", "evaluation criteria"),
        ("procedure", "peer assessment"),
        ("review_route", "appeal marks"),
        ("related_risk", "fairness review"),
    ),
    "S10": (
        ("actor_and_activity", "literature synthesis"),
        ("related_roles", "researcher citations"),
        ("permission_authority", "research integrity"),
        ("restriction_boundary", "citation accuracy"),
        ("procedure", "research procedure"),
        ("review_route", "research ethics"),
        ("related_risk", "privacy research"),
    ),
    "S11": (
        ("actor_and_activity", "simulated participant"),
        ("related_roles", "researcher analysis"),
        ("permission_authority", "ethics approval"),
        ("restriction_boundary", "human participants"),
        ("procedure", "research procedure"),
        ("review_route", "ethics review"),
        ("related_risk", "research validity"),
    ),
    "S12": (
        ("actor_and_activity", "research agent"),
        ("related_roles", "laboratory actions"),
        ("permission_authority", "research oversight"),
        ("restriction_boundary", "safety procedure"),
        ("procedure", "research governance"),
        ("review_route", "ethics review"),
        ("related_risk", "computational execution"),
    ),
    "S14": (
        ("actor_and_activity", "learner profile"),
        ("related_roles", "student profiling"),
        ("permission_authority", "consent purpose"),
        ("restriction_boundary", "course data"),
        ("procedure", "access correction"),
        ("review_route", "privacy complaint"),
        ("related_risk", "data sharing"),
    ),
    "S15": (
        ("actor_and_activity", "institutional systems"),
        ("related_roles", "student services"),
        ("permission_authority", "standing authority"),
        ("restriction_boundary", "system access"),
        ("procedure", "security procedure"),
        ("review_route", "review decision"),
        ("related_risk", "data protection"),
    ),
}


def _scenario_number(scenario_id: str) -> int:
    return int(scenario_id[1:])


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _read_resources(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        "SELECT university, codebook_text, scenarios_json FROM resources ORDER BY university"
    ).fetchall()
    if {row["university"] for row in rows} != set(UNIVERSITIES):
        raise ValueError("The indexed resources do not contain all five universities.")
    resources: dict[str, dict[str, Any]] = {}
    shared_codebook: str | None = None
    shared_scenarios: dict[str, Any] | None = None
    for row in rows:
        codebook = row["codebook_text"]
        scenarios = json.loads(row["scenarios_json"])
        if not isinstance(codebook, str) or not codebook.strip():
            raise ValueError(f"{row['university']}: supplied codebook text is empty.")
        if set(scenarios) != set(SCENARIO_IDS):
            raise ValueError(
                f"{row['university']}: expected scenarios S1-S15; found {sorted(scenarios)}."
            )
        if shared_codebook is None:
            shared_codebook = codebook
            shared_scenarios = scenarios
        elif codebook != shared_codebook or scenarios != shared_scenarios:
            raise ValueError(
                f"{row['university']}: extracted codebook/scenarios differ from the shared package set."
            )
        resources[row["university"]] = {
            "codebook_text": codebook,
            "scenarios": scenarios,
        }
    return resources


def _dimension_names(codebook_text: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for line in codebook_text.splitlines():
        match = re.match(r"^\s*(D[1-6])\s+(.+?)\s*$", line)
        if match and match.group(1) not in names:
            names[match.group(1)] = match.group(2)
    if set(names) != set(DIMENSIONS):
        raise ValueError("The supplied codebook does not expose all six dimension headings.")
    return names


def _search_plan(scenario_id: str) -> tuple[tuple[str, str], ...]:
    if scenario_id in PILOT_SEARCH_PLAN:
        return PILOT_SEARCH_PLAN[scenario_id] + PILOT_PLAN_COMPLEMENTS.get(scenario_id, ())
    return EXTENDED_SEARCH_PLAN[scenario_id]


def _verify_pilot_checkpoint(root: Path, input_signature: str) -> Path:
    checkpoint_path = root / "data" / "preparation" / ".preparation_checkpoint.sqlite3"
    if not checkpoint_path.is_file():
        raise FileNotFoundError("The completed Curtin preparation pilot checkpoint is missing.")
    connection = sqlite3.connect(
        checkpoint_path.resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("input_signature") != input_signature:
            raise ValueError("The pilot checkpoint no longer matches package/database inputs.")
        counts = dict(connection.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status"))
        total = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if total == 0 or counts.get("completed", 0) != total:
            raise ValueError("The pilot checkpoint has failed, pending, or running tasks.")
        expected = {
            f"search:Curtin:{scenario_id}:{index:02d}"
            for scenario_id, specs in PILOT_SEARCH_PLAN.items()
            for index in range(1, len(specs) + 1)
        }
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT task_id FROM tasks WHERE task_type='search_query' AND status='completed'"
            )
        }
        if not expected.issubset(actual):
            raise ValueError("The completed pilot checkpoint is missing one or more Curtin searches.")
    finally:
        connection.close()
    return checkpoint_path


def _pilot_search_result(path: Path, scenario_id: str, query_index: int) -> dict[str, Any]:
    task_id = f"search:Curtin:{scenario_id}:{query_index:02d}"
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status, result_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if not row or row[0] != "completed" or not row[1]:
            raise ValueError(f"Pilot search result is missing: {task_id}")
        return json.loads(row[1])
    finally:
        connection.close()


def _source_signature(root: Path, input_signature: str, protocol_path: Path) -> str:
    names = (
        "policy_index.csv",
        "source_audit.csv",
        "search_log.csv",
        "evidence_candidates.csv",
    )
    pilot = root / "data" / "preparation"
    hashes = {
        name: hashlib.sha256((pilot / name).read_bytes()).hexdigest()
        for name in names
    }
    hashes["pilot_checkpoint"] = hashlib.sha256(
        (pilot / ".preparation_checkpoint.sqlite3").read_bytes()
    ).hexdigest()
    hashes["protocol"] = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    return _sha256_bytes(_json({"input_signature": input_signature, "reused_files": hashes}).encode())


def _candidate_id(
    university: str, scenario_id: str, source_id: str, viewer_page: int, line_start: str
) -> str:
    value = f"{university}|{scenario_id}|{source_id}|{viewer_page}|{line_start}"
    return "C-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:14]


def _normalise_candidate(
    row: dict[str, Any], index_by_key: dict[tuple[str, int], dict[str, Any]]
) -> dict[str, Any]:
    university = str(row["university"])
    viewer_page = int(row["viewer_page"])
    index = index_by_key.get((university, viewer_page), {})
    flags = set(filter(None, re.split(r";\s*", str(row.get("page_review_flags", "")))))
    flags.update(index.get("extraction_flags", []))
    line_start = str(row["line_start"])
    scenario_id = str(row["scenario_id"])
    source_id = str(row["source_id"])
    status = (
        "Unreviewed candidate copied from PDF-verified indexed lines; not confirmed evidence."
    )
    if "scrambled_reading_order" in flags or "ambiguous_table" in flags:
        status += " Visual verification is required before quoting."
    return {
        "candidate_id": _candidate_id(
            university, scenario_id, source_id, viewer_page, line_start
        ),
        "university": university,
        "scenario_id": scenario_id,
        "query_ids": row.get("query_ids", row.get("query_id", "")),
        "search_categories": row.get("search_categories", row.get("search_category", "")),
        "search_terms": row.get("search_terms", ""),
        "source_id": source_id,
        "document_title": row["document_title"],
        "viewer_page": viewer_page,
        "printed_bundle_page": row.get("printed_bundle_page", ""),
        "line_start": line_start,
        "line_end": str(row.get("line_end", line_start)),
        "candidate_text": str(row["candidate_text"]),
        "context_before": str(row.get("context_before", "")),
        "context_after": str(row.get("context_after", "")),
        "page_review_flags": "; ".join(sorted(flags)),
        "candidate_status": status,
        "human_verified": "No",
        "rating": "",
    }


def _candidate_rows_for_query(
    university: str,
    scenario_id: str,
    query_id: str,
    category: str,
    query: str,
    hits: list[sqlite3.Row],
    index_by_key: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    query_tokens = {
        token.casefold()
        for token in re.findall(r"[\w'-]+", query, flags=re.UNICODE)
    }
    ranked: list[tuple[int, int, int, str, dict[str, Any]]] = []
    for hit in hits:
        viewer_page = int(hit["viewer_page"])
        page = index_by_key.get((university, viewer_page))
        if not page or page.get("mapping_status") != "verified":
            continue
        source_id = str(page.get("source_id", ""))
        title = str(page.get("document_title", ""))
        printed_page = page.get("printed_bundle_page", "")
        if not source_id or not title or printed_page in ("", None):
            continue
        lines = page.get("line_text_json", {})
        if not isinstance(lines, dict) or not lines:
            continue
        for label, text in lines.items():
            if not isinstance(text, str) or not text.strip():
                continue
            line_tokens = {
                token.casefold()
                for token in re.findall(r"[\w'-]+", text, flags=re.UNICODE)
            }
            matched = len(query_tokens & line_tokens)
            if not matched:
                continue
            exact_text = indexed_quote(lines, label, label)
            if exact_text != text.strip():
                continue
            line_number = int(label[1:]) if re.fullmatch(r"L\d{3}", label) else 0
            before = lines.get(f"L{line_number - 1:03d}", "") if line_number > 1 else ""
            after = lines.get(f"L{line_number + 1:03d}", "")
            candidate = {
                "university": university,
                "scenario_id": scenario_id,
                "query_id": query_id,
                "search_category": category,
                "search_terms": query,
                "source_id": source_id,
                "document_title": title,
                "viewer_page": viewer_page,
                "printed_bundle_page": printed_page,
                "line_start": label,
                "line_end": label,
                "candidate_text": exact_text,
                "context_before": before or str(page.get("context_before", ""))[-500:],
                "context_after": after or str(page.get("context_after", ""))[:500],
                "page_review_flags": "; ".join(page.get("extraction_flags", [])),
                "candidate_status": (
                    "Unreviewed candidate copied from PDF-verified indexed lines; not confirmed evidence."
                ),
                "human_verified": "No",
                "rating": "",
            }
            ranked.append(
                (-matched, viewer_page, line_number, source_id, candidate)
            )
    ranked.sort(key=lambda item: item[:4])
    return [item[-1] for item in ranked[:MAX_CANDIDATES_PER_QUERY]]


def _execute_search(
    connection: sqlite3.Connection,
    run_id: str,
    university: str,
    scenario_id: str,
    query_id: str,
    category: str,
    query: str,
    index_by_key: dict[tuple[str, int], dict[str, Any]],
    *,
    search_origin: str,
) -> dict[str, Any]:
    hits = page_search(connection, university, query, SEARCH_LIMIT)
    timestamp = _utc_now()
    capped = len(hits) >= SEARCH_LIMIT
    result_status = "capped_at_search_limit" if capped else "all_index_hits_returned"
    rows: list[dict[str, Any]] = []
    for rank, hit in enumerate(hits, start=1):
        page = index_by_key.get((university, int(hit["viewer_page"])), {})
        rows.append({
            "run_id": run_id,
            "searched_at": timestamp,
            "university": university,
            "scenario_id": scenario_id,
            "query_id": query_id,
            "category": category,
            "query": query,
            "result_rank": rank,
            "result_count": len(hits),
            "source_id": hit["source_id"] or "",
            "document_title": hit["title"] or "",
            "viewer_page": hit["viewer_page"],
            "printed_bundle_page": hit["bundle_page"] or "",
            "visible_line_labels": ", ".join(page.get("visible_line_labels", [])),
            "search_snippet": hit["snippet"],
            "review_flags": "; ".join(page.get("extraction_flags", [])),
            "result_status": result_status,
            "search_origin": search_origin,
        })
    if not rows:
        rows.append({
            "run_id": run_id,
            "searched_at": timestamp,
            "university": university,
            "scenario_id": scenario_id,
            "query_id": query_id,
            "category": category,
            "query": query,
            "result_rank": "",
            "result_count": 0,
            "source_id": "",
            "document_title": "",
            "viewer_page": "",
            "printed_bundle_page": "",
            "visible_line_labels": "",
            "search_snippet": "",
            "review_flags": "",
            "result_status": "no_index_hits",
            "search_origin": search_origin,
        })
    return {
        "search_rows": rows,
        "candidate_rows": _candidate_rows_for_query(
            university, scenario_id, query_id, category, query, hits, index_by_key
        ),
    }


def _progress_snapshot(
    checkpoint: Checkpoint, started: float | None = None
) -> dict[str, Any]:
    tasks = checkpoint.connection.execute(
        "SELECT status FROM tasks"
    ).fetchall()
    counts = Counter(row["status"] for row in tasks)
    total = len(tasks)
    finished = counts["completed"] + counts["failed"]
    started_at = checkpoint.get_metadata("run_started_at") or _utc_now()
    elapsed = max(0.0, time.time() - started) if started is not None else 0.0
    attempt_rows = checkpoint.connection.execute(
        "SELECT started_at, finished_at FROM task_attempts WHERE outcome='completed'"
    ).fetchall()
    durations: list[float] = []
    for attempt in attempt_rows:
        try:
            start = datetime.fromisoformat(attempt["started_at"]).timestamp()
            end = datetime.fromisoformat(attempt["finished_at"]).timestamp()
        except (TypeError, ValueError):
            continue
        if end >= start:
            durations.append(end - start)
    eta_seconds: int | None = None
    if total > finished:
        average = (
            median(durations)
            if durations
            else elapsed / finished if finished else 0.0
        )
        if average > 0:
            eta_seconds = int(average * (total - finished))
    percent = 100.0 * finished / total if total else 0.0
    successful_percent = 100.0 * counts["completed"] / total if total else 0.0
    bar_size = 30
    filled = round(bar_size * finished / total) if total else 0
    bar = "[" + "#" * filled + "-" * (bar_size - filled) + "]"
    state = (
        "complete"
        if total and counts["completed"] == total
        else "incomplete"
    )
    return {
        "updated_at": _utc_now(),
        "run_started_at": started_at,
        "status": state,
        "bar": bar,
        "finished_percent": round(percent, 1),
        "successful_percent": round(successful_percent, 1),
        "completed": counts["completed"],
        "failed": counts["failed"],
        "pending": counts["pending"],
        "running": counts["running"],
        "total": total,
        "eta_seconds": eta_seconds,
    }


def _write_progress(root: Path, checkpoint: Checkpoint, started: float | None = None) -> None:
    snapshot = _progress_snapshot(checkpoint, started)
    output = root / "data" / "preparation" / OUTPUT_DIR_NAME
    failures = json.loads(checkpoint.get_metadata("progress_write_failures") or "[]")
    try:
        _atomic_replace(
            output / "progress.json",
            (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            allow_replace=True,
        )
        eta = (
            "estimating"
            if snapshot["eta_seconds"] is None
            else _format_duration(snapshot["eta_seconds"])
        )
        text = [
            "MAIN DATASET PREPARATION PROGRESS",
            f"{snapshot['bar']} {snapshot['finished_percent']:.1f}% handled",
            f"Successful tasks: {snapshot['completed']} / {snapshot['total']} "
            f"({snapshot['successful_percent']:.1f}%)",
            f"Failed: {snapshot['failed']} | Pending: {snapshot['pending']} | Running: {snapshot['running']}",
            f"Estimated remaining: {eta}",
            f"Updated: {snapshot['updated_at']}",
            "Refresh this file or progress.json for the latest saved status.",
        ]
        _atomic_replace(
            output / "progress.txt",
            ("\n".join(text) + "\n").encode("utf-8"),
            allow_replace=True,
        )
    except OSError as exc:
        failures.append(f"{_utc_now()} {type(exc).__name__}: {exc}")
        checkpoint.set_metadata("progress_write_failures", json.dumps(failures))
    checkpoint.set_metadata(
        "last_progress_snapshot", json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    )


def _format_duration(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def _run_task(
    checkpoint: Checkpoint,
    task_id: str,
    operation: Callable[[], Any],
    root: Path,
    started: float,
) -> dict[str, Any] | None:
    task = checkpoint.task(task_id)
    if task["status"] == "completed":
        return checkpoint.result(task_id)
    if task["status"] == "failed":
        return None
    while True:
        task = checkpoint.task(task_id)
        if int(task["attempts"]) > MAX_RETRIES:
            checkpoint.mark_failed_without_retry(
                task_id, task["error_text"] or "Retry limit already reached."
            )
            _write_progress(root, checkpoint, started)
            return None
        attempt = checkpoint.begin_attempt(task_id)
        try:
            result = operation()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            persistent = checkpoint.fail_attempt(task_id, attempt, error)
            _write_progress(root, checkpoint, started)
            if persistent:
                return None
        else:
            checkpoint.complete(task_id, attempt, result)
            _write_progress(root, checkpoint, started)
            return result


def _write_task_output(
    root: Path,
    checkpoint: Checkpoint,
    task_id: str,
    path: Path,
    content: bytes,
    started: float,
) -> None:
    expected_hash = _sha256_bytes(content)
    task = checkpoint.task(task_id)
    if task["status"] == "completed":
        saved = checkpoint.result(task_id) or {}
        if path.is_file() and saved.get("sha256") == expected_hash == hashlib.sha256(path.read_bytes()).hexdigest():
            return
        checkpoint.mark_failed_without_retry(
            task_id, "Previously completed output is missing or changed; it was not overwritten."
        )
        _write_progress(root, checkpoint, started)
        return

    def operation() -> dict[str, Any]:
        if path.exists():
            existing_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing_hash == expected_hash:
                return {"path": str(path.name), "sha256": expected_hash, "reused_existing": True}
            previous = checkpoint.result(task_id) or {}
            if previous.get("sha256") != existing_hash:
                raise FileExistsError(
                    f"Existing output is not the previously checkpointed version; refusing to overwrite: {path.name}"
                )
            _atomic_replace(path, content, allow_replace=True)
            return {"path": str(path.name), "sha256": expected_hash, "reused_existing": False}
        _atomic_replace(path, content, allow_replace=False)
        return {"path": str(path.name), "sha256": expected_hash, "reused_existing": False}

    _run_task(checkpoint, task_id, operation, root, started)


def _register_tasks(
    checkpoint: Checkpoint,
    index_rows: list[dict[str, Any]],
) -> None:
    for row in index_rows:
        if "ambiguous_table" in row["extraction_flags"]:
            university = row["university"]
            viewer_page = int(row["viewer_page"])
            checkpoint.add_task(
                f"table_review:{university}:{viewer_page:04d}",
                "table_reading_order_review",
                f"{university}/viewer-{viewer_page}",
            )
    for university in UNIVERSITIES:
        for scenario_id in SCENARIO_IDS:
            for query_index, (category, _) in enumerate(_search_plan(scenario_id), start=1):
                checkpoint.add_task(
                    f"search:{university}:{scenario_id}:{query_index:02d}",
                    "scenario_search",
                    f"{university}/{scenario_id}/{category}",
                )
            checkpoint.add_task(
                f"handoff:{university}:{scenario_id}",
                "ai_handoff",
                f"{university}/{scenario_id}",
            )
    for filename, task_type in (
        ("policy_index.csv", "policy_index_export"),
        ("source_audit.csv", "source_audit_export"),
        ("search_log.csv", "search_log_export"),
        ("evidence_candidates.csv", "candidate_export"),
        ("proposal_return_schema.json", "proposal_schema_export"),
        ("additional_search_request_schema.json", "additional_search_schema_export"),
        ("additional_search_requests.json", "additional_search_template_export"),
        ("ANTIGRAVITY_START_HERE.txt", "antigravity_instruction_export"),
        ("preparation_validation_report.txt", "final_report_export"),
    ):
        checkpoint.add_task(f"output:{filename}", task_type, filename)


def _migrate_main_checkpoint(checkpoint_path: Path, signature: str) -> None:
    if not checkpoint_path.is_file():
        return
    connection = sqlite3.connect(checkpoint_path, timeout=30)
    try:
        check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(
                f"Main preparation checkpoint failed integrity check: {check}"
            )
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        version = metadata.get("plan_version")
        if version not in {"1", "2", "3"}:
            return
        if metadata.get("input_signature") != signature:
            raise ValueError(
                "Main preparation inputs changed; refusing to migrate this checkpoint."
            )
        affected = (
            """task_id LIKE 'search:%'
            OR task_id LIKE 'handoff:%'
            OR task_id IN (
                'output:search_log.csv',
                'output:evidence_candidates.csv',
                'output:preparation_validation_report.txt'
            )"""
            if version in {"1", "2"}
            else """task_id LIKE 'handoff:%'
            OR task_id='output:preparation_validation_report.txt'"""
        )
        connection.execute(
            """UPDATE tasks
            SET status='pending', attempts=0, completed_at='', error_text=''
            WHERE """ + affected
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='plan_version'",
            (MAIN_PLAN_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()


def _reuse_pilot_searches(
    root: Path,
    checkpoint: Checkpoint,
    pilot_checkpoint: Path,
    started: float,
) -> None:
    for scenario_id, specs in PILOT_SEARCH_PLAN.items():
        for query_index, _ in enumerate(specs, start=1):
            result = _pilot_search_result(pilot_checkpoint, scenario_id, query_index)
            for row in result["search_rows"]:
                row["search_origin"] = "reused_from_completed_Curtin_pilot"
            for row in result["candidate_rows"]:
                row["university"] = "Curtin"
            new_task_id = f"search:Curtin:{scenario_id}:{query_index:02d}"
            if checkpoint.task(new_task_id)["status"] == "completed":
                continue
            attempt = checkpoint.begin_attempt(new_task_id)
            checkpoint.complete(new_task_id, attempt, result)
            _write_progress(root, checkpoint, started)


def _table_review_task(
    root: Path,
    checkpoint: Checkpoint,
    row: dict[str, Any],
    started: float,
) -> None:
    university = row["university"]
    viewer_page = int(row["viewer_page"])
    task_id = f"table_review:{university}:{viewer_page:04d}"
    pdfs = list((root / "data" / "raw" / university).glob("*.pdf"))
    if len(pdfs) != 1:
        _run_task(
            checkpoint,
            task_id,
            lambda: (_ for _ in ()).throw(
                FileNotFoundError(f"{university}: expected one PDF; found {len(pdfs)}.")
            ),
            root,
            started,
        )
        return

    def operation() -> dict[str, Any]:
        with fitz.open(pdfs[0]) as document:
            if viewer_page < 1 or viewer_page > len(document):
                raise IndexError(f"{university} PDF has no viewer page {viewer_page}.")
            return {"flags": _table_flags(document[viewer_page - 1])}

    _run_task(checkpoint, task_id, operation, root, started)


def _updated_index_rows(
    checkpoint: Checkpoint, index_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for old in index_rows:
        row = dict(old)
        if "ambiguous_table" in row["extraction_flags"]:
            task_id = f"table_review:{row['university']}:{int(row['viewer_page']):04d}"
            task = checkpoint.task(task_id)
            if task["status"] == "completed":
                table_flags = (checkpoint.result(task_id) or {}).get("flags", [])
                row["extraction_flags"] = sorted(set(row["extraction_flags"]) | set(table_flags))
            elif task["status"] == "failed":
                row["extraction_flags"] = sorted(
                    set(row["extraction_flags"]) | {"table_reading_order_check_failed"}
                )
            else:
                row["extraction_flags"] = sorted(
                    set(row["extraction_flags"]) | {"table_reading_order_check_pending"}
                )
        result.append(row)
    return result


def _index_maps(
    rows: list[dict[str, Any]]
) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        row["viewer_page"] = int(row["viewer_page"])
        row["printed_bundle_page"] = (
            int(row["printed_bundle_page"])
            if str(row["printed_bundle_page"]).isdigit()
            else ""
        )
        labels = row.get("visible_line_labels", [])
        row["visible_line_labels"] = (
            labels.split(", ")
            if isinstance(labels, str) and labels
            else list(labels) if isinstance(labels, list) else []
        )
        line_text = row.get("line_text_json", {})
        row["line_text_json"] = (
            json.loads(line_text)
            if isinstance(line_text, str) and line_text
            else dict(line_text) if isinstance(line_text, dict) else {}
        )
        row["extraction_flags"] = (
            [flag.strip() for flag in row["extraction_flags"].split(";") if flag.strip()]
            if isinstance(row["extraction_flags"], str)
            else list(row["extraction_flags"])
        )
        result[(row["university"], row["viewer_page"])] = row
    return result


def _query_task(
    connection: sqlite3.Connection,
    checkpoint: Checkpoint,
    root: Path,
    started: float,
    run_id: str,
    university: str,
    scenario_id: str,
    query_index: int,
    category: str,
    query: str,
    index_by_key: dict[tuple[str, int], dict[str, Any]],
) -> None:
    task_id = f"search:{university}:{scenario_id}:{query_index:02d}"
    if checkpoint.task(task_id)["status"] == "completed":
        return
    if (
        university == "Curtin"
        and scenario_id in PILOT_SEARCH_PLAN
        and query_index <= len(PILOT_SEARCH_PLAN[scenario_id])
    ):
        return

    def operation() -> dict[str, Any]:
        return _execute_search(
            connection,
            run_id,
            university,
            scenario_id,
            f"{scenario_id}-Q{query_index:02d}",
            category,
            query,
            index_by_key,
            search_origin="executed_in_main_run",
        )

    _run_task(checkpoint, task_id, operation, root, started)


def _materialize_searches(
    checkpoint: Checkpoint,
    *,
    include_university: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    search_rows: list[dict[str, Any]] = []
    candidate_map: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}
    universities = (include_university,) if include_university else UNIVERSITIES
    for university in universities:
        for scenario_id in SCENARIO_IDS:
            for query_index, (category, query) in enumerate(_search_plan(scenario_id), start=1):
                task_id = f"search:{university}:{scenario_id}:{query_index:02d}"
                task = checkpoint.task(task_id)
                if task["status"] == "completed" and task["result_json"]:
                    result = checkpoint.result(task_id) or {}
                    search_rows.extend(result.get("search_rows", []))
                    for candidate in result.get("candidate_rows", []):
                        key = (
                            candidate["university"],
                            candidate["scenario_id"],
                            candidate["source_id"],
                            int(candidate["viewer_page"]),
                            candidate["line_start"],
                        )
                        if key not in candidate_map:
                            candidate_map[key] = candidate
                        else:
                            old = candidate_map[key]
                            old["query_id"] += ", " + candidate["query_id"]
                            old["search_category"] += ", " + candidate["search_category"]
                            old["search_terms"] += " | " + candidate["search_terms"]
                elif task["status"] in {"failed", "pending", "running"}:
                    search_rows.append({
                        "run_id": checkpoint.get_metadata("input_signature"),
                        "searched_at": "",
                        "university": university,
                        "scenario_id": scenario_id,
                        "query_id": f"{scenario_id}-Q{query_index:02d}",
                        "category": category,
                        "query": query,
                        "result_rank": "",
                        "result_count": "",
                        "source_id": "",
                        "document_title": "",
                        "viewer_page": "",
                        "printed_bundle_page": "",
                        "visible_line_labels": "",
                        "search_snippet": "",
                        "review_flags": "",
                        "result_status": (
                            f"search_task_{task['status']}"
                            + (f": {task['error_text']}" if task["error_text"] else "")
                        ),
                        "search_origin": (
                            "reused_from_completed_Curtin_pilot"
                            if university == "Curtin" and scenario_id in PILOT_SEARCH_PLAN
                            else "executed_in_main_run"
                        ),
                    })
    candidates = [
        _normalise_candidate(candidate, {})
        for candidate in candidate_map.values()
    ]
    candidates.sort(
        key=lambda row: (
            UNIVERSITY_ORDER.index(row["university"]),
            _scenario_number(row["scenario_id"]),
            row["source_id"],
            int(row["viewer_page"]),
            row["line_start"],
        )
    )
    search_rows.sort(
        key=lambda row: (
            UNIVERSITY_ORDER.index(row["university"]),
            _scenario_number(row["scenario_id"]),
            int(row["query_id"].split("-Q")[-1]),
            int(row["result_rank"]) if str(row["result_rank"]).isdigit() else 0,
        )
    )
    return search_rows, candidates


def _merge_candidate_flags(
    candidates: list[dict[str, Any]],
    index_by_key: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _normalise_candidate(candidate, index_by_key)
        for candidate in candidates
    ]


def _display_university(university: str) -> str:
    return "Notre Dame" if university == "Notre_Dame" else university


def _proposal_schema() -> dict[str, Any]:
    version_location_schema = {
        "type": "object",
        "required": [
            "source_id",
            "document_title",
            "printed_bundle_page",
            "viewer_page",
            "line_start",
            "line_end",
            "evidence_type",
            "text",
        ],
        "properties": {
            "source_id": {"type": "string", "minLength": 1},
            "document_title": {"type": "string", "minLength": 1},
            "printed_bundle_page": {"type": "integer", "minimum": 1},
            "viewer_page": {"type": "integer", "minimum": 1},
            "line_start": {"type": "string", "pattern": "^L[0-9]{3}$"},
            "line_end": {"type": "string", "pattern": "^L[0-9]{3}$"},
            "evidence_type": {"enum": ["Exact quotation", "Paraphrase"]},
            "text": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }
    passage_schema = {
        "type": "object",
        "required": [
            "source_id",
            "document_title",
            "printed_bundle_page",
            "viewer_page",
            "line_start",
            "line_end",
            "evidence_type",
            "text",
            "evidence_role",
            "operative_at_cutoff",
            "version_evidence",
            "version_evidence_location",
            "visual_reading_order_checked",
            "visual_review_note",
        ],
        "properties": {
            "source_id": {"type": "string", "minLength": 1},
            "document_title": {"type": "string", "minLength": 1},
            "printed_bundle_page": {"type": "integer", "minimum": 1},
            "viewer_page": {"type": "integer", "minimum": 1},
            "line_start": {"type": "string", "pattern": "^L[0-9]{3}$"},
            "line_end": {"type": "string", "pattern": "^L[0-9]{3}$"},
            "evidence_type": {"enum": ["Exact quotation", "Paraphrase"]},
            "text": {"type": "string", "minLength": 1},
            "evidence_role": {"enum": ["MAIN", "SUPPORTING"]},
            "candidate_id": {"type": "string"},
            "query_ids": {"type": "array", "items": {"type": "string"}},
            "operative_at_cutoff": {"type": "boolean"},
            "version_evidence": {"type": "string"},
            "version_evidence_location": {
                "oneOf": [version_location_schema, {"type": "null"}]
            },
            "visual_reading_order_checked": {"type": "boolean"},
            "visual_review_note": {"type": "string"},
        },
        "additionalProperties": False,
    }
    dimension_schema = {
        "type": "object",
        "required": [
            "dimension",
            "score",
            "reason",
            "search_terms_used",
            "documents_checked",
            "evidence",
            "zero_score_basis",
            "uncertainty_notes",
        ],
        "properties": {
            "dimension": {"enum": list(DIMENSIONS)},
            "score": {
                "oneOf": [
                    {"type": "integer", "minimum": 0, "maximum": 3},
                    {"type": "null"},
                ]
            },
            "reason": {"type": "string"},
            "search_terms_used": {"type": "array", "items": {"type": "string"}},
            "documents_checked": {"type": "array", "items": passage_schema},
            "evidence": {"type": "array", "items": passage_schema},
            "zero_score_basis": {
                "enum": [
                    "not_applicable",
                    "no_applicable_provision_found",
                    "no_decision_maker_identified",
                    "direct_unresolved_contradiction",
                    "no_actionable_route",
                    "no_relevant_safeguards",
                    "no_relevant_mechanism",
                ]
            },
            "uncertainty_notes": {"type": "string"},
        },
        "additionalProperties": False,
    }
    additional_request_schema = {
        "type": "object",
        "required": [
            "request_id",
            "university",
            "case_id",
            "scenario_id",
            "query",
            "category",
            "rationale",
        ],
        "properties": {
            "request_id": {"type": "string", "minLength": 1},
            "university": {"enum": list(UNIVERSITIES)},
            "case_id": {"type": "string"},
            "scenario_id": {"enum": list(SCENARIO_IDS)},
            "query": {"type": "string", "minLength": 1},
            "category": {"type": "string", "minLength": 1},
            "rationale": {"type": "string", "minLength": 1},
            "requested_by": {"type": "string"},
        },
        "additionalProperties": False,
    }
    case_summary_schema = {
        "type": "object",
        "required": [
            "mode",
            "review_route",
            "review_route_explanation",
            "critical_gap",
            "critical_risk_criterion",
            "critical_gap_explanation",
            "evidence_status",
            "insufficient_evidence_reason",
            "rule_direction",
            "conflict_confirmed",
            "conflict_basis",
            "case_note",
            "supporting_passages",
            "review_route_evidence",
        ],
        "properties": {
            "mode": {"enum": ["Prescribing", "Delegating", "None"]},
            "review_route": {"enum": ["Yes", "No", "Uncertain"]},
            "review_route_explanation": {"type": "string"},
            "critical_gap": {"enum": ["Yes", "No", "Uncertain"]},
            "critical_risk_criterion": {"type": "string"},
            "critical_gap_explanation": {"type": "string"},
            "evidence_status": {"enum": ["Sufficient", "Insufficient"]},
            "insufficient_evidence_reason": {"type": "string"},
            "rule_direction": {
                "enum": ["Permission", "Prohibition", "Conditional", "Unclear"]
            },
            "conflict_confirmed": {"type": "boolean"},
            "conflict_basis": {"type": "string"},
            "case_note": {"type": "string"},
            "supporting_passages": {"type": "array", "items": passage_schema},
            "review_route_evidence": {
                "oneOf": [passage_schema, {"type": "null"}]
            },
            "search_terms_used": {"type": "array", "items": {"type": "string"}},
            "documents_checked": {"type": "array", "items": passage_schema},
            "uncertainty_notes": {"type": "string"},
        },
        "additionalProperties": False,
    }
    proposal_schema = {
        "type": "object",
        "required": [
            "university",
            "case_id",
            "scenario_id",
            "coding_date",
            "protocol_version",
            "coder_id",
            "human_confirmed",
            "dimensions",
            "case_summary",
            "additional_search_requests",
        ],
        "properties": {
            "university": {"enum": list(UNIVERSITIES)},
            "case_id": {"type": "string"},
            "scenario_id": {"enum": list(SCENARIO_IDS)},
            "coding_date": {"type": "string", "format": "date"},
            "protocol_version": {"const": "1.1"},
            "coder_id": {
                "const": "Antigravity AI proposal — awaiting human verification"
            },
            "human_confirmed": {"const": False},
            "dimensions": {
                "type": "array",
                "minItems": 6,
                "maxItems": 6,
                "items": dimension_schema,
            },
            "case_summary": case_summary_schema,
            "additional_search_requests": {
                "type": "array",
                "items": additional_request_schema,
            },
        },
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Human-unconfirmed policy coding proposals",
        "description": (
            "AI proposals only. Python must validate citations and fields. "
            "Never treat this data as human-confirmed coding."
        ),
        "type": "object",
        "required": ["schema_version", "proposals"],
        "properties": {
            "schema_version": {"const": "1.0"},
            "proposals": {
                "type": "array",
                "minItems": 75,
                "maxItems": 75,
                "items": proposal_schema,
            },
        },
        "$defs": {
            "additional_search_request": additional_request_schema,
            "version_evidence_location": version_location_schema,
        },
        "additionalProperties": False,
    }


def _additional_request_file_schema() -> dict[str, Any]:
    request_schema = _proposal_schema()["$defs"]["additional_search_request"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Additional local policy searches",
        "type": "object",
        "required": ["schema_version", "requests"],
        "properties": {
            "schema_version": {"const": "1.0"},
            "requests": {"type": "array", "items": request_schema},
        },
        "additionalProperties": False,
        "$defs": {"additional_search_request": request_schema},
    }


def _source_audit_text(row: dict[str, str]) -> str:
    return (
        f"{row.get('source_id','')} | {row.get('title','')} | "
        f"effective_date={row.get('effective_date') or 'unknown/not recorded'} "
        f"({row.get('effective_date_status') or 'unreviewed'}) | "
        f"operative_version={row.get('operative_version_at_freeze') or 'unknown/not recorded'} "
        f"({row.get('operative_version_status') or 'unreviewed'}) | "
        f"authority={row.get('authority') or 'unknown/not recorded'} "
        f"({row.get('authority_status') or 'unreviewed'}) | "
        f"supersession={row.get('supersedes_or_duplicate_of') or 'not recorded'} "
        f"({row.get('supersession_status') or 'unreviewed'}) | "
        f"missing_linked_instruments={row.get('missing_linked_instrument_status') or 'unreviewed'} | "
        f"unresolved_links={row.get('unresolved_link_references') or 'none recorded'}"
    )


def _has_reportable_link_gap(row: dict[str, str]) -> bool:
    references = str(row.get("unresolved_link_references", "")).strip().casefold()
    if references and references not in {
        "0", "[]", "none", "none recorded", "no unresolved links", "not applicable"
    }:
        return True
    status = str(row.get("missing_linked_instrument_status", "")).strip().casefold()
    normalized = re.sub(r"[_-]+", " ", status)
    if not normalized or any(
        phrase in normalized
        for phrase in (
            "no explicit missing link detected",
            "none detected",
            "no unresolved links",
            "not applicable",
            "completeness not established",
            "unknown",
            "not recorded",
        )
    ):
        return False
    return bool(re.search(r"\b(missing|unresolved)\b", normalized))


def _handoff_text(
    university: str,
    scenario_id: str,
    resources: dict[str, dict[str, Any]],
    protocol: dict[str, Any],
    dimension_names: dict[str, str],
    search_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    audit_rows: list[dict[str, str]],
    table_review_failures: list[str],
    policy_index_path: str,
) -> str:
    scenario = resources[university]["scenarios"][scenario_id]
    criteria = {
        dimension: dimension_score_criteria(
            resources[university]["codebook_text"], dimension
        )
        for dimension in DIMENSIONS
    }
    case_candidates = [
        row
        for row in candidates
        if row["university"] == university and row["scenario_id"] == scenario_id
    ]
    case_searches = [
        row
        for row in search_rows
        if row["university"] == university and row["scenario_id"] == scenario_id
    ]
    referenced_source_ids = {
        row["source_id"] for row in case_candidates if row.get("source_id")
    }
    referenced_source_ids.update(
        row["source_id"]
        for row in case_searches
        if row.get("source_id")
    )
    university_audit = [row for row in audit_rows if row["university"] == university]
    relevant_audit = [
        row for row in university_audit if row["source_id"] in referenced_source_ids
    ]
    missing_rows = [
        row for row in university_audit
        if _has_reportable_link_gap(row)
    ]
    display = _display_university(university)
    lines = [
        "ANTIGRAVITY EVIDENCE-PREPARATION HANDOFF",
        "Preparation only: candidates are not confirmed evidence; no ratings were assigned.",
        f"University: {display}",
        f"Case ID: {university}-{scenario_id}",
        f"Scenario: {scenario_id} — {scenario['title']}",
        f"Policy cut-off: {POLICY_CUTOFF}",
        "Protocol: v1.1; adopted after preliminary review on 2026-11-15.",
        "Protocol status: " + str(protocol.get("status", "unverified")),
        "Retrieval dates do not establish effective dates or operative-at-cutoff status.",
        "Searches are bounded lexical searches on this university's local full-text index; they are not exhaustive semantic review.",
        "Table-review task failures: "
        + (
            ", ".join(table_review_failures)
            if table_review_failures
            else "none recorded"
        ),
        "",
        "FULL SCENARIO (from the supplied Scenarios.docx):",
        str(scenario["text"]),
        "",
        "SUPPLIED CODEBOOK SCORING DEFINITIONS (extracted from Human_Codebook.docx):",
    ]
    for dimension in DIMENSIONS:
        lines.append(f"{dimension} — {dimension_names[dimension]}")
        lines.extend(f"  {item}" for item in criteria[dimension])
    lines.extend([
        "",
        "CRITICAL-RISK CRITERION (Protocol v1.1):",
        str(protocol["critical_risk_criteria"][scenario_id]),
        "The protocol amendment was adopted after preliminary review; disclose this timing in any proposal.",
        "",
        "SEARCH ACTIVITY ACTUALLY EXECUTED:",
    ])
    first_per_query: set[str] = set()
    for row in case_searches:
        query_id = row["query_id"]
        if query_id in first_per_query:
            continue
        first_per_query.add(query_id)
        if str(row.get("result_rank")) not in ("", "1", "None"):
            continue
        lines.append(
            f"- {query_id} [{row.get('category','')}]: {row.get('query','')} — "
            f"{row.get('result_count','')} result(s); {row.get('result_status','')}; "
            f"origin={row.get('search_origin','')}"
        )
    lines.extend([
        "",
        "SOURCE-VERSION AUDIT (candidate/search-hit sources):",
        "Use source_audit.csv for all register entries; unknown values remain unknown.",
    ])
    if relevant_audit:
        lines.extend("- " + _source_audit_text(row) for row in relevant_audit)
    else:
        lines.append("- No source audit row is linked to a search hit or candidate.")
    lines.append("Unresolved/missing linked instruments for this university:")
    if missing_rows:
        lines.extend("- " + _source_audit_text(row) for row in missing_rows)
    else:
        lines.append(
            "- No confirmed missing instrument or unresolved linked-source reference was recorded; "
            "linked-document completeness remains unverified where stated in source_audit.csv."
        )
    lines.extend([
        "",
        "CANDIDATE PASSAGES (unconfirmed; copy only after checking the original PDF):",
        "Every passage below carries its own source, title, printed page, viewer page, and existing L-labels.",
        "Do not replace a missing printed page or line label with a PDF viewer page or invented number.",
        "Pages flagged ambiguous_table or scrambled_reading_order require visual verification before quotation.",
    ])
    if not case_candidates:
        lines.append("- No line-located candidates passed the existing PDF-to-index mapping checks.")
    for row in case_candidates:
        lines.extend([
            "",
            f"Candidate ID: {row['candidate_id']}",
            f"Source ID: {row['source_id']}",
            f"Document title: {row['document_title']}",
            f"Printed bundle page: {row['printed_bundle_page'] or 'not extracted'}",
            f"PDF viewer page (navigation only): {row['viewer_page']}",
            f"Line numbers: {row['line_start']}–{row['line_end']}",
            f"Search query IDs: {row['query_ids']}",
            f"Search terms: {row['search_terms']}",
            f"Extraction/review flags: {row['page_review_flags'] or 'none recorded'}",
            f"Context before: {row['context_before'] or '[none extracted]'}",
            f"Candidate passage: {row['candidate_text']}",
            f"Context after: {row['context_after'] or '[none extracted]'}",
            f"Candidate status: {row['candidate_status']}",
            "Human verified: No | Rating: blank",
        ])
    lines.extend([
        "",
        "ADDITIONAL SEARCH REQUESTS:",
        "If a gap requires another query, add it to additional_search_requests.json using additional_search_request_schema.json.",
        "Do not claim an additional search was performed until Python executes it and writes an additional_search_results file.",
        "Execute requested searches in PowerShell:",
        r".\.venv\Scripts\python.exe -m policy_coder.main_preparation --root . --additional-search-requests <path-to-json>",
        "",
        "WIDER CORPUS:",
        f"- Full page index: {policy_index_path}",
        "- Filter by the exact university value; the index contains all five policy collections.",
        "- Search log contains every returned index hit for each logged query, including zero-hit queries.",
        "- Source audit and extraction flags are in the shared CSV files in this handoff folder.",
        "- PDFs are in data/raw/<University>/; use their original page images to verify quotations and table reading order.",
        "",
        "RETURN FORMAT:",
        "- Return one JSON object per each of the 75 cases in the top-level proposals array.",
        "- Follow proposal_return_schema.json exactly. Use score null if evidence does not support a proposal.",
        "- Keep coder_id as the specified AI-proposal label and human_confirmed=false.",
        "- Cite source ID, exact document title, printed bundle page, original viewer page, and existing L-line range for every passage.",
        "- Label each passage Exact quotation or Paraphrase. Never present a paraphrase as a quotation.",
        "- Set operative_at_cutoff=true only when version_evidence and its own source/page/line location are recorded; otherwise set false and leave status unverified.",
        "- State whether you visually checked table reading order, but do not claim human confirmation.",
        "- A score of 0 requires the dimension-specific zero_score_basis, actual searches, documents checked, and an explanation; missing evidence alone is not zero.",
        "- For D3=0, set conflict_confirmed=true only when two distinct operative provisions are incompatible and the conflict remains unresolved.",
        "- Record actual searches and documents checked; do not call the supplied search set exhaustive.",
        "- Keep unavailable instruments, unknown dates, extraction warnings, and uncertainty explicit.",
        "- Do not write to the coding database, populate human ratings, or mark proposals human-approved.",
    ])
    return "\n".join(lines) + "\n"


def _source_audit_status_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    return {
        "unknown_effective_date": sum(
            row.get("effective_date_status") == "unknown_not_recorded" for row in rows
        ),
        "unknown_operative_version": sum(
            row.get("operative_version_status") == "unknown_not_recorded" for row in rows
        ),
        "unknown_supersession": sum(
            row.get("supersession_status") == "unknown_not_recorded" for row in rows
        ),
        "unresolved_link_references": sum(
            bool(row.get("unresolved_link_references")) for row in rows
        ),
    }


def _final_report(
    checkpoint: Checkpoint,
    index_rows: list[dict[str, Any]],
    audit_rows: list[dict[str, str]],
    search_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    reused_searches: int,
    executed_searches: int,
    input_differences: list[str],
) -> str:
    task_rows = checkpoint.connection.execute(
        """SELECT task_id, task_type, scope, status, attempts, error_text FROM tasks
        WHERE task_type NOT LIKE 'additional_%'
        AND task_id NOT LIKE 'output:additional%'
        AND task_id != 'output:preparation_validation_report.txt'
        ORDER BY task_id"""
    ).fetchall()
    counts = Counter(row["status"] for row in task_rows)
    unknown = _source_audit_status_counts(audit_rows)
    failed_tasks = [
        row for row in task_rows if row["status"] == "failed"
    ]
    pending_tasks = [
        row for row in task_rows if row["status"] in {"pending", "running"}
    ]
    table_warnings = sum("ambiguous_table" in row["extraction_flags"] for row in index_rows)
    scrambled = sum("scrambled_reading_order" in row["extraction_flags"] for row in index_rows)
    no_lines = sum(
        "no_visible_L_labels_in_pdf" in row["extraction_flags"]
        for row in index_rows
    )
    status = (
        "complete"
        if counts["failed"] == counts["pending"] == counts["running"] == 0
        and not input_differences
        else "incomplete"
    )
    progress_write_failures = json.loads(
        checkpoint.get_metadata("progress_write_failures") or "[]"
    )
    return "\n".join([
        "MAIN DATASET PREPARATION VALIDATION REPORT",
        f"Run started: {checkpoint.get_metadata('run_started_at')}",
        f"Generated: {checkpoint.get_metadata('finalized_at') or _utc_now()}",
        f"Status: {status}",
        f"Policy cut-off: {POLICY_CUTOFF}",
        "No ratings, final policy judgments, or human approvals were created.",
        "",
        "TASK COUNTS:",
        f"Total tasks (excluding this report task): {len(task_rows)}",
        f"Completed: {counts['completed']}",
        f"Failed after up to {MAX_RETRIES} retries: {counts['failed']}",
        f"Pending: {counts['pending']}",
        f"Running at finalization: {counts['running']}",
        "Recorded task attempts: "
        + str(checkpoint.connection.execute(
            """SELECT COUNT(*) FROM task_attempts
            WHERE task_id != 'output:preparation_validation_report.txt'
            AND task_id NOT LIKE 'additional_%'
            AND task_id NOT LIKE 'output:additional%'"""
        ).fetchone()[0]),
        "",
        "REUSED PREPARATION:",
        f"Verified PDF page rows reused: {len(index_rows)}",
        f"Source audit rows reused: {len(audit_rows)}",
        f"Curtin pilot searches reused: {reused_searches}",
        f"New scenario searches executed: {executed_searches}",
        "Original five package ZIPs and extracted package files were hash-verified before and after the run.",
        "The policy coding database was opened read-only; existing human ratings and exports were not changed.",
        "",
        "INDEX AND EXTRACTION WARNINGS:",
        f"Indexed pages: {len(index_rows)}",
        f"Pages with existing visible line labels: {sum(bool(row.get('visible_line_labels')) for row in index_rows)}",
        f"Pages without visible L-labels: {no_lines}",
        f"Pages flagged ambiguous_table: {table_warnings}",
        f"Pages flagged scrambled_reading_order: {scrambled}",
        f"Table-reading-order review tasks failed: {sum(row['task_type'] == 'table_reading_order_review' and row['status'] == 'failed' for row in task_rows)}",
        "Printed bundle pages and PDF viewer pages remain separate; no L-labels were invented.",
        "Table warnings are not resolved by an index text match; original PDF visual inspection remains required.",
        "",
        "SOURCE AUDIT:",
        f"Source register rows: {len(audit_rows)}",
        f"Rows with unknown effective date: {unknown['unknown_effective_date']}",
        f"Rows with unknown operative version: {unknown['unknown_operative_version']}",
        f"Rows with unknown supersession status: {unknown['unknown_supersession']}",
        f"Rows with unresolved linked-source references: {unknown['unresolved_link_references']}",
        "Unknown, blank, duplicate, supersession, and missing-instrument fields were not guessed.",
        "Retrieval date was not used as an effective date.",
        "",
        "SEARCHES AND CANDIDATES:",
        "Cases prepared: 75 (5 universities x 15 scenarios).",
        f"Actual query tasks: {sum(row['task_type'] == 'scenario_search' for row in task_rows)}",
        f"Search result rows (including zero-hit records): {len(search_rows)}",
        f"Unreviewed candidate passages: {len(candidates)}",
        "Searches are reproducible, university-filtered lexical retrieval. A zero-hit query means no exact-token index hits, not proof that no evidence exists.",
        "No limited or lexical result set is reported as exhaustive semantic review.",
        "",
        "PROTOCOL:",
        "Critical-risk criteria came from Protocol v1.1 and are included in every case packet.",
        "The protocol file discloses adoption after preliminary review on 2026-11-15.",
        "Operative-at-cutoff status is not inferred from that amendment; source version evidence remains as recorded in source_audit.csv.",
        "",
        "INPUT CHANGES:",
        *(input_differences or ["- None."]),
        "",
        "PERSISTENT FAILURES:",
        *(
            [f"- {row['task_id']} ({row['attempts']} attempts): {row['error_text']}" for row in failed_tasks]
            if failed_tasks else ["- None recorded."]
        ),
        "",
        "PENDING TASKS:",
        *(
            [f"- {row['task_id']} [{row['task_type']}, {row['scope']}]" for row in pending_tasks]
            if pending_tasks else ["- None."]
        ),
        "",
        "PROGRESS REPORT WRITE FAILURES:",
        *(
            [f"- {value}" for value in progress_write_failures]
            if progress_write_failures else ["- None recorded."]
        ),
        "",
        "DELIVERABLES:",
        "- policy_index.csv (reused index plus targeted table-reading-order flags)",
        "- source_audit.csv (reused, unchanged source audit)",
        "- search_log.csv",
        "- evidence_candidates.csv (all ratings blank)",
        "- proposal_return_schema.json",
        "- additional_search_request_schema.json",
        "- additional_search_requests.json (empty request template)",
        "- ANTIGRAVITY_START_HERE.txt",
        "- AI_handoff_<University>_S1-S15.txt (75 case packets)",
        "- progress.txt and progress.json (refreshed after every task)",
        "- .main_dataset_checkpoint.sqlite3",
        "",
    ]) + "\n"


def _load_pilot_files(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]], Path]:
    pilot = root / "data" / "preparation"
    index_rows = _read_csv(pilot / "policy_index.csv")
    audit_rows = _read_csv(pilot / "source_audit.csv")
    expected_by_university = {
        "Curtin": 579,
        "ECU": 151,
        "Murdoch": 364,
        "Notre_Dame": 447,
        "UWA": 602,
    }
    index_counts = Counter(row["university"] for row in index_rows)
    if len(index_rows) != 2143 or dict(index_counts) != expected_by_university:
        raise ValueError(
            "The completed pilot index does not match the verified 2,143-page five-university index."
        )
    if len(audit_rows) != 221 or set(row["university"] for row in audit_rows) != set(UNIVERSITIES):
        raise ValueError("The completed pilot source audit is incomplete or missing a university.")
    for row in index_rows:
        row["viewer_page"] = int(row["viewer_page"])
        row["printed_bundle_page"] = (
            int(row["printed_bundle_page"])
            if str(row["printed_bundle_page"]).isdigit()
            else ""
        )
        row["visible_line_labels"] = (
            row["visible_line_labels"].split(", ")
            if row["visible_line_labels"]
            else []
        )
        row["line_text_json"] = json.loads(row["line_text_json"] or "{}")
        row["extraction_flags"] = [
            flag.strip() for flag in row["extraction_flags"].split(";") if flag.strip()
        ]
    return index_rows, audit_rows, pilot / ".preparation_checkpoint.sqlite3"


def _table_failure_ids(checkpoint: Checkpoint) -> list[str]:
    return [
        row["task_id"]
        for row in checkpoint.connection.execute(
            "SELECT task_id FROM tasks WHERE task_type='table_reading_order_review' AND status='failed'"
        )
    ]


def run_main_preparation(root: Path) -> dict[str, Any]:
    root = root.resolve()
    prep_dir = root / "data" / "preparation"
    output_dir = prep_dir / OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    protocol_path = prep_dir / "protocol_v1.1_critical_risk_amendment.json"
    if not protocol_path.is_file():
        raise FileNotFoundError("Protocol v1.1 critical-risk amendment is missing.")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol.get("protocol_version") != "1.1" or protocol.get("policy_cutoff_date") != POLICY_CUTOFF:
        raise ValueError("Protocol version or policy cut-off does not match the agreed preparation scope.")
    if set(protocol.get("critical_risk_criteria", {})) != set(SCENARIO_IDS):
        raise ValueError("Protocol v1.1 must provide a critical-risk criterion for S1-S15.")

    package_hashes, export_hashes = _verify_package_inputs(root)
    input_signature = _input_signature(root, package_hashes)
    pilot_checkpoint_path = _verify_pilot_checkpoint(root, input_signature)
    index_rows, audit_rows, _ = _load_pilot_files(root)
    signature = _source_signature(root, input_signature, protocol_path)
    checkpoint_path = output_dir / ".main_dataset_checkpoint.sqlite3"
    _migrate_main_checkpoint(checkpoint_path, signature)
    checkpoint = Checkpoint(
        checkpoint_path,
        signature,
        package_hashes,
        export_hashes,
        plan_version=MAIN_PLAN_VERSION,
    )
    database_path = root / "data" / "policy_coding.sqlite3"
    connection = _readonly_database(database_path)
    started = time.time()
    try:
        resources = _read_resources(connection)
        dimension_names = _dimension_names(resources["Curtin"]["codebook_text"])
        if set(protocol.get("applies_to_universities", [])) != set(UNIVERSITIES):
            raise ValueError("Protocol v1.1 does not list all five universities.")
        _register_tasks(checkpoint, index_rows)
        _write_progress(root, checkpoint, started)

        for row in index_rows:
            if "ambiguous_table" in row["extraction_flags"]:
                _table_review_task(root, checkpoint, row, started)

        updated_index = _updated_index_rows(checkpoint, index_rows)
        index_by_key = _index_maps(updated_index)
        _reuse_pilot_searches(root, checkpoint, pilot_checkpoint_path, started)

        run_id = signature
        for university in UNIVERSITIES:
            for scenario_id in SCENARIO_IDS:
                for query_index, (category, query) in enumerate(
                    _search_plan(scenario_id), start=1
                ):
                    _query_task(
                        connection,
                        checkpoint,
                        root,
                        started,
                        run_id,
                        university,
                        scenario_id,
                        query_index,
                        category,
                        query,
                        index_by_key,
                    )

        search_rows, candidates = _materialize_searches(checkpoint)
        candidates = _merge_candidate_flags(candidates, index_by_key)
        table_failures = _table_failure_ids(checkpoint)
        for university in UNIVERSITIES:
            for scenario_id in SCENARIO_IDS:
                task_id = f"handoff:{university}:{scenario_id}"
                filename = f"AI_handoff_{university}_{scenario_id}.txt"
                text = _handoff_text(
                    university,
                    scenario_id,
                    resources,
                    protocol,
                    dimension_names,
                    search_rows,
                    candidates,
                    audit_rows,
                    table_failures,
                    f"{OUTPUT_DIR_NAME}\\policy_index.csv",
                )
                _write_task_output(
                    root,
                    checkpoint,
                    task_id,
                    output_dir / filename,
                    text.encode("utf-8"),
                    started,
                )

        index_output_rows = [
            {
                **row,
                "visible_line_labels": ", ".join(row["visible_line_labels"]),
                "line_text_json": _json(row["line_text_json"]),
                "extraction_flags": "; ".join(row["extraction_flags"]),
            }
            for row in updated_index
        ]
        schema_bytes = (
            json.dumps(_proposal_schema(), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        request_template = {
            "schema_version": "1.0",
            "requests": [],
        }
        output_specs = [
            (
                "output:policy_index.csv",
                "policy_index.csv",
                _csv_bytes(index_output_rows, PAGE_COLUMNS),
            ),
            (
                "output:source_audit.csv",
                "source_audit.csv",
                (prep_dir / "source_audit.csv").read_bytes(),
            ),
            (
                "output:search_log.csv",
                "search_log.csv",
                _csv_bytes(search_rows, SEARCH_COLUMNS),
            ),
            (
                "output:evidence_candidates.csv",
                "evidence_candidates.csv",
                _csv_bytes(candidates, CANDIDATE_COLUMNS),
            ),
            ("output:proposal_return_schema.json", "proposal_return_schema.json", schema_bytes),
            (
                "output:additional_search_request_schema.json",
                "additional_search_request_schema.json",
                (json.dumps(_additional_request_file_schema(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            ),
            (
                "output:additional_search_requests.json",
                "additional_search_requests.json",
                (json.dumps(request_template, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            ),
            (
                "output:ANTIGRAVITY_START_HERE.txt",
                "ANTIGRAVITY_START_HERE.txt",
                ANTIGRAVITY_START.encode("utf-8"),
            ),
        ]
        for task_id, filename, content in output_specs:
            _write_task_output(
                root, checkpoint, task_id, output_dir / filename, content, started
            )

        package_hashes_after, export_hashes_after = _verify_package_inputs(root)
        input_differences: list[str] = []
        if package_hashes_after != package_hashes:
            input_differences.append("package or extracted-file hashes changed during preparation")
        if export_hashes_after != export_hashes:
            input_differences.append("existing data/exports changed during preparation")
        if _input_signature(root, package_hashes_after) != input_signature:
            input_differences.append("indexed database changed during preparation")
        if _source_signature(root, input_signature, protocol_path) != signature:
            input_differences.append("reused pilot outputs or Protocol v1.1 changed during preparation")

        if checkpoint.get_metadata("finalized_at") is None:
            checkpoint.set_metadata("finalized_at", _utc_now())
        reused_searches = len(PILOT_SEARCH_PLAN["S1"]) + len(PILOT_SEARCH_PLAN["S6"]) + len(PILOT_SEARCH_PLAN["S13"])
        executed_searches = len(UNIVERSITIES) * sum(
            len(_search_plan(scenario_id)) for scenario_id in SCENARIO_IDS
        ) - reused_searches
        report = _final_report(
            checkpoint,
            updated_index,
            audit_rows,
            search_rows,
            candidates,
            reused_searches=reused_searches,
            executed_searches=executed_searches,
            input_differences=input_differences,
        )
        _write_task_output(
            root,
            checkpoint,
            "output:preparation_validation_report.txt",
            output_dir / "preparation_validation_report.txt",
            report.encode("utf-8"),
            started,
        )
        _write_progress(root, checkpoint, started)
        counts = {
            status: checkpoint.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status=?", (status,)
            ).fetchone()[0]
            for status in ("completed", "failed", "pending", "running")
        }
        return {
            "status": (
                "complete"
                if counts["failed"] == counts["pending"] == counts["running"] == 0
                and not input_differences
                else "incomplete"
            ),
            "task_counts": counts,
            "cases": len(UNIVERSITIES) * len(SCENARIO_IDS),
            "indexed_pages_reused": len(updated_index),
            "source_audit_rows_reused": len(audit_rows),
            "search_tasks_reused": reused_searches,
            "search_tasks_executed": executed_searches,
            "search_rows": len(search_rows),
            "candidate_passages": len(candidates),
            "handoffs": sum(
                (output_dir / f"AI_handoff_{university}_{scenario_id}.txt").is_file()
                for university in UNIVERSITIES
                for scenario_id in SCENARIO_IDS
            ),
            "output_dir": str(output_dir),
        }
    finally:
        connection.close()
        checkpoint.close()


def _validate_additional_requests(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, dict) or data.get("schema_version") != "1.0":
        raise ValueError("Additional-search request file must have schema_version '1.0'.")
    requests = data.get("requests")
    if not isinstance(requests, list):
        raise ValueError("Additional-search request file must contain a requests array.")
    seen: set[str] = set()
    normalised: list[dict[str, str]] = []
    required = {
        "request_id",
        "university",
        "case_id",
        "scenario_id",
        "query",
        "category",
        "rationale",
    }
    for item in requests:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("Each additional-search request must include all required fields.")
        allowed = required | {"requested_by"}
        if set(item) - allowed:
            raise ValueError(
                "Unknown additional-search request fields: "
                + ", ".join(sorted(set(item) - allowed))
            )
        row = {key: str(value).strip() for key, value in item.items()}
        if row["university"] not in UNIVERSITIES:
            raise ValueError(f"Unknown university in additional-search request: {row['university']}")
        if row["scenario_id"] not in SCENARIO_IDS:
            raise ValueError(f"Unknown scenario in additional-search request: {row['scenario_id']}")
        if row["case_id"] != f"{row['university']}-{row['scenario_id']}":
            raise ValueError(f"case_id does not match university/scenario: {row['case_id']}")
        if row["request_id"] in seen or not row["request_id"]:
            raise ValueError(f"Missing or duplicate additional-search request_id: {row['request_id']}")
        if not row["query"] or not row["category"] or not row["rationale"]:
            raise ValueError("Additional search query, category, and rationale cannot be blank.")
        seen.add(row["request_id"])
        normalised.append(row)
    return normalised


def run_additional_searches(root: Path, requests_path: Path) -> dict[str, Any]:
    root = root.resolve()
    output_dir = root / "data" / "preparation" / OUTPUT_DIR_NAME
    request_path = requests_path.resolve()
    if not output_dir.is_dir() or not (output_dir / ".main_dataset_checkpoint.sqlite3").is_file():
        raise FileNotFoundError("Prepare the main dataset before running additional searches.")
    requests_data = json.loads(request_path.read_text(encoding="utf-8"))
    requests = _validate_additional_requests(requests_data)
    if not requests:
        return {"status": "no_requests", "executed": 0}
    request_signature = _sha256_bytes(
        json.dumps(requests, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    base_prep = root / "data" / "preparation"
    protocol_path = base_prep / "protocol_v1.1_critical_risk_amendment.json"
    package_hashes, export_hashes = _verify_package_inputs(root)
    input_signature = _input_signature(root, package_hashes)
    _verify_pilot_checkpoint(root, input_signature)
    base_signature = _source_signature(root, input_signature, protocol_path)
    checkpoint = Checkpoint(
        output_dir / ".main_dataset_checkpoint.sqlite3",
        base_signature,
        package_hashes,
        export_hashes,
        plan_version=MAIN_PLAN_VERSION,
    )
    connection = _readonly_database(root / "data" / "policy_coding.sqlite3")
    started = time.time()
    try:
        index_rows = _read_csv(output_dir / "policy_index.csv")
        for row in index_rows:
            row["viewer_page"] = int(row["viewer_page"])
            row["printed_bundle_page"] = (
                int(row["printed_bundle_page"])
                if str(row["printed_bundle_page"]).isdigit()
                else ""
            )
            row["visible_line_labels"] = (
                row["visible_line_labels"].split(", ")
                if row["visible_line_labels"]
                else []
            )
            row["line_text_json"] = json.loads(row["line_text_json"] or "{}")
            row["extraction_flags"] = [
                flag.strip() for flag in row["extraction_flags"].split(";") if flag.strip()
            ]
        index_by_key = _index_maps(index_rows)
        for request in requests:
            digest = hashlib.sha256(request["request_id"].encode("utf-8")).hexdigest()[:12]
            checkpoint.add_task(
                f"additional_search:{request_signature[:12]}:{digest}",
                "additional_search_query",
                f"{request['university']}/{request['case_id']}/{request['request_id']}",
            )
        for request in requests:
            digest = hashlib.sha256(request["request_id"].encode("utf-8")).hexdigest()[:12]
            task_id = f"additional_search:{request_signature[:12]}:{digest}"

            def operation(request: dict[str, str] = request) -> dict[str, Any]:
                result = _execute_search(
                    connection,
                    request_signature,
                    request["university"],
                    request["scenario_id"],
                    "additional-" + request["request_id"],
                    request["category"],
                    request["query"],
                    index_by_key,
                    search_origin="additional_search_request",
                )
                for row in result["search_rows"]:
                    row["request_id"] = request["request_id"]
                    row["rationale"] = request["rationale"]
                for row in result["candidate_rows"]:
                    row["request_id"] = request["request_id"]
                return result

            _run_task(checkpoint, task_id, operation, root, started)

        result_rows: list[dict[str, Any]] = []
        candidate_rows: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}
        for request in requests:
            digest = hashlib.sha256(request["request_id"].encode("utf-8")).hexdigest()[:12]
            result = checkpoint.result(
                f"additional_search:{request_signature[:12]}:{digest}"
            )
            if not result:
                continue
            result_rows.extend(result.get("search_rows", []))
            for row in result.get("candidate_rows", []):
                key = (
                    row["university"],
                    row["scenario_id"],
                    row["source_id"],
                    int(row["viewer_page"]),
                    row["line_start"],
                )
                if key not in candidate_rows:
                    candidate_rows[key] = row
                else:
                    old = candidate_rows[key]
                    old["query_id"] += ", " + row["query_id"]
                    old["search_category"] += ", " + row["search_category"]
                    old["search_terms"] += " | " + row["search_terms"]
        normalised_candidates = _merge_candidate_flags(
            list(candidate_rows.values()), index_by_key
        )
        suffix = request_signature[:12]
        search_path = output_dir / f"additional_search_results_{suffix}.csv"
        candidate_path = output_dir / f"additional_evidence_candidates_{suffix}.csv"
        report_path = output_dir / f"additional_search_report_{suffix}.txt"
        for task_id, path, rows, columns in (
            (
                f"output:additional-search:{suffix}",
                search_path,
                result_rows,
                SEARCH_COLUMNS + ("request_id", "rationale"),
            ),
            (
                f"output:additional-candidates:{suffix}",
                candidate_path,
                normalised_candidates,
                CANDIDATE_COLUMNS + ("request_id",),
            ),
        ):
            checkpoint.add_task(task_id, "additional_search_export", path.name)
            _write_task_output(
                root,
                checkpoint,
                task_id,
                path,
                _csv_bytes(rows, columns),
                started,
            )
        report_lines = [
            "ADDITIONAL SEARCH EXECUTION REPORT",
            f"Request file: {request_path}",
            f"Requests: {len(requests)}",
            f"Search result rows: {len(result_rows)}",
            f"Candidate passages: {len(normalised_candidates)}",
            "Results are local index retrieval only; candidates remain unconfirmed.",
            "",
        ]
        report_lines.extend(
            f"- {row['request_id']} | {row['university']} {row['scenario_id']} | "
            f"{row['query']} | {row['rationale']}"
            for row in requests
        )
        checkpoint.add_task(
            f"output:additional-search-report:{suffix}",
            "additional_search_report",
            report_path.name,
        )
        _write_task_output(
            root,
            checkpoint,
            f"output:additional-search-report:{suffix}",
            report_path,
            ("\n".join(report_lines) + "\n").encode("utf-8"),
            started,
        )
        _write_progress(root, checkpoint, started)
        return {
            "status": "complete" if all(
                checkpoint.task(f"additional_search:{request_signature[:12]}:"
                                f"{hashlib.sha256(row['request_id'].encode('utf-8')).hexdigest()[:12]}")["status"]
                == "completed"
                for row in requests
            ) else "incomplete",
            "executed": len(requests),
            "search_rows": len(result_rows),
            "candidate_passages": len(normalised_candidates),
            "search_log": str(search_path),
            "candidate_log": str(candidate_path),
            "report": str(report_path),
        }
    finally:
        connection.close()
        checkpoint.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare traceable evidence packets for all five universities and 15 scenarios."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--additional-search-requests",
        type=Path,
        help="Execute and log additional searches from a JSON request file.",
    )
    args = parser.parse_args()
    result = run_main_preparation(args.root)
    if args.additional_search_requests:
        result["additional_searches"] = run_additional_searches(
            args.root, args.additional_search_requests
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
