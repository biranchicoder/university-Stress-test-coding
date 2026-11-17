from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz

from app import page_search
from policy_coder.core import UNIVERSITIES, indexed_quote
from policy_coder.indexer import (
    BUNDLE_RE,
    LINE_RE,
    SOURCE_RE,
    page_lines,
    sha256_file,
)


POLICY_CUTOFF = "2026-11-12"
PLAN_VERSION = "1"
MAX_RETRIES = 3
SCENARIO_IDS = ("S1", "S6", "S13")

SEARCH_PLAN: dict[str, tuple[tuple[str, str], ...]] = {
    "S1": (
        ("actor_and_activity", "generative AI assessment"),
        ("permission", "written permission"),
        ("restriction", "not permitted assessment"),
        ("procedure", "assessment procedure student"),
        ("disclosure", "student disclosure"),
        ("review_route", "appeal review decision"),
        ("related_integrity_rules", "academic integrity student"),
    ),
    "S6": (
        ("actor_and_role", "student representative"),
        ("acting_for_student", "on behalf student"),
        ("authority", "delegated authority"),
        ("access_and_restrictions", "password account"),
        ("procedure", "complaint process student"),
        ("review_route", "student appeal review"),
        ("confidentiality", "privacy disclosure information"),
    ),
    "S13": (
        ("actor_and_activity", "student data performance"),
        ("purpose", "personal information purpose"),
        ("consent", "consent information collection"),
        ("collection_and_use_limits", "collect use disclose data"),
        ("retention_and_deletion", "retention disposal records"),
        ("access_and_correction", "student record access"),
        ("security_and_review", "privacy security data"),
    ),
}

PAGE_COLUMNS = (
    "university",
    "source_id",
    "document_title",
    "viewer_page",
    "printed_bundle_page",
    "visible_line_labels",
    "line_text_json",
    "page_text",
    "context_before",
    "context_after",
    "extraction_flags",
    "mapping_status",
    "pdf_path",
    "pdf_sha256",
)
AUDIT_COLUMNS = (
    "university",
    "source_id",
    "title",
    "official_url",
    "retrieved",
    "policy_cutoff",
    "retrieval_is_effective_date",
    "human_inclusion",
    "inclusion_status",
    "authority",
    "authority_status",
    "effective_date",
    "effective_date_status",
    "operative_version_at_freeze",
    "operative_version_status",
    "supersedes_or_duplicate_of",
    "supersession_status",
    "freeze_date",
    "reviewer",
    "bundle_page_occurrences",
    "possible_duplicate_ids",
    "referenced_source_ids",
    "unresolved_link_references",
    "missing_linked_instrument_status",
    "audit_notes",
)
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
)
CANDIDATE_COLUMNS = (
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


class SimulatedInterruption(BaseException):
    """Test-only interruption raised after durable page checkpoints."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _readonly_database(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _source_registers(root: Path) -> dict[str, dict[str, dict[str, str]]]:
    result: dict[str, dict[str, dict[str, str]]] = {}
    for university in UNIVERSITIES:
        paths = list((root / "data" / "raw" / university).glob("Source_Register_*.csv"))
        if len(paths) != 1:
            raise FileNotFoundError(
                f"{university}: expected one extracted Source_Register_*.csv; found {len(paths)}."
            )
        with paths[0].open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        registers: dict[str, dict[str, str]] = {}
        for row in rows:
            source_id = (row.get("source_id") or "").strip()
            if not source_id or source_id in registers:
                raise ValueError(f"{university}: missing or duplicate source ID {source_id!r}.")
            registers[source_id] = {key: (value or "").strip() for key, value in row.items()}
        result[university] = registers
    return result


def _verify_package_inputs(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    manifest_path = root / "data" / "derived" / "file_hashes.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            "The extraction hash manifest is missing. Run the existing safe package extractor first."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_records = manifest.get("packages", {})
    if set(package_records) != set(UNIVERSITIES):
        raise ValueError("The extraction manifest does not contain exactly the five universities.")

    hashes: dict[str, str] = {}
    for university in UNIVERSITIES:
        record = package_records[university]
        archive = root / record["archive"]
        if not archive.is_file():
            raise FileNotFoundError(f"Original package archive is missing: {archive}")
        archive_hash = sha256_file(archive)
        if archive_hash != record["archive_sha256"]:
            raise ValueError(f"Original package hash changed: {archive.name}")
        hashes[f"archive:{archive.name}"] = archive_hash
        for relative, expected in record.get("files", {}).items():
            extracted = root / expected["path"]
            if not extracted.is_file():
                raise FileNotFoundError(f"Extracted package file is missing: {extracted}")
            actual_hash = sha256_file(extracted)
            if actual_hash != expected["sha256"]:
                raise ValueError(f"Extracted package hash changed: {expected['path']}")
            hashes[f"extracted:{university}:{relative}"] = actual_hash
    hashes["manifest"] = sha256_file(manifest_path)

    export_hashes = {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted((root / "data" / "exports").glob("*"))
        if path.is_file()
    }
    return hashes, export_hashes


def _input_signature(root: Path, package_hashes: dict[str, str]) -> str:
    database = root / "data" / "policy_coding.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"Indexed SQLite database is missing: {database}")
    inputs = dict(package_hashes)
    inputs["database"] = sha256_file(database)
    wal = Path(str(database) + "-wal")
    if wal.exists() and wal.stat().st_size:
        inputs["database-wal"] = sha256_file(wal)
    return _sha256_bytes(_json(inputs).encode("utf-8"))


class Checkpoint:
    def __init__(self, path: Path, signature: str, package_hashes: dict[str, str],
                 export_hashes: dict[str, str], *, plan_version: str = PLAN_VERSION) -> None:
        self.path = path
        existed = path.exists()
        if not existed:
            unexpected = [
                item for item in path.parent.iterdir()
                if item.name != path.name and item.is_file()
            ]
            if unexpected:
                raise FileExistsError(
                    "Preparation outputs exist without a checkpoint; refusing to overwrite: "
                    + ", ".join(item.name for item in unexpected)
                )
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                error_text TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS task_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL,
                error_text TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self.connection.commit()
        check = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"Preparation checkpoint failed integrity check: {check}")

        stored = self.get_metadata("input_signature")
        if stored is not None and stored != signature:
            raise ValueError(
                "Package, extracted-file, or indexed-database hashes changed since this checkpoint. "
                "Refusing to resume against different inputs."
            )
        if stored is None:
            if existed and self.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
                raise ValueError("Checkpoint metadata is incomplete; refusing to resume.")
            self.set_metadata("input_signature", signature)
            self.set_metadata("package_hashes", _json(package_hashes))
            self.set_metadata("original_export_hashes", _json(export_hashes))
            self.set_metadata("run_started_at", _utc_now())
            self.set_metadata("plan_version", plan_version)
        elif self.get_metadata("plan_version") != plan_version:
            raise ValueError("Preparation plan version changed; refusing to reuse this checkpoint.")
        self.connection.execute(
            "UPDATE tasks SET status='pending' WHERE status='running'"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get_metadata(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value)
        )
        self.connection.commit()

    def add_task(self, task_id: str, task_type: str, scope: str) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO tasks(task_id, task_type, scope, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)""",
            (task_id, task_type, scope, _utc_now()),
        )
        self.connection.commit()

    def task(self, task_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unregistered preparation task: {task_id}")
        return row

    def result(self, task_id: str) -> dict[str, Any] | None:
        task = self.task(task_id)
        return json.loads(task["result_json"]) if task["result_json"] else None

    def begin_attempt(self, task_id: str) -> int:
        task = self.task(task_id)
        attempt = int(task["attempts"]) + 1
        self.connection.execute(
            "UPDATE tasks SET status='running', attempts=?, error_text='' WHERE task_id=?",
            (attempt, task_id),
        )
        self.connection.execute(
            """INSERT INTO task_attempts(task_id, attempt_number, started_at, outcome)
            VALUES (?, ?, ?, 'running')""",
            (task_id, attempt, _utc_now()),
        )
        self.connection.commit()
        return attempt

    def fail_attempt(self, task_id: str, attempt: int, error: str) -> bool:
        task = self.task(task_id)
        is_persistent = int(task["attempts"]) > MAX_RETRIES
        status = "failed" if is_persistent else "pending"
        self.connection.execute(
            "UPDATE tasks SET status=?, error_text=? WHERE task_id=?",
            (status, error, task_id),
        )
        self.connection.execute(
            """UPDATE task_attempts SET finished_at=?, outcome=?, error_text=?
            WHERE task_id=? AND attempt_number=?""",
            (_utc_now(), "failed" if is_persistent else "retry", error, task_id, attempt),
        )
        self.connection.commit()
        return is_persistent

    def complete(self, task_id: str, attempt: int, result: Any) -> None:
        self.connection.execute(
            """UPDATE tasks SET status='completed', completed_at=?, error_text='', result_json=?
            WHERE task_id=?""",
            (_utc_now(), _json(result), task_id),
        )
        self.connection.execute(
            """UPDATE task_attempts SET finished_at=?, outcome='completed'
            WHERE task_id=? AND attempt_number=?""",
            (_utc_now(), task_id, attempt),
        )
        self.connection.commit()

    def mark_failed_without_retry(self, task_id: str, error: str) -> None:
        self.connection.execute(
            "UPDATE tasks SET status='failed', error_text=? WHERE task_id=?",
            (error, task_id),
        )
        self.connection.commit()


def _write_progress_report(root: Path, checkpoint: Checkpoint) -> None:
    rows = checkpoint.connection.execute(
        "SELECT task_id, task_type, scope, status, attempts, error_text FROM tasks ORDER BY task_id"
    ).fetchall()
    counts = {status: sum(row["status"] == status for row in rows)
              for status in ("completed", "failed", "pending", "running")}
    attempts = checkpoint.connection.execute(
        "SELECT COUNT(*) FROM task_attempts"
    ).fetchone()[0]
    errors = [
        f"- {row['task_id']} ({row['attempts']} attempts): {row['error_text']}"
        for row in rows if row["status"] == "failed"
    ]
    pending = [
        f"- {row['task_id']} [{row['task_type']}, {row['scope']}]"
        for row in rows if row["status"] in {"pending", "running"}
    ]
    text = [
        "Evidence preparation validation and progress report",
        f"Run started: {checkpoint.get_metadata('run_started_at')}",
        f"Policy cut-off: {POLICY_CUTOFF}",
        "Preparation status: " + (
            "complete" if rows and counts["completed"] == len(rows) else "incomplete"
        ),
        "",
        "Task counts:",
        f"  Completed: {counts['completed']}",
        f"  Failed after {MAX_RETRIES} retries: {counts['failed']}",
        f"  Pending: {counts['pending']}",
        f"  Running at report time: {counts['running']}",
        f"  Attempts recorded: {attempts}",
        "",
        "Persistent failures:",
        *(errors or ["- None recorded."]),
        "",
        "Pending tasks:",
        *(pending or ["- None."]),
        "",
        "A task failure is retained in the checkpoint and is not treated as a successful extraction.",
        "Ratings are not assigned or changed by this preparation run.",
    ]
    report_path = root / "data" / "preparation" / "preparation_validation_report.txt"
    _atomic_replace(report_path, ("\n".join(text) + "\n").encode("utf-8"), allow_replace=True)
    checkpoint.set_metadata(
        "last_reported_task_count", str(counts["completed"] + counts["failed"])
    )


def _maybe_write_progress_report(
    root: Path, checkpoint: Checkpoint, *, force: bool = False
) -> None:
    finished = checkpoint.connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('completed', 'failed')"
    ).fetchone()[0]
    last_written = int(checkpoint.get_metadata("last_reported_task_count") or "0")
    if not force and finished - last_written < 25:
        return
    try:
        _write_progress_report(root, checkpoint)
    except OSError as exc:
        failures = json.loads(checkpoint.get_metadata("progress_report_write_failures") or "[]")
        failures.append(f"{_utc_now()} {type(exc).__name__}: {exc}")
        checkpoint.set_metadata("progress_report_write_failures", _json(failures))


