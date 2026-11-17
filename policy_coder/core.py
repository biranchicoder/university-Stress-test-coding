from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


UNIVERSITIES = {
    "Curtin": "Curtin University",
    "ECU": "Edith Cowan University",
    "Murdoch": "Murdoch University",
    "Notre_Dame": "The University of Notre Dame Australia",
    "UWA": "The University of Western Australia",
}
SCENARIO_IDS = tuple(f"S{i}" for i in range(1, 16))
DIMENSIONS = tuple(f"D{i}" for i in range(1, 7))
DIMENSION_LABELS = {
    "D1": ("Scope", "Does the policy cover this actor and action?"),
    "D2": ("Authority", "Who is authorised to decide, and on what basis?"),
    "D3": ("Coherence", "Do the controlling documents agree?"),
    "D4": ("Procedure", "Can a person follow the process?"),
    "D5": ("Safeguards", "Are the agreed risks protected?"),
    "D6": ("Adaptation", "How would the university notice and respond to change?"),
}
POLICY_CUTOFF = date(2026, 11, 12)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    university: str
    source_id: str
    viewer_page: int = Field(ge=1)
    line_start: str
    line_end: str
    kind: Literal["quotation", "paraphrase"]
    quotation_text: str = ""
    paraphrase_text: str = ""
    operative_at_cutoff: bool = False
    operative_version_evidence: str = ""
    reading_order_verified: bool = False

    @field_validator("line_start", "line_end")
    @classmethod
    def valid_line_label(cls, value: str) -> str:
        if not re.fullmatch(r"L\d{3}", value):
            raise ValueError("Use an existing visible L-number label from the indexed page.")
        return value

    @field_validator("quotation_text", "paraphrase_text", "operative_version_evidence")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()


class HumanRating(BaseModel):
    model_config = ConfigDict(extra="forbid")

    university: str
    case_id: str
    dimension: Literal["D1", "D2", "D3", "D4", "D5", "D6"]
    coder: str = Field(min_length=1)
    score: int | None = Field(default=None, ge=0, le=3)
    search_terms: str = ""
    documents_checked: str = ""
    reason: str = ""
    uncertainty_note: str = ""

    @model_validator(mode="after")
    def scored_rating_has_audit_fields(self) -> "HumanRating":
        if self.score is not None:
            missing = [
                name for name in ("search_terms", "documents_checked", "reason")
                if not getattr(self, name).strip()
            ]
            if missing:
                raise ValueError(
                    "A scored rating requires search terms, documents checked, and a reason."
                )
        return self


class CaseSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    university: str
    case_id: str
    coder: str = Field(min_length=1)
    mode: Literal["Prescribing", "Delegating", "None", ""] = ""
    review_route: Literal["Yes", "No", ""] = ""
    review_route_evidence_id: int | None = None
    review_route_explanation: str = ""
    critical_gap: Literal["Yes", "No", "Uncertain", ""] = ""
    specific_gap_evidence: str = ""
    evidence_status: Literal["Sufficient", "Insufficient", ""] = ""
    insufficient_evidence_reason: str = ""
    rule_direction: Literal["Permission", "Prohibition", "Conditional", "Unclear", ""] = ""
    conflict_a_id: int | None = None
    conflict_b_id: int | None = None
    conflict_confirmed: bool = False
    conflict_explanation: str = ""
    case_note: str = ""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            university TEXT NOT NULL,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            register_json TEXT NOT NULL,
            PRIMARY KEY (university, source_id)
        );
        CREATE TABLE IF NOT EXISTS pages (
            university TEXT NOT NULL,
            viewer_page INTEGER NOT NULL,
            bundle_page INTEGER,
            source_id TEXT,
            text TEXT NOT NULL,
            lines_json TEXT NOT NULL,
            flags_json TEXT NOT NULL,
            PRIMARY KEY (university, viewer_page)
        );
        CREATE TABLE IF NOT EXISTS source_review (
            university TEXT NOT NULL,
            source_id TEXT NOT NULL,
            inclusion TEXT NOT NULL DEFAULT '',
            inclusion_reason TEXT NOT NULL DEFAULT '',
            authority TEXT NOT NULL DEFAULT '',
            effective_date TEXT NOT NULL DEFAULT '',
            operative_version TEXT NOT NULL DEFAULT '',
            supersedes TEXT NOT NULL DEFAULT '',
            reviewer TEXT NOT NULL DEFAULT '',
            missing_instruments TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (university, source_id),
            FOREIGN KEY (university, source_id) REFERENCES documents(university, source_id)
        );
        CREATE TABLE IF NOT EXISTS resources (
            university TEXT PRIMARY KEY,
            codebook_text TEXT NOT NULL,
            scenarios_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS protocol_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            cutoff_date TEXT NOT NULL,
            coder TEXT NOT NULL,
            critical_risks_json TEXT NOT NULL,
            notes TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rating_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            university TEXT NOT NULL,
            case_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            coder TEXT NOT NULL,
            revision INTEGER NOT NULL,
            protocol_version_id INTEGER NOT NULL,
            score INTEGER,
            search_terms TEXT NOT NULL,
            documents_checked TEXT NOT NULL,
            reason TEXT NOT NULL,
            uncertainty_note TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (university, case_id, dimension, coder, revision),
            FOREIGN KEY (protocol_version_id) REFERENCES protocol_versions(id)
        );
        CREATE TABLE IF NOT EXISTS evidence_passages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            university TEXT NOT NULL,
            case_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            coder TEXT NOT NULL,
            source_id TEXT NOT NULL,
            viewer_page INTEGER NOT NULL,
            line_start TEXT NOT NULL,
            line_end TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('quotation', 'paraphrase')),
            quotation_text TEXT NOT NULL DEFAULT '',
            paraphrase_text TEXT NOT NULL DEFAULT '',
            operative_at_cutoff INTEGER NOT NULL DEFAULT 0,
            operative_version_evidence TEXT NOT NULL DEFAULT '',
            reading_order_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (university, source_id) REFERENCES documents(university, source_id)
        );
        CREATE TABLE IF NOT EXISTS case_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            university TEXT NOT NULL,
            case_id TEXT NOT NULL,
            coder TEXT NOT NULL,
            revision INTEGER NOT NULL,
            protocol_version_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            review_route TEXT NOT NULL,
            review_route_evidence_id INTEGER,
            review_route_explanation TEXT NOT NULL DEFAULT '',
            critical_gap TEXT NOT NULL,
            specific_gap_evidence TEXT NOT NULL DEFAULT '',
            evidence_status TEXT NOT NULL,
            insufficient_evidence_reason TEXT NOT NULL DEFAULT '',
            rule_direction TEXT NOT NULL,
            conflict_a_id INTEGER,
            conflict_b_id INTEGER,
            conflict_confirmed INTEGER NOT NULL DEFAULT 0,
            conflict_explanation TEXT NOT NULL,
            case_note TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (university, case_id, coder, revision),
            FOREIGN KEY (protocol_version_id) REFERENCES protocol_versions(id),
            FOREIGN KEY (conflict_a_id) REFERENCES evidence_passages(id),
            FOREIGN KEY (conflict_b_id) REFERENCES evidence_passages(id)
        );
        CREATE TABLE IF NOT EXISTS ai_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            university TEXT NOT NULL,
            case_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            coder TEXT NOT NULL,
            prompt TEXT NOT NULL,
            suggestion TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS search_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coder TEXT NOT NULL,
            university TEXT NOT NULL,
            case_id TEXT NOT NULL,
            dimension TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('search', 'document_checked')),
            query TEXT NOT NULL DEFAULT '',
            source_id TEXT,
            viewer_page INTEGER,
            bundle_page INTEGER,
            result_count INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (university, source_id) REFERENCES documents(university, source_id)
        );
        """
    )
    evidence_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(evidence_passages)").fetchall()
    }
    if "operative_version_evidence" not in evidence_columns:
        conn.execute(
            "ALTER TABLE evidence_passages ADD COLUMN operative_version_evidence TEXT NOT NULL DEFAULT ''"
        )
    if "reading_order_verified" not in evidence_columns:
        conn.execute(
            "ALTER TABLE evidence_passages ADD COLUMN reading_order_verified INTEGER NOT NULL DEFAULT 0"
        )
    case_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(case_revisions)").fetchall()
    }
    for column, definition in (
        ("review_route_evidence_id", "INTEGER"),
        ("review_route_explanation", "TEXT NOT NULL DEFAULT ''"),
        ("specific_gap_evidence", "TEXT NOT NULL DEFAULT ''"),
        ("insufficient_evidence_reason", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in case_columns:
            conn.execute(f"ALTER TABLE case_revisions ADD COLUMN {column} {definition}")
    search_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(search_activity)").fetchall()
    }
    if "result_count" not in search_columns:
        conn.execute("ALTER TABLE search_activity ADD COLUMN result_count INTEGER")
    fts_definition = conn.execute(
        "SELECT type, sql FROM sqlite_master WHERE name='page_fts'"
    ).fetchone()
    if fts_definition and "using fts5" not in (fts_definition["sql"] or "").lower():
        conn.execute("DROP TABLE page_fts")
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
            university UNINDEXED, viewer_page UNINDEXED, source_id UNINDEXED, text
        )"""
    )
    if conn.execute("SELECT COUNT(*) FROM protocol_versions").fetchone()[0] == 0:
        conn.execute(
            """INSERT INTO protocol_versions
            (version, cutoff_date, coder, critical_risks_json, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "1.0",
                POLICY_CUTOFF.isoformat(),
                "Coder 1",
                "{}",
                "Critical-risk criteria are intentionally unagreed and blank.",
                utc_now(),
            ),
        )
    conn.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def latest_protocol(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM protocol_versions ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("Protocol initialization failed.")
    return row


def validate_citation(conn: sqlite3.Connection, citation: Citation) -> list[str]:
    problems: list[str] = []
    document = conn.execute(
        "SELECT 1 FROM documents WHERE university=? AND source_id=?",
        (citation.university, citation.source_id),
    ).fetchone()
    if document is None:
        problems.append("Source ID does not belong to this university's indexed register.")
        return problems

    page = conn.execute(
        "SELECT source_id, lines_json, text, flags_json FROM pages WHERE university=? AND viewer_page=?",
        (citation.university, citation.viewer_page),
    ).fetchone()
    if page is None:
        problems.append("Viewer page is not present in the verified PDF index.")
        return problems
    if page["source_id"] and page["source_id"] != citation.source_id:
        problems.append(
            f"Page footer identifies {page['source_id']}, not selected source {citation.source_id}."
        )
    lines: dict[str, str] = json.loads(page["lines_json"])
    labels = sorted(lines, key=lambda label: int(label[1:]))
    if citation.line_start not in lines or citation.line_end not in lines:
        problems.append("Line range must use existing visible L-number labels on this page.")
    elif int(citation.line_start[1:]) > int(citation.line_end[1:]):
        problems.append("Line range start must not be after its end.")
    elif any(
        f"L{number:03d}" not in lines
        for number in range(int(citation.line_start[1:]), int(citation.line_end[1:]) + 1)
    ):
        problems.append("Line range crosses a missing visible L-number label in the extracted index.")
    if citation.kind == "quotation":
        if not citation.quotation_text:
            problems.append("Quotation text is missing.")
        else:
            page_flags = json.loads(page["flags_json"])
            order_review_required = (
                "scrambled_reading_order" in page_flags or "ambiguous_table" in page_flags
            )
            if order_review_required:
                if not citation.reading_order_verified:
                    problems.append(
                        "This table page requires visual reading-order verification before accepting a quotation."
                    )
                page_text = " ".join(page["text"].split())
                quotation_text = " ".join(citation.quotation_text.split())
                if quotation_text not in page_text:
                    problems.append(
                        "Quotation must be manually checked and copied as a contiguous passage from the PDF page."
                    )
            else:
                expected = indexed_quote(lines, citation.line_start, citation.line_end)
                if citation.quotation_text != expected:
                    problems.append("Quotation differs from the text copied from the verified index.")
    elif not citation.paraphrase_text:
        problems.append("Paraphrase text is required when the passage is not a quotation.")

    review = conn.execute(
        "SELECT effective_date FROM source_review WHERE university=? AND source_id=?",
        (citation.university, citation.source_id),
    ).fetchone()
    if citation.operative_at_cutoff:
        if not citation.operative_version_evidence.strip():
            problems.append(
                "Marking a passage operative at cut-off requires recorded version evidence."
            )
        if review and review["effective_date"]:
            try:
                if date.fromisoformat(review["effective_date"]) > POLICY_CUTOFF:
                    problems.append("A source effective after the policy cut-off cannot be marked operative at cut-off.")
            except ValueError:
                problems.append("Source effective date must be ISO YYYY-MM-DD or blank.")
    return problems


def indexed_quote(lines: dict[str, str], line_start: str, line_end: str) -> str:
    start, end = int(line_start[1:]), int(line_end[1:])
    return "\n".join(
        lines.get(f"L{number:03d}", "")
        for number in range(start, end + 1)
        if f"L{number:03d}" in lines
    ).strip()


def latest_rating(
    conn: sqlite3.Connection, university: str, case_id: str, dimension: str, coder: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM rating_revisions
        WHERE university=? AND case_id=? AND dimension=? AND coder=?
        ORDER BY revision DESC LIMIT 1""",
        (university, case_id, dimension, coder),
    ).fetchone()