def _atomic_replace(path: Path, content: bytes, *, allow_replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == content:
            return
        if not allow_replace:
            raise FileExistsError(f"Refusing to overwrite an existing completed output: {path}")
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if allow_replace:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    os.replace(temporary, path)
                    break
                except OSError:
                    if attempt == MAX_RETRIES:
                        raise
                    time.sleep(0.1 * (attempt + 1))
        else:
            os.link(temporary, path)
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _run_task(
    checkpoint: Checkpoint,
    task_id: str,
    operation: Callable[[], Any],
    report_root: Path,
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
            _maybe_write_progress_report(report_root, checkpoint)
            return None
        attempt = checkpoint.begin_attempt(task_id)
        try:
            result = operation()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            persistent = checkpoint.fail_attempt(task_id, attempt, error)
            _maybe_write_progress_report(report_root, checkpoint)
            if persistent:
                return None
        else:
            checkpoint.complete(task_id, attempt, result)
            _maybe_write_progress_report(report_root, checkpoint)
            return result


def _make_task_plan(checkpoint: Checkpoint, registers: dict[str, dict[str, dict[str, str]]]) -> None:
    for university in UNIVERSITIES:
        checkpoint.add_task(f"pdf_open:{university}", "pdf_open", university)
        for source_id in registers[university]:
            checkpoint.add_task(
                f"source_audit:{university}:{source_id}", "source_audit", f"{university}/{source_id}"
            )
    for scenario_id, specs in SEARCH_PLAN.items():
        for index, (category, _) in enumerate(specs, start=1):
            checkpoint.add_task(
                f"search:Curtin:{scenario_id}:{index:02d}",
                "search_query",
                f"Curtin/{scenario_id}/{category}",
            )
        checkpoint.add_task(f"handoff:Curtin:{scenario_id}", "ai_handoff", f"Curtin/{scenario_id}")
    for output_name, output_type in (
        ("policy_index.csv", "policy_index_export"),
        ("source_audit.csv", "source_audit_export"),
        ("search_log.csv", "search_log_export"),
        ("evidence_candidates.csv", "candidate_export"),
    ):
        checkpoint.add_task(f"output:{output_name}", output_type, output_name)


def _ensure_page_tasks(
    connection: sqlite3.Connection,
    checkpoint: Checkpoint,
    university: str,
) -> int:
    opened = checkpoint.result(f"pdf_open:{university}")
    indexed_pages = connection.execute(
        "SELECT COALESCE(MAX(viewer_page), 0) FROM pages WHERE university=?", (university,)
    ).fetchone()[0]
    pdf_pages = int(opened["page_count"]) if opened else 0
    count = max(int(indexed_pages), pdf_pages)
    for viewer_page in range(1, count + 1):
        checkpoint.add_task(
            f"pdf_page:{university}:{viewer_page:04d}", "pdf_page", f"{university}/viewer-{viewer_page}"
        )
    if pdf_pages != indexed_pages:
        checkpoint.set_metadata(
            f"page_count_warning:{university}",
            f"PDF has {pdf_pages} pages; SQLite index has {indexed_pages} pages.",
        )
    return count


def _pdf_open(root: Path, university: str) -> dict[str, Any]:
    pdfs = list((root / "data" / "raw" / university).glob("*.pdf"))
    if len(pdfs) != 1:
        raise FileNotFoundError(f"{university}: expected one extracted PDF; found {len(pdfs)}.")
    with fitz.open(pdfs[0]) as document:
        return {
            "pdf_path": str(pdfs[0].relative_to(root)),
            "pdf_sha256": sha256_file(pdfs[0]),
            "page_count": len(document),
        }


def _page_verification(
    root: Path,
    connection: sqlite3.Connection,
    university: str,
    viewer_page: int,
    pdf_info: dict[str, Any] | None,
    pdf_document: fitz.Document | None = None,
) -> dict[str, Any]:
    pdf_path = root / "data" / "raw" / university
    pdfs = list(pdf_path.glob("*.pdf"))
    if len(pdfs) != 1:
        raise FileNotFoundError(f"{university}: expected one extracted PDF; found {len(pdfs)}.")
    pdf = pdfs[0]
    indexed = connection.execute(
        "SELECT * FROM pages WHERE university=? AND viewer_page=?",
        (university, viewer_page),
    ).fetchone()
    documents = connection.execute(
        "SELECT source_id, title FROM documents WHERE university=?", (university,)
    ).fetchall()
    titles = {row["source_id"]: row["title"] for row in documents}
    flags = json.loads(indexed["flags_json"]) if indexed else []
    issues: list[str] = list(flags)
    if indexed is None:
        issues.append("pdf_page_missing_from_sqlite_index")

    if pdf_info is None or viewer_page > int(pdf_info["page_count"]):
        issues.append("pdf_page_could_not_be_opened")
        return _fallback_page_row(
            root, university, viewer_page, indexed, titles, pdf, issues
        )

    if pdf_document is None:
        with fitz.open(pdf) as document:
            page = document[viewer_page - 1]
            raw_text_value = page.get_text("text", sort=False)
            if not isinstance(raw_text_value, str):
                raise TypeError("PyMuPDF returned non-text content for text extraction.")
            raw_text = raw_text_value
            visible_lines = page_lines(page)
    else:
        page = pdf_document[viewer_page - 1]
        raw_text_value = page.get_text("text", sort=False)
        if not isinstance(raw_text_value, str):
            raise TypeError("PyMuPDF returned non-text content for text extraction.")
        raw_text = raw_text_value
        visible_lines = page_lines(page)
    normalized_text = LINE_RE.sub("", raw_text)
    normalized_text = re.sub(r"\n{3,}", "\n\n", normalized_text).strip()
    bundle_match = BUNDLE_RE.search(raw_text)
    source_match = SOURCE_RE.search(raw_text)
    pdf_bundle = int(bundle_match.group(1)) if bundle_match else None
    pdf_source = source_match.group(1).upper() if source_match else None

    index_lines = json.loads(indexed["lines_json"]) if indexed else {}
    if indexed:
        if indexed["bundle_page"] != pdf_bundle:
            issues.append("printed_bundle_page_does_not_match_pdf")
        if indexed["source_id"] != pdf_source:
            issues.append("source_id_does_not_match_pdf_footer")
        if indexed["text"] != normalized_text:
            issues.append("indexed_page_text_does_not_match_pdf_text")
        if index_lines != visible_lines:
            issues.append("indexed_L_label_mapping_does_not_match_pdf_layout")
    if pdf_bundle is None:
        issues.append("printed_bundle_page_not_extracted")
    if visible_lines and not pdf_source:
        issues.append("visible_L_labels_without_source_ID")
    if not raw_text.strip():
        issues.append("no_extractable_text")
    if not visible_lines:
        issues.append("no_visible_L_labels_in_pdf")
    if pdf_source and pdf_source not in titles:
        issues.append("pdf_footer_source_id_missing_from_register")

    verification_errors = {
        "pdf_page_missing_from_sqlite_index",
        "pdf_page_could_not_be_opened",
        "printed_bundle_page_does_not_match_pdf",
        "source_id_does_not_match_pdf_footer",
        "indexed_page_text_does_not_match_pdf_text",
        "indexed_L_label_mapping_does_not_match_pdf_layout",
        "visible_L_labels_without_source_ID",
        "no_extractable_text",
        "pdf_footer_source_id_missing_from_register",
    }
    if verification_errors.intersection(issues):
        status = "failed"
    elif not visible_lines:
        status = "verified_no_visible_L_labels"
    else:
        status = "verified"

    return {
        "university": university,
        "source_id": pdf_source or "",
        "index_source_id": indexed["source_id"] if indexed and indexed["source_id"] else "",
        "document_title": titles.get(pdf_source or "", ""),
        "viewer_page": viewer_page,
        "printed_bundle_page": pdf_bundle if pdf_bundle is not None else "",
        "visible_line_labels": list(visible_lines),
        "line_text_json": visible_lines,
        "index_line_text_json": index_lines,
        "page_text": normalized_text,
        "indexed_page_text": indexed["text"] if indexed else "",
        "context_before": "",
        "context_after": "",
        "extraction_flags": sorted(set(issues)),
        "mapping_status": status,
        "pdf_path": str(pdf.relative_to(root)),
        "pdf_sha256": (pdf_info or {}).get("pdf_sha256", sha256_file(pdf)),
    }


def _fallback_page_row(
    root: Path,
    university: str,
    viewer_page: int,
    indexed: sqlite3.Row | None,
    titles: dict[str, str],
    pdf: Path,
    flags: list[str],
) -> dict[str, Any]:
    index_lines = json.loads(indexed["lines_json"]) if indexed else {}
    return {
        "university": university,
        "source_id": indexed["source_id"] if indexed and indexed["source_id"] else "",
        "index_source_id": indexed["source_id"] if indexed and indexed["source_id"] else "",
        "document_title": titles.get(indexed["source_id"], "") if indexed else "",
        "viewer_page": viewer_page,
        "printed_bundle_page": indexed["bundle_page"] if indexed and indexed["bundle_page"] else "",
        "visible_line_labels": [],
        "line_text_json": {},
        "index_line_text_json": index_lines,
        "page_text": "",
        "indexed_page_text": indexed["text"] if indexed else "",
        "context_before": "",
        "context_after": "",
        "extraction_flags": sorted(set(flags + ["preparation_extraction_failure"])),
        "mapping_status": "failed",
        "pdf_path": str(pdf.relative_to(root)),
        "pdf_sha256": sha256_file(pdf),
    }


def _page_task_result(
    root: Path,
    connection: sqlite3.Connection,
    checkpoint: Checkpoint,
    university: str,
    viewer_page: int,
    pdf_document: fitz.Document | None = None,
) -> dict[str, Any] | None:
    task_id = f"pdf_page:{university}:{viewer_page:04d}"
    if checkpoint.task(task_id)["status"] == "completed":
        return checkpoint.result(task_id)
    open_task = checkpoint.task(f"pdf_open:{university}")
    pdf_info = checkpoint.result(f"pdf_open:{university}") if open_task["status"] == "completed" else None

    def operation() -> dict[str, Any]:
        return _page_verification(
            root, connection, university, viewer_page, pdf_info, pdf_document
        )

    return _run_task(checkpoint, task_id, operation, root)


def _contextualize_pages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(row["university"], row["viewer_page"]): row for row in rows}
    for row in rows:
        if row["mapping_status"] == "failed":
            continue
        for delta, field, take_tail in ((-1, "context_before", True), (1, "context_after", False)):
            other = by_key.get((row["university"], row["viewer_page"] + delta))
            if not other or other["source_id"] != row["source_id"] or other["mapping_status"] == "failed":
                continue
            text = other["page_text"]
            if not isinstance(text, str):
                continue
            row[field] = text[-1000:] if take_tail else text[:1000]
    return rows


def _candidate_records(
    scenario_id: str,
    query_id: str,
    category: str,
    query: str,
    hits: list[sqlite3.Row],
    verified_pages: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    query_tokens = {token.casefold() for token in re.findall(r"[\w'-]+", query, flags=re.UNICODE)}
    for rank, hit in enumerate(hits, start=1):
        key = ("Curtin", int(hit["viewer_page"]))
        page = verified_pages.get(key)
        if (
            not page
            or page["mapping_status"] != "verified"
            or not page["source_id"]
            or not page["document_title"]
        ):
            continue
        lines = page["line_text_json"]
        scored: list[tuple[int, int, str, str]] = []
        for label, text in lines.items():
            tokens = {token.casefold() for token in re.findall(r"[\w'-]+", text, flags=re.UNICODE)}
            matched = len(query_tokens & tokens)
            if matched:
                scored.append((-matched, int(label[1:]), label, text))
        for _, _, label, text in sorted(scored)[:3]:
            quotation = indexed_quote(lines, label, label)
            if quotation != text.strip():
                continue
            label_number = int(label[1:])
            context_before = lines.get(f"L{label_number - 1:03d}", "")
            context_after = lines.get(f"L{label_number + 1:03d}", "")
            candidates.append({
                "university": "Curtin",
                "scenario_id": scenario_id,
                "query_id": query_id,
                "search_category": category,
                "search_terms": query,
                "source_id": page["source_id"],
                "document_title": page["document_title"],
                "viewer_page": page["viewer_page"],
                "printed_bundle_page": page["printed_bundle_page"],
                "line_start": label,
                "line_end": label,
                "candidate_text": quotation,
                "context_before": context_before,
                "context_after": context_after,
                "page_review_flags": "; ".join(page["extraction_flags"]),
                "candidate_status": (
                    "Unreviewed candidate copied from PDF-verified index; not confirmed evidence."
                ),
                "human_verified": "No",
                "rating": "",
                "_result_rank": rank,
            })
    return candidates


def _query_task(
    connection: sqlite3.Connection,
    checkpoint: Checkpoint,
    scenario_id: str,
    query_index: int,
    category: str,
    query: str,
    verified_pages: dict[tuple[str, int], dict[str, Any]],
    root: Path,
) -> dict[str, Any] | None:
    task_id = f"search:Curtin:{scenario_id}:{query_index:02d}"

    def operation() -> dict[str, Any]:
        hits = page_search(connection, "Curtin", query, 10000)
        timestamp = _utc_now()
        rows: list[dict[str, Any]] = []
        for rank, hit in enumerate(hits, start=1):
            page = verified_pages.get(("Curtin", int(hit["viewer_page"])), {})
            rows.append({
                "run_id": checkpoint.get_metadata("input_signature"),
                "searched_at": timestamp,
                "university": "Curtin",
                "scenario_id": scenario_id,
                "query_id": f"{scenario_id}-Q{query_index:02d}",
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
                "review_flags": "; ".join(json.loads(hit["flags_json"])),
                "result_status": "retrieved_from_Curtin_full_text_index",
            })
        if not rows:
            rows.append({
                "run_id": checkpoint.get_metadata("input_signature"),
                "searched_at": timestamp,
                "university": "Curtin",
                "scenario_id": scenario_id,
                "query_id": f"{scenario_id}-Q{query_index:02d}",
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
            })
        candidates = _candidate_records(
            scenario_id, f"{scenario_id}-Q{query_index:02d}",
            category, query, hits, verified_pages,
        )
        return {"search_rows": rows, "candidate_rows": candidates}

    return _run_task(checkpoint, task_id, operation, root)


def _source_references(
    university: str,
    source_id: str,
    page_rows: list[dict[str, Any]],
    register: dict[str, dict[str, str]],
) -> tuple[list[str], list[str], list[int]]:
    referenced: set[str] = set()
    occurrences: list[int] = []
    for page in page_rows:
        if page["university"] != university or page["source_id"] != source_id:
            continue
        occurrences.append(int(page["viewer_page"]))
        page_text = page.get("page_text")
        if not isinstance(page_text, str):
            continue
        text = re.sub(
            r"(?im)^\s*(?:Bundle page|.*?\|\s*[NA]\d{3}\s*\|\s*Source reading page).*$",
            "",
            page_text,
        )
        referenced.update(
            match.group(0).upper()
            for match in re.finditer(r"\b[NA]\d{3}\b", text, flags=re.IGNORECASE)
            if match.group(0).upper() != source_id
        )
        lowered_text = text.casefold()
        for other_id, other_record in register.items():
            other_title = " ".join(other_record.get("title", "").casefold().split())
            if other_id != source_id and len(other_title) >= 8 and other_title in lowered_text:
                referenced.add(other_id)
    linked = sorted(value for value in referenced if value in register)
    unresolved = sorted(value for value in referenced if value not in register)
    return linked, unresolved, occurrences


def _duplicate_candidates(
    source_id: str, record: dict[str, str], register: dict[str, dict[str, str]]
) -> list[str]:
    def normalize(value: str) -> str:
        return " ".join(value.casefold().split()).rstrip("/")

    title = normalize(record.get("title", ""))
    url = normalize(record.get("official_url", ""))
    matches = []
    for other_id, other in register.items():
        if other_id == source_id:
            continue
        if title and title == normalize(other.get("title", "")):
            matches.append(other_id)
        elif url and url == normalize(other.get("official_url", "")):
            matches.append(other_id)
    return sorted(matches)


def _source_audit_row(
    university: str,
    source_id: str,
    record: dict[str, str],
    register: dict[str, dict[str, str]],
    page_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    linked, unresolved, occurrences = _source_references(
        university, source_id, page_rows, register
    )
    duplicates = _duplicate_candidates(source_id, record, register)
    effective_date = record.get("effective_date", "").strip()
    effective_status = "unknown_not_recorded"
    if effective_date:
        try:
            effective_status = (
                "on_or_before_policy_cutoff"
                if datetime.strptime(effective_date, "%Y-%m-%d").date().isoformat() <= POLICY_CUTOFF
                else "after_policy_cutoff"
            )
        except ValueError:
            effective_status = "unparsed_register_value_requires_review"
    linked_not_in_bundle = [
        reference for reference in linked
        if not any(
            row["university"] == university and row["source_id"] == reference
            for row in page_rows
        )
    ]
    missing_parts = []
    if unresolved:
        missing_parts.append("unmatched_explicit_source_IDs=" + ",".join(unresolved))
    if linked_not_in_bundle:
        missing_parts.append(
            "registered_linked_sources_without_PDF_footer_pages=" + ",".join(linked_not_in_bundle)
        )
    missing_status = (
        "; ".join(missing_parts)
        if missing_parts else
        "no_explicit_missing_link_detected; linked-document_completeness_not_established"
    )
    notes = [
        "Retrieval date is not an effective date.",
        "Register fields are preserved verbatim; blank review fields remain unknown/unreviewed.",
        "Exact title/URL matches are only duplicate candidates, not a duplicate determination."
        if duplicates else
        "No exact-title/URL match observed within this university register; broader duplication is not ruled out.",
    ]
    if not occurrences:
        notes.append("No PDF page footer was mapped to this source ID; this alone does not establish absence.")
    if linked_not_in_bundle:
        notes.append(
            "Referenced source(s) occur in the register but have no mapped PDF footer page: "
            + ", ".join(linked_not_in_bundle)
            + "."
        )
    return {
        "university": university,
        "source_id": source_id,
        "title": record.get("title", ""),
        "official_url": record.get("official_url", ""),
        "retrieved": record.get("retrieved", ""),
        "policy_cutoff": POLICY_CUTOFF,
        "retrieval_is_effective_date": "No",
        "human_inclusion": record.get("human_inclusion", ""),
        "inclusion_status": record.get("human_inclusion", "") or "unreviewed_blank_in_register",
        "authority": record.get("authority", ""),
        "authority_status": record.get("authority", "") or "unknown_not_recorded",
        "effective_date": effective_date,
        "effective_date_status": effective_status,
        "operative_version_at_freeze": record.get("operative_version_at_freeze", ""),
        "operative_version_status": record.get("operative_version_at_freeze", "") or "unknown_not_recorded",
        "supersedes_or_duplicate_of": record.get("supersedes_or_duplicate_of", ""),
        "supersession_status": record.get("supersedes_or_duplicate_of", "") or "unknown_not_recorded",
        "freeze_date": record.get("freeze_date", ""),
        "reviewer": record.get("reviewer", ""),
        "bundle_page_occurrences": ", ".join(map(str, occurrences)),
        "possible_duplicate_ids": ", ".join(duplicates),
        "referenced_source_ids": ", ".join(linked),
        "unresolved_link_references": ", ".join(unresolved),
        "missing_linked_instrument_status": missing_status,
        "audit_notes": " ".join(notes),
    }


def _csv_bytes(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _materialize_pages(checkpoint: Checkpoint, connection: sqlite3.Connection,
                       root: Path) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    task_rows = checkpoint.connection.execute(
        "SELECT task_id, status, result_json, error_text FROM tasks WHERE task_type='pdf_page' ORDER BY task_id"
    ).fetchall()
    for task in task_rows:
        if task["status"] == "completed" and task["result_json"]:
            pages.append(json.loads(task["result_json"]))
            continue
        _, university, page_text = task["task_id"].split(":")
        viewer_page = int(page_text)
        indexed = connection.execute(
            "SELECT * FROM pages WHERE university=? AND viewer_page=?",
            (university, viewer_page),
        ).fetchone()
        documents = connection.execute(
            "SELECT source_id, title FROM documents WHERE university=?", (university,)
        ).fetchall()
        titles = {row["source_id"]: row["title"] for row in documents}
        pdfs = list((root / "data" / "raw" / university).glob("*.pdf"))
        if len(pdfs) != 1:
            raise FileNotFoundError(
                f"{university}: expected one extracted PDF to record a failed page; found {len(pdfs)}."
            )
        pdf = pdfs[0]
        fallback = _fallback_page_row(
            root, university, viewer_page, indexed, titles, pdf,
            ["preparation_task_" + task["status"], task["error_text"]],
        )
        pages.append(fallback)
    return _contextualize_pages(pages)


def _materialize_audit(
    checkpoint: Checkpoint,
    registers: dict[str, dict[str, dict[str, str]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for university, register in registers.items():
        for source_id, record in register.items():
            task = checkpoint.task(f"source_audit:{university}:{source_id}")
            if task["status"] == "completed" and task["result_json"]:
                rows.append(json.loads(task["result_json"]))
            elif task["status"] == "failed":
                rows.append({
                    "university": university,
                    "source_id": source_id,
                    "title": record.get("title", ""),
                    "official_url": record.get("official_url", ""),
                    "retrieved": record.get("retrieved", ""),
                    "policy_cutoff": POLICY_CUTOFF,
                    "retrieval_is_effective_date": "No",
                    "effective_date": record.get("effective_date", ""),
                    "effective_date_status": "audit_task_failed",
                    "missing_linked_instrument_status": "audit_task_failed_manual_review_required",
                    "audit_notes": task["error_text"],
                })
            else:
                rows.append({
                    "university": university,
                    "source_id": source_id,
                    "title": record.get("title", ""),
                    "policy_cutoff": POLICY_CUTOFF,
                    "effective_date_status": "audit_task_pending",
                    "missing_linked_instrument_status": "audit_task_pending",
                    "audit_notes": "Source audit task is pending.",
                })
    return rows


def _materialize_searches(checkpoint: Checkpoint) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    search_rows: list[dict[str, Any]] = []
    candidate_map: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for scenario_id, specs in SEARCH_PLAN.items():
        for index, _ in enumerate(specs, start=1):
            task_id = f"search:Curtin:{scenario_id}:{index:02d}"
            task = checkpoint.task(task_id)
            if task["status"] == "completed" and task["result_json"]:
                result = json.loads(task["result_json"])
                search_rows.extend(result["search_rows"])
                for candidate in result["candidate_rows"]:
                    key = (candidate["scenario_id"], candidate["source_id"],
                           int(candidate["viewer_page"]), candidate["line_start"])
                    if key not in candidate_map:
                        candidate_map[key] = candidate
                    else:
                        existing = candidate_map[key]
                        existing["query_id"] += ", " + candidate["query_id"]
                        existing["search_category"] += ", " + candidate["search_category"]
                        existing["search_terms"] += " | " + candidate["search_terms"]
            elif task["status"] == "failed":
                category, query = specs[index - 1]
                search_rows.append({
                    "run_id": checkpoint.get_metadata("input_signature"),
                    "searched_at": "",
                    "university": "Curtin",
                    "scenario_id": scenario_id,
                    "query_id": f"{scenario_id}-Q{index:02d}",
                    "category": category,
                    "query": query,
                    "result_rank": "",
                    "result_count": "",
                    "result_status": f"search_task_failed: {task['error_text']}",
                })
    candidates = []
    for candidate in candidate_map.values():
        candidates.append({
            "university": candidate["university"],
            "scenario_id": candidate["scenario_id"],
            "query_ids": candidate["query_id"],
            "search_categories": candidate["search_category"],
            "search_terms": candidate["search_terms"],
            "source_id": candidate["source_id"],
            "document_title": candidate["document_title"],
            "viewer_page": candidate["viewer_page"],
            "printed_bundle_page": candidate["printed_bundle_page"],
            "line_start": candidate["line_start"],
            "line_end": candidate["line_end"],
            "candidate_text": candidate["candidate_text"],
            "context_before": candidate["context_before"],
            "context_after": candidate["context_after"],
            "page_review_flags": candidate["page_review_flags"],
            "candidate_status": candidate["candidate_status"],
            "human_verified": candidate["human_verified"],
            "rating": candidate["rating"],
        })
    return search_rows, candidates


def _handoff_text(
    connection: sqlite3.Connection,
    scenario_id: str,
    audit_rows: list[dict[str, Any]],
    search_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    resource = connection.execute(
        "SELECT codebook_text, scenarios_json FROM resources WHERE university='Curtin'"
    ).fetchone()
    if resource is None:
        raise ValueError("Curtin codebook/scenario resources are missing from SQLite.")
    scenarios = json.loads(resource["scenarios_json"])
    scenario = scenarios.get(scenario_id)
    if not scenario:
        raise ValueError(f"Curtin scenario {scenario_id} is missing from SQLite.")
    scenario_candidates = [row for row in candidates if row["scenario_id"] == scenario_id]
    source_ids = {row["source_id"] for row in scenario_candidates if row["source_id"]}
    source_ids.update(
        row["source_id"] for row in search_rows
        if row.get("scenario_id") == scenario_id and row.get("source_id")
    )
    source_audit = [row for row in audit_rows if row["university"] == "Curtin"
                    and row["source_id"] in source_ids]
    queries = [row for row in search_rows if row["scenario_id"] == scenario_id]
    lines = [
        "LOCAL POLICY EVIDENCE PREPARATION HANDOFF",
        "This is preparation material, not human coding or confirmed evidence.",
        f"University: Curtin University",
        f"Scenario: {scenario_id} — {scenario['title']}",
        f"Policy cut-off: {POLICY_CUTOFF}",
        "Human ratings: blank; none assigned by this preparation.",
        "Critical-risk criteria: not adopted/documented in a protocol amendment; do not infer them.",
        "Retrieval date is not an effective date.",
        "",
        "SCENARIO TEXT (verbatim from supplied Scenarios.docx):",
        scenario["text"],
        "",
        "SUPPLIED CODEBOOK (verbatim extracted text; do not modify):",
        resource["codebook_text"],
        "",
        "SEARCH ACTIVITY (actual full-text searches executed):",
    ]
    lines.extend(
        f"- {row['query_id']} [{row['category']}]: {row['query']} — "
        f"{row['result_count']} result(s); {row['result_status']}"
        for row in queries if row.get("result_rank") in ("", None, 1)
    )
    lines.extend([
        "",
        "SOURCE AUDIT FOR SOURCES WITH CANDIDATE PASSAGES:",
    ])
    if not source_audit:
        lines.append("- No candidate source IDs; inspect the source_audit.csv and full searchable corpus.")
    for row in source_audit:
        lines.append(
            f"- {row['source_id']} | {row['title']} | retrieved={row['retrieved']} | "
            f"effective_date={row['effective_date'] or 'unknown/not recorded'} | "
            f"operative_version={row['operative_version_at_freeze'] or 'unknown/not recorded'} | "
            f"supersession={row['supersedes_or_duplicate_of'] or 'unknown/not recorded'} | "
            f"authority={row['authority'] or 'unknown/not recorded'}"
        )
    lines.extend([
        "",
        "UNREVIEWED CANDIDATE PASSAGES (verbatim from PDF-verified indexed lines):",
        "Each passage is only a lexical search candidate. Independently inspect the PDF image,",
        "surrounding provisions, source authority, effective version, exceptions and linked documents.",
        "Do not treat candidates as confirmed evidence or assign ratings from them.",
    ])
    if not scenario_candidates:
        lines.append("- No line-located candidates passed PDF-to-index verification for this scenario.")
    for index, row in enumerate(scenario_candidates, start=1):
        lines.extend([
            "",
            f"Candidate {index} — {row['candidate_status']}",
            f"Source ID: {row['source_id']}",
            f"Title: {row['document_title']}",
            f"PDF viewer page: {row['viewer_page']}",
            f"Printed Bundle page: {row['printed_bundle_page'] or 'not extracted'}",
            f"Existing visible lines: {row['line_start']}-{row['line_end']}",
            f"Search query/categories: {row['search_terms']} [{row['search_categories']}]",
            f"Context before: {row['context_before']}",
            f"Passage (verbatim candidate): {row['candidate_text']}",
            f"Context after: {row['context_after']}",
            f"PDF review flags: {row['page_review_flags'] or 'none'}",
            "Human verified: No | Rating: blank",
        ])
    lines.extend([
        "",
        "WIDER CORPUS ACCESS:",
        "- policy_index.csv contains PDF-extracted page text and existing line-label maps for all five universities.",
        "- Use the local Streamlit app's Full-text search page or search the complete policy_index.csv.",
        "- Filter by university before making any university-specific comparison.",
        "- Search log contains each actual query and each indexed result; zero-hit queries are retained.",
        "- source_audit.csv contains the package-register audit for all five universities.",
        "- Check preparation_validation_report.txt for completed, failed and pending tasks.",
    ])
    return "\n".join(lines) + "\n"


def _preserve_exports(root: Path, checkpoint: Checkpoint) -> list[str]:
    expected = json.loads(checkpoint.get_metadata("original_export_hashes") or "{}")
    actual = {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted((root / "data" / "exports").glob("*"))
        if path.is_file()
    }
    differences = []
    for name in sorted(set(expected) | set(actual)):
        if expected.get(name) != actual.get(name):
            differences.append(name)
    return differences


def _write_output_task(
    root: Path,
    checkpoint: Checkpoint,
    task_id: str,
    filename: str,
    content: bytes,
) -> None:
    path = root / "data" / "preparation" / filename
    expected_hash = _sha256_bytes(content)
    task = checkpoint.task(task_id)
    if task["status"] == "completed":
        saved = checkpoint.result(task_id) or {}
        if path.is_file() and sha256_file(path) == saved.get("sha256") == expected_hash:
            return
        checkpoint.mark_failed_without_retry(
            task_id,
            "Previously completed output is missing or changed; it was not overwritten.",
        )
        _maybe_write_progress_report(root, checkpoint, force=True)
        return

    def operation() -> dict[str, Any]:
        if path.exists():
            existing = _sha256_bytes(path.read_bytes())
            if existing != expected_hash:
                raise FileExistsError(
                    f"Existing output differs from the checkpointed result; refusing to overwrite: {filename}"
                )
            return {"path": filename, "sha256": expected_hash, "preserved_existing": True}
        _atomic_replace(path, content, allow_replace=False)
        return {"path": filename, "sha256": expected_hash, "preserved_existing": False}

    _run_task(checkpoint, task_id, operation, root)


def _full_report(root: Path, checkpoint: Checkpoint, pages: list[dict[str, Any]],
                 audit_rows: list[dict[str, Any]], search_rows: list[dict[str, Any]],
                 candidates: list[dict[str, Any]], export_differences: list[str],
                 input_differences: list[str]) -> None:
    rows = checkpoint.connection.execute(
        "SELECT task_id, task_type, scope, status, attempts, error_text FROM tasks ORDER BY task_id"
    ).fetchall()
    counts = {status: sum(row["status"] == status for row in rows)
              for status in ("completed", "failed", "pending", "running")}
    status = "complete" if counts["failed"] == counts["pending"] == counts["running"] == 0 else "incomplete"
    if export_differences or input_differences:
        status = "incomplete"
    if checkpoint.get_metadata("finalized_at") is None:
        checkpoint.set_metadata("finalized_at", _utc_now())
    page_counts: dict[str, int] = defaultdict(int)
    for page in pages:
        page_counts[page["mapping_status"]] += 1
    ambiguous_table_pages = sum("ambiguous_table" in row["extraction_flags"] for row in pages)
    extraction_problem_pages = sum(
        any("extraction" in str(flag) or "no_extractable_text" in str(flag)
            for flag in row["extraction_flags"])
        for row in pages
    )
    errors = [
        f"- {row['task_id']} ({row['attempts']} attempts): {row['error_text']}"
        for row in rows if row["status"] == "failed"
    ]
    pending = [
        f"- {row['task_id']} [{row['task_type']}, {row['scope']}]"
        for row in rows if row["status"] in {"pending", "running"}
    ]
    report_write_failures = json.loads(
        checkpoint.get_metadata("progress_report_write_failures") or "[]"
    )
    search_query_count = len(SEARCH_PLAN["S1"]) + len(SEARCH_PLAN["S6"]) + len(SEARCH_PLAN["S13"])
    zero_queries = sorted({
        f"{row.get('scenario_id')} {row.get('query_id')}: {row.get('query')}"
        for row in search_rows if row.get("result_count") == 0
    })
    text = [
        "PREPARATION VALIDATION REPORT",
        f"Run started: {checkpoint.get_metadata('run_started_at')}",
        f"Finalized: {checkpoint.get_metadata('finalized_at')}",
        f"Policy cut-off: {POLICY_CUTOFF}",
        f"Overall status: {status}",
        "",
        "TASK COUNTS:",
        f"Completed: {counts['completed']}",
        f"Failed after {MAX_RETRIES} retries: {counts['failed']}",
        f"Pending: {counts['pending']}",
        f"Running at finalization: {counts['running']}",
        f"Total recorded task attempts: {checkpoint.connection.execute('SELECT COUNT(*) FROM task_attempts').fetchone()[0]}",
        "",
        "INPUT PRESERVATION:",
        "All five original ZIP hashes and extracted-file hashes were verified against data/derived/file_hashes.json.",
        "The indexed SQLite database was opened read-only; no coding records were changed.",
        "Original data/exports hashes: " + ("UNCHANGED" if not export_differences else "CHANGED: " + ", ".join(export_differences)),
        "Original package/extracted-file hashes: " + ("UNCHANGED" if not input_differences else "CHANGED/UNVERIFIED: " + "; ".join(input_differences)),
        "The preparation process does not write to original package ZIPs or data/exports.",
        "",
        "INDEX VERIFICATION:",
        f"PDF page tasks represented in policy_index.csv: {len(pages)}",
        f"PDF-to-index mappings verified with visible line labels: {page_counts['verified']}",
        f"Mappings verified but PDF has no visible L-labels: {page_counts['verified_no_visible_L_labels']}",
        f"Page mappings/extraction tasks failed or mismatched: {page_counts['failed']}",
        f"Pages flagged as ambiguous tables: {ambiguous_table_pages}",
        f"Pages with extraction-related flags: {extraction_problem_pages}",
        "Every row retains its own viewer page; printed Bundle page is taken only from the PDF footer.",
        "Visible L-labels are not synthesized. Pages without them cannot become line-located candidates.",
        "Pages flagged ambiguous_table or with extraction/index issues remain flagged for human review.",
        "",
        "SOURCE AUDIT:",
        f"Register rows represented: {len(audit_rows)}",
        f"Rows with unknown effective date: {sum(row.get('effective_date_status') == 'unknown_not_recorded' for row in audit_rows)}",
        f"Rows with unknown operative version: {sum(row.get('operative_version_status') == 'unknown_not_recorded' for row in audit_rows)}",
        f"Rows with unknown supersession status: {sum(row.get('supersession_status') == 'unknown_not_recorded' for row in audit_rows)}",
        "Register blanks are reported as unknown/unreviewed; retrieval dates were not converted to effective dates.",
        "Exact duplicate candidates are not treated as confirmed duplicates. Unmatched linked IDs require manual checking.",
        "",
        "SEARCH AND CANDIDATES:",
        f"Scenario query tasks planned: {search_query_count}",
        f"Search result rows (including zero-hit query rows): {len(search_rows)}",
        f"Candidate passages: {len(candidates)}",
        "All candidate ratings are blank and human_verified is No.",
        "Searches are AND-token searches through the existing complete SQLite FTS index for Curtin.",
        "Zero-hit queries:",
        *([f"- {value}" for value in zero_queries] if zero_queries else ["- None."]),
        "",
        "PROTOCOL STATUS:",
        "No scores or final policy judgments were created.",
        "Critical-risk criteria remain unadopted/unset; this run only indexes and retrieves evidence.",
        "Scenario text, codebook, and original package files were not edited.",
        "",
        "PERSISTENT FAILURES:",
        *(errors or ["- None recorded."]),
        "",
        "PERSISTENT PROGRESS-REPORT WRITE FAILURES:",
        *(
            [f"- {value}" for value in report_write_failures]
            if report_write_failures else ["- None recorded."]
        ),
        "",
        "PENDING TASKS:",
        *(pending or ["- None."]),
        "",
        "DELIVERABLES:",
        "- policy_index.csv",
        "- source_audit.csv",
        "- search_log.csv",
        "- evidence_candidates.csv",
        "- AI_handoff_Curtin_S1.txt, AI_handoff_Curtin_S6.txt, AI_handoff_Curtin_S13.txt",
        "- .preparation_checkpoint.sqlite3 (restart-safe task/checkpoint history)",
    ]
    path = root / "data" / "preparation" / "preparation_validation_report.txt"
    _atomic_replace(path, ("\n".join(text) + "\n").encode("utf-8"), allow_replace=True)


def run_preparation(root: Path, *, interrupt_after_pages: int | None = None) -> dict[str, Any]:
    root = root.resolve()
    output_dir = root / "data" / "preparation"
    output_dir.mkdir(parents=True, exist_ok=True)
    package_hashes, export_hashes = _verify_package_inputs(root)
    signature = _input_signature(root, package_hashes)
    registers = _source_registers(root)
    database_path = root / "data" / "policy_coding.sqlite3"
    connection = _readonly_database(database_path)
    checkpoint_path = output_dir / ".preparation_checkpoint.sqlite3"
    checkpoint = Checkpoint(checkpoint_path, signature, package_hashes, export_hashes)
    completed_page_tasks_this_run = 0
    try:
        _make_task_plan(checkpoint, registers)
        _maybe_write_progress_report(root, checkpoint, force=True)

        for university in UNIVERSITIES:
            _run_task(
                checkpoint,
                f"pdf_open:{university}",
                lambda university=university: _pdf_open(root, university),
                root,
            )
            _ensure_page_tasks(connection, checkpoint, university)
            _maybe_write_progress_report(root, checkpoint, force=True)

        for university in UNIVERSITIES:
            count = _ensure_page_tasks(connection, checkpoint, university)
            page_documents: fitz.Document | None = None
            if checkpoint.task(f"pdf_open:{university}")["status"] == "completed":
                open_result = checkpoint.result(f"pdf_open:{university}")
                if open_result is not None:
                    pdf_path = root / open_result["pdf_path"]
                    try:
                        page_documents = fitz.open(pdf_path)
                    except Exception as exc:
                        checkpoint.set_metadata(
                            f"pdf_session_warning:{university}",
                            f"{type(exc).__name__}: {exc}; page tasks will reopen individually.",
                        )
                        _maybe_write_progress_report(root, checkpoint, force=True)
            for viewer_page in range(1, count + 1):
                task_id = f"pdf_page:{university}:{viewer_page:04d}"
                if checkpoint.task(task_id)["status"] == "completed":
                    continue
                open_task = checkpoint.task(f"pdf_open:{university}")
                if open_task["status"] == "failed":
                    checkpoint.mark_failed_without_retry(
                        task_id,
                        f"PDF open task failed after retries: {open_task['error_text']}",
                    )
                    _maybe_write_progress_report(root, checkpoint, force=True)
                else:
                    _page_task_result(
                        root, connection, checkpoint, university, viewer_page, page_documents
                    )
                if checkpoint.task(task_id)["status"] == "completed":
                    completed_page_tasks_this_run += 1
                    if interrupt_after_pages is not None and completed_page_tasks_this_run >= interrupt_after_pages:
                        raise SimulatedInterruption(
                            f"Simulated interruption after {completed_page_tasks_this_run} page checkpoints."
                        )
            if page_documents is not None:
                page_documents.close()
            _maybe_write_progress_report(root, checkpoint, force=True)

        pages = _materialize_pages(checkpoint, connection, root)
        for university, register in registers.items():
            for source_id, record in register.items():
                task_id = f"source_audit:{university}:{source_id}"
                _run_task(
                    checkpoint,
                    task_id,
                    lambda university=university, source_id=source_id, record=record, register=register:
                        _source_audit_row(university, source_id, record, register, pages),
                    root,
                )

        verified_pages = {
            (page["university"], int(page["viewer_page"])): page
            for page in pages if page["mapping_status"] != "failed"
        }
        for scenario_id, specs in SEARCH_PLAN.items():
            for query_index, (category, query) in enumerate(specs, start=1):
                _query_task(
                    connection, checkpoint, scenario_id, query_index, category, query,
                    verified_pages, root,
                )

        audit_rows = _materialize_audit(checkpoint, registers)
        search_rows, candidates = _materialize_searches(checkpoint)
        for scenario_id in SCENARIO_IDS:
            task_id = f"handoff:Curtin:{scenario_id}"
            filename = f"AI_handoff_Curtin_{scenario_id}.txt"
            text = _handoff_text(
                connection, scenario_id, audit_rows, search_rows, candidates
            )
            _write_output_task(root, checkpoint, task_id, filename, text.encode("utf-8"))

        output_specs = [
            ("output:policy_index.csv", "policy_index.csv",
             _csv_bytes([
                 {
                     **row,
                     "visible_line_labels": ", ".join(row["visible_line_labels"]),
                     "line_text_json": _json(row["line_text_json"]),
                     "extraction_flags": "; ".join(row["extraction_flags"]),
                 }
                 for row in pages
             ], PAGE_COLUMNS)),
            ("output:source_audit.csv", "source_audit.csv",
             _csv_bytes(audit_rows, AUDIT_COLUMNS)),
            ("output:search_log.csv", "search_log.csv",
             _csv_bytes(search_rows, SEARCH_COLUMNS)),
            ("output:evidence_candidates.csv", "evidence_candidates.csv",
             _csv_bytes(candidates, CANDIDATE_COLUMNS)),
        ]
        for task_id, filename, content in output_specs:
            _write_output_task(root, checkpoint, task_id, filename, content)

        export_differences = _preserve_exports(root, checkpoint)
        input_differences: list[str] = []
        try:
            final_package_hashes, final_export_hashes = _verify_package_inputs(root)
            if final_package_hashes != package_hashes:
                input_differences.append("package or extracted-file hashes changed during preparation")
            if final_export_hashes != export_hashes:
                export_differences.append("data/exports changed during preparation")
            if _input_signature(root, final_package_hashes) != signature:
                input_differences.append("indexed database hash changed during preparation")
        except Exception as exc:
            input_differences.append(f"{type(exc).__name__}: {exc}")
        _full_report(
            root, checkpoint, pages, audit_rows, search_rows, candidates,
            export_differences, input_differences,
        )
        counts = {
            status: checkpoint.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status=?", (status,)
            ).fetchone()[0]
            for status in ("completed", "failed", "pending", "running")
        }
        return {
            "status": "complete" if counts["failed"] == counts["pending"] == counts["running"] == 0
            and not export_differences and not input_differences else "incomplete",
            "task_counts": counts,
            "page_count": len(pages),
            "verified_pages": sum(row["mapping_status"] == "verified" for row in pages),
            "failed_pages": sum(row["mapping_status"] == "failed" for row in pages),
            "source_audit_rows": len(audit_rows),
            "search_rows": len(search_rows),
            "candidate_rows": len(candidates),
            "output_dir": str(output_dir),
        }
    finally:
        connection.close()
        checkpoint.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build resumable, read-only policy evidence-preparation outputs."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = run_preparation(args.root)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