def latest_summary(
    conn: sqlite3.Connection, university: str, case_id: str, coder: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM case_revisions
        WHERE university=? AND case_id=? AND coder=?
        ORDER BY revision DESC LIMIT 1""",
        (university, case_id, coder),
    ).fetchone()


def case_outcome(
    scores: dict[str, int | None],
    summary: dict[str, Any],
    conflict_valid: bool = False,
) -> str:
    if summary.get("evidence_status") == "Insufficient":
        return "Unassessable"
    if summary.get("critical_gap") == "Uncertain":
        return "Unassessable"
    if summary.get("evidence_status") != "Sufficient":
        return "Pending"
    if any(scores.get(d) is None for d in DIMENSIONS):
        return "Pending"
    required = ("mode", "review_route", "critical_gap", "rule_direction")
    if any(not summary.get(field) for field in required):
        return "Pending"
    d1, d2, d3, d4, d5, _d6 = (scores[d] for d in DIMENSIONS)
    if d3 == 0:
        return "Documentary conflict" if conflict_valid and summary.get("conflict_explanation", "").strip() else "Pending"
    if d1 == 0 or d2 <= 1 or d4 == 0 or d5 == 0 or summary["critical_gap"] == "Yes":
        return "Indeterminate"
    if (
        summary["mode"] == "Prescribing"
        and d1 == 3 and d2 == 3 and d3 >= 2 and d4 == 3 and d5 == 3
        and summary["review_route"] == "Yes" and summary["critical_gap"] == "No"
    ):
        return "Resolved"
    if (
        summary["mode"] == "Delegating"
        and d1 >= 1 and d2 >= 2 and d3 >= 1 and d4 >= 2 and d5 >= 2
        and summary["review_route"] == "Yes" and summary["critical_gap"] == "No"
    ):
        return "Structured discretion"
    return "Indeterminate"


def validate_outcome_inputs(
    conn: sqlite3.Connection,
    university: str,
    case_id: str,
    coder: str,
    scores: dict[str, int | None],
    summary: dict[str, Any],
) -> tuple[list[str], list[str], bool]:
    errors: list[str] = []
    warnings: list[str] = []
    passages = conn.execute(
        """SELECT * FROM evidence_passages
        WHERE university=? AND case_id=? AND coder=? ORDER BY id""",
        (university, case_id, coder),
    ).fetchall()
    conflict_valid = False
    if summary.get("review_route") == "Yes":
        evidence_id = summary.get("review_route_evidence_id")
        if evidence_id is None:
            errors.append("A Yes review route requires a supporting evidence citation.")
        else:
            route_evidence = conn.execute(
                """SELECT 1 FROM evidence_passages WHERE id=? AND university=?
                AND case_id=? AND coder=?""",
                (evidence_id, university, case_id, coder),
            ).fetchone()
            if route_evidence is None:
                errors.append("Review-route evidence must belong to this university, case, and coder.")
    if scores.get("D3") == 0:
        if summary.get("conflict_a_id") is None or summary.get("conflict_b_id") is None:
            errors.append("D3 = 0 requires citations to two incompatible operative provisions.")
        else:
            by_id = {row["id"]: row for row in passages}
            a, b = by_id.get(summary["conflict_a_id"]), by_id.get(summary["conflict_b_id"])
            distinct_citations = (
                a is not None
                and b is not None
                and (a["source_id"], a["viewer_page"], a["line_start"], a["line_end"])
                != (b["source_id"], b["viewer_page"], b["line_start"], b["line_end"])
            )
            valid_pair = (
                a is not None and b is not None and a["id"] != b["id"]
                and distinct_citations
                and a["operative_at_cutoff"] and b["operative_at_cutoff"]
                and a["operative_version_evidence"].strip()
                and b["operative_version_evidence"].strip()
            )
            if not valid_pair:
                errors.append("D3 = 0 requires two distinct cited provisions, each verified operative at cut-off.")
            if not summary.get("conflict_confirmed"):
                errors.append("Confirm that the two cited operative provisions are incompatible and the conflict is unresolved.")
            if not summary.get("conflict_explanation", "").strip():
                errors.append("D3 = 0 requires a written explanation of the unresolved conflict.")
            conflict_valid = bool(
                valid_pair
                and summary.get("conflict_confirmed")
                and summary.get("conflict_explanation", "").strip()
            )
    elif summary.get("conflict_a_id") or summary.get("conflict_b_id") or summary.get("conflict_explanation"):
        warnings.append("Conflict provisions are recorded although D3 is not 0.")

    if summary.get("evidence_status") == "Insufficient" and any(v == 0 for v in scores.values()):
        errors.append("Insufficient evidence conflicts with a zero score; missing evidence is not a zero.")
    if summary.get("mode") == "None" and summary.get("review_route") == "Yes":
        warnings.append("Mode is None while an applicable review route is marked Yes; check for contradiction.")
    if summary.get("rule_direction") == "Unclear" and scores.get("D1") == 3 and scores.get("D4") == 3:
        warnings.append("Rule direction is Unclear despite high scope and procedure scores; review the entered fields.")
    if summary.get("critical_gap") == "Yes" and summary.get("evidence_status") == "Insufficient":
        warnings.append("Critical gap is asserted while evidence is Insufficient; check that this is not inferred from absence.")
    return errors, warnings, conflict_valid


def codebook_workbook_report(conn: sqlite3.Connection) -> dict[str, Any]:
    report: dict[str, Any] = {
        "policy_cutoff": POLICY_CUTOFF.isoformat(),
        "workbooks": {},
        "differences": [
            {
                "rule": "D3 = 0 documentary conflict",
                "codebook": "Requires two incompatible operative provisions and a written explanation that hierarchy, exceptions, or supersession do not resolve the conflict.",
                "template_formula": "Checks D3=0 and nonblank conflict_basis only; does not validate two citations, operative status, distinct provisions, or substantive incompatibility.",
                "application": "Adds distinct-passage citation, operative-at-cutoff, incompatibility confirmation, and explanation validation; does not alter original template formulas.",
            },
            {
                "rule": "Missing evidence is not a zero",
                "codebook": "Insufficient evidence is Unassessable; blank scores are unfinished, not zero.",
                "template_formula": "Blank scores produce Pending; explicit Insufficient evidence produces Unassessable.",
                "application": "Matches this branch and flags zero scores entered with Insufficient evidence.",
            },
            {
                "rule": "Critical gap uncertainty precedes incomplete score fields",
                "codebook": "Critical gap = Uncertain gives Unassessable at the evidence-first stage.",
                "template_formula": "Checks Critical gap = Uncertain only after all six scores and required summary fields are present; otherwise it can return Pending.",
                "application": "Returns Unassessable for Critical gap = Uncertain before checking score completeness.",
            },
            {
                "rule": "Score values must be 0 through 3",
                "codebook": "Each D1-D6 score has only the values 0, 1, 2, and 3.",
                "template_formula": "The formulas check for six numeric scores but do not validate their range.",
                "application": "Pydantic constrains every entered score to the codebook range before saving or exporting.",
            },
            {
                "rule": "Contradictory inputs",
                "codebook": "Treats mode, review route, evidence status, critical gap, and rule direction as separate case fields whose stated conditions govern the outcome.",
                "template_formula": "Calculates a label from cell values but does not detect contradictions such as Mode=None with Review route=Yes.",
                "application": "Flags contradictory field combinations and surfaces the warning without silently rewriting the human inputs.",
            },
        ],
    }
    return report


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

