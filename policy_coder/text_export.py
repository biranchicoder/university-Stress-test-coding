from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from policy_coder.core import (
    DIMENSIONS,
    DIMENSION_LABELS,
    UNIVERSITIES,
    Citation,
    case_outcome,
    connect,
    initialize_db,
    latest_protocol,
    validate_citation,
    validate_outcome_inputs,
)


DIMENSION_FIELDS = (
    "case_id",
    "university",
    "scenario_id",
    "dimension",
    "search_terms",
    "documents_checked",
    "score",
    "your_source_ids",
    "your_pages_lines",
    "quotation_or_paraphrase",
    "reason_for_score",
    "negative_search_or_access_note",
)
CASE_TEMPLATE_FIELDS = (
    "mode",
    "review_route",
    "critical_gap",
    "evidence_status",
    "rule_direction",
    "conflict_basis",
    "case_note",
)


class ExportValidationError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def _template_rows(root: Path, university: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches = list((root / "data" / "raw" / university).glob("Human_Coder_1_*.xlsx"))
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected exactly one supplied workbook for {university}.")
    workbook = load_workbook(matches[0], data_only=False, read_only=False)
    if "Cases" not in workbook.sheetnames or "Evidence" not in workbook.sheetnames:
        raise ValueError(f"{university}: supplied workbook must contain Cases and Evidence sheets.")
    case_sheet = workbook["Cases"]
    evidence_sheet = workbook["Evidence"]
    case_header_row = _find_header_row(case_sheet, "case_id")
    evidence_header_row = _find_header_row(evidence_sheet, "case_id")
    case_headers = _headers(case_sheet, case_header_row)
    evidence_headers = _headers(evidence_sheet, evidence_header_row)
    missing_case = [
        field for field in
        ("case_id", "university", "scenario_id", "family", "level") + CASE_TEMPLATE_FIELDS
        if field not in case_headers
    ]
    missing_evidence = [field for field in DIMENSION_FIELDS if field not in evidence_headers]
    if missing_case:
        raise ValueError(f"{university}: Cases template is missing fields {missing_case}.")
    if missing_evidence:
        raise ValueError(f"{university}: Evidence template is missing fields {missing_evidence}.")

    case_rows = []
    for row_number in range(case_header_row + 1, case_sheet.max_row + 1):
        case_id = case_sheet.cell(row_number, case_headers["case_id"]).value
        if not case_id:
            continue
        case_rows.append(
            {
                field: case_sheet.cell(row_number, column).value
                for field, column in case_headers.items()
                if field in case_headers
            }
        )
    evidence_rows = []
    for row_number in range(evidence_header_row + 1, evidence_sheet.max_row + 1):
        case_id = evidence_sheet.cell(row_number, evidence_headers["case_id"]).value
        if not case_id:
            continue
        evidence_rows.append(
            {
                field: evidence_sheet.cell(row_number, column).value
                for field, column in evidence_headers.items()
                if field in evidence_headers
            }
        )
    workbook.close()
    return case_rows, evidence_rows


def _find_header_row(sheet, required_header: str) -> int:
    for row in range(1, min(sheet.max_row, 12) + 1):
        if any(sheet.cell(row, col).value == required_header for col in range(1, sheet.max_column + 1)):
            return row
    raise ValueError(f"Sheet {sheet.title} has no {required_header!r} header.")


def _headers(sheet, row: int) -> dict[str, int]:
    return {
        sheet.cell(row, col).value: col
        for col in range(1, sheet.max_column + 1)
        if isinstance(sheet.cell(row, col).value, str)
    }


def _display(value: Any) -> str:
    return "" if value is None else str(value)


def _source_title(conn, university: str, source_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM documents WHERE university=? AND source_id=?",
        (university, source_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"Source {source_id} is not registered under {university}.")
    return row["title"]


def _format_evidence(
    conn,
    university: str,
    case_id: str,
    dimension: str,
    coder: str,
) -> tuple[list[dict[str, str]], list[str]]:
    problems: list[str] = []
    records = conn.execute(
        """SELECT * FROM evidence_passages
        WHERE university=? AND case_id=? AND dimension=? AND coder=?
        ORDER BY id""",
        (university, case_id, dimension, coder),
    ).fetchall()
    passages: list[dict[str, str]] = []
    for item in records:
        citation = Citation(
            university=item["university"],
            source_id=item["source_id"],
            viewer_page=item["viewer_page"],
            line_start=item["line_start"],
            line_end=item["line_end"],
            kind=item["kind"],
            quotation_text=item["quotation_text"],
            paraphrase_text=item["paraphrase_text"],
            operative_at_cutoff=bool(item["operative_at_cutoff"]),
            operative_version_evidence=item["operative_version_evidence"],
            reading_order_verified=bool(item["reading_order_verified"]),
        )
        problems.extend(
            f"{case_id} {dimension} evidence #{item['id']}: {problem}"
            for problem in validate_citation(conn, citation)
        )
        page = conn.execute(
            """SELECT bundle_page FROM pages
            WHERE university=? AND viewer_page=?""",
            (university, item["viewer_page"]),
        ).fetchone()
        if page is None:
            problems.append(f"{case_id} {dimension} evidence #{item['id']}: indexed page is missing.")
            continue
        if page["bundle_page"] is None:
            problems.append(
                f"{case_id} {dimension} evidence #{item['id']}: printed Bundle page is missing."
            )
        title = _source_title(conn, university, item["source_id"])
        passages.append(
            {
                "source_id": item["source_id"],
                "title": title,
                "bundle_page": (
                    str(page["bundle_page"])
                    if page["bundle_page"] is not None
                    else "Not extracted; viewer page not substituted"
                ),
                "line_numbers": f"{item['line_start']}-{item['line_end']}",
                "kind": item["kind"],
                "text": (
                    item["quotation_text"]
                    if item["kind"] == "quotation"
                    else item["paraphrase_text"]
                ),
                "operative_status": (
                    "Verified at policy cut-off"
                    if item["operative_at_cutoff"] and item["operative_version_evidence"].strip()
                    else "Unverified at policy cut-off"
                ),
                "operative_version_evidence": item["operative_version_evidence"],
                "reading_order_verified": (
                    "Yes" if item["reading_order_verified"] else "No"
                ),
            }
        )
    return passages, problems


def _format_passage(passage: dict[str, str], heading: str | None = None) -> list[str]:
    lines = [heading] if heading else []
    lines.extend(
        [
            f"Source ID: {passage['source_id']}",
            f"Document title: {passage['title']}",
            f"Printed bundle page: {passage['bundle_page']}",
            f"Line numbers: {passage['line_numbers']}",
            f"Evidence type: {'Exact quotation' if passage['kind'] == 'quotation' else 'Paraphrase'}",
        ]
    )
    if passage["kind"] == "quotation":
        lines.extend(["Exact quotation:", passage["text"]])
        if passage["reading_order_verified"] == "Yes":
            lines.append("PDF reading order: Visually verified by the coder")
    else:
        lines.extend(["Evidence:", passage["text"]])
    lines.append(f"Operative at policy cut-off: {passage['operative_status']}")
    if passage["operative_version_evidence"]:
        lines.append(
            "Recorded version evidence: " + passage["operative_version_evidence"]
        )
    return lines


def _case_reference(
    conn,
    university: str,
    evidence_id: int | None,
    heading: str,
) -> tuple[list[str], list[str]]:
    if evidence_id is None:
        return [heading, "Not recorded."], []
    row = conn.execute(
        """SELECT e.*, p.bundle_page FROM evidence_passages e
        JOIN pages p ON p.university=e.university AND p.viewer_page=e.viewer_page
        WHERE e.id=? AND e.university=?""",
        (evidence_id, university),
    ).fetchone()
    if row is None:
        return [heading, "Citation not found in the indexed university corpus."], [
            f"{heading}: referenced evidence passage is missing."
        ]
    title = _source_title(conn, university, row["source_id"])
    passage = {
        "source_id": row["source_id"],
        "title": title,
        "bundle_page": (
            str(row["bundle_page"])
            if row["bundle_page"] is not None
            else "Not extracted; viewer page not substituted"
        ),
        "line_numbers": f"{row['line_start']}-{row['line_end']}",
        "kind": row["kind"],
        "text": row["quotation_text"] if row["kind"] == "quotation" else row["paraphrase_text"],
        "operative_status": (
            "Verified at policy cut-off"
            if row["operative_at_cutoff"] and row["operative_version_evidence"].strip()
            else "Unverified at policy cut-off"
        ),
        "operative_version_evidence": row["operative_version_evidence"],
        "reading_order_verified": "Yes" if row["reading_order_verified"] else "No",
    }
    citation = Citation(
        university=row["university"],
        source_id=row["source_id"],
        viewer_page=row["viewer_page"],
        line_start=row["line_start"],
        line_end=row["line_end"],
        kind=row["kind"],
        quotation_text=row["quotation_text"],
        paraphrase_text=row["paraphrase_text"],
        operative_at_cutoff=bool(row["operative_at_cutoff"]),
        operative_version_evidence=row["operative_version_evidence"],
        reading_order_verified=bool(row["reading_order_verified"]),
    )
    problems = [
        f"{heading}: {problem}" for problem in validate_citation(conn, citation)
    ]
    return _format_passage(passage, heading), problems


def _rating_record(conn, university: str, case_id: str, dimension: str, coder: str):
    return conn.execute(
        """SELECT * FROM rating_revisions
        WHERE university=? AND case_id=? AND dimension=? AND coder=?
        ORDER BY revision DESC LIMIT 1""",
        (university, case_id, dimension, coder),
    ).fetchone()


def _logged_search_activity(conn, university: str, case_id: str, dimension: str, coder: str):
    rows = conn.execute(
        """SELECT a.*, d.title FROM search_activity a
        LEFT JOIN documents d ON d.university=a.university AND d.source_id=a.source_id
        WHERE a.university=? AND a.case_id=? AND a.dimension=? AND a.coder=?
        ORDER BY a.id""",
        (university, case_id, dimension, coder),
    ).fetchall()
    search_rows = [
        row for row in rows
        if row["action"] == "search" and row["query"].strip()
    ]
    documents: dict[tuple[str, str], list[str]] = {}
    seen = set()
    for row in rows:
        if row["action"] != "document_checked":
            continue
        key = (row["source_id"], row["viewer_page"])
        if key in seen:
            continue
        seen.add(key)
        document_key = (row["source_id"] or "", row["title"] or "")
        printed_page = (
            str(row["bundle_page"])
            if row["bundle_page"] is not None
            else f"not extracted (viewer page {row['viewer_page']} not substituted)"
        )
        documents.setdefault(document_key, []).append(printed_page)
    document_text = "\n".join(
        f"{source_id} — {title}, printed bundle pages checked: {', '.join(pages)}"
        for (source_id, title), pages in documents.items()
    )
    no_result_searches = list(
        dict.fromkeys(
            row["query"] for row in search_rows if row["result_count"] == 0
        )
    )
    return (
        "\n".join(row["query"] for row in search_rows),
        document_text,
        no_result_searches,
    )


def _summary_record(conn, university: str, case_id: str, coder: str):
    return conn.execute(
        """SELECT * FROM case_revisions WHERE university=? AND case_id=? AND coder=?
        ORDER BY revision DESC LIMIT 1""",
        (university, case_id, coder),
    ).fetchone()


def validate_export(
    conn,
    root: Path,
    university: str,
    coder: str,
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    incomplete: list[str] = []
    cases, evidence_rows = _template_rows(root, university)
    if len(cases) != 15:
        problems.append(f"{university}: expected 15 template cases; found {len(cases)}.")
    if len(evidence_rows) != 90:
        problems.append(f"{university}: expected 90 template dimension rows; found {len(evidence_rows)}.")
    expected_order = [f"{university}-S{i}" for i in range(1, 16)]
    if [str(row["case_id"]) for row in cases] != expected_order:
        problems.append(f"{university}: case order does not match the supplied workbook.")
    expected_evidence_order = [
        (case_id, dimension) for case_id in expected_order for dimension in DIMENSIONS
    ]
    actual_evidence_order = [
        (str(row["case_id"]), str(row["dimension"])) for row in evidence_rows
    ]
    if actual_evidence_order != expected_evidence_order:
        problems.append(f"{university}: dimension order does not match the supplied workbook.")
    protocol = latest_protocol(conn)
    risks = json.loads(protocol["critical_risks_json"])

    for row in evidence_rows:
        case_id = str(row["case_id"])
        dimension = str(row["dimension"])
        rating = _rating_record(conn, university, case_id, dimension, coder)
        activity = _logged_search_activity(conn, university, case_id, dimension, coder)
        logged_search_terms, logged_documents = activity[0], activity[1]
        citation_problems = _format_evidence(
            conn, university, case_id, dimension, coder
        )[1]
        problems.extend(citation_problems)
        if rating is not None and rating["score"] is not None:
            for value, label in (
                (logged_search_terms, "search activity"),
                (logged_documents, "document-check activity"),
                (rating["reason"], "reason"),
            ):
                if not value.strip():
                    problems.append(f"{case_id} {dimension}: scored rating has no {label}.")
            source_list = conn.execute(
                """SELECT 1 FROM evidence_passages WHERE university=? AND case_id=?
                AND dimension=? AND coder=? LIMIT 1""",
                (university, case_id, dimension, coder),
            ).fetchone()
            if source_list is None:
                incomplete.append(f"{case_id} {dimension}: no cited evidence passage.")
        elif rating is None or rating["score"] is None:
            incomplete.append(f"{case_id} {dimension}: score is blank (unfinished, not zero).")

    for row in cases:
        case_id = str(row["case_id"])
        scenario_id = str(row["scenario_id"])
        summary = _summary_record(conn, university, case_id, coder)
        if summary is None:
            incomplete.append(f"{case_id}: case summary has not been recorded.")
        else:
            required_summary_fields = (
                ("mode", "Mode"),
                ("review_route", "Review route"),
                ("review_route_explanation", "Brief explanation of which decision the route reviews"),
                ("critical_gap", "Critical gap"),
                ("evidence_status", "Evidence status"),
                ("rule_direction", "Rule direction"),
                ("case_note", "Case note"),
            )
            for field, label in required_summary_fields:
                if not str(summary[field] or "").strip():
                    incomplete.append(f"{case_id}: {label} is blank.")
            if summary["review_route"] == "Yes" and not summary["review_route_evidence_id"]:
                incomplete.append(f"{case_id}: review-route supporting citation is blank.")
            if summary["critical_gap"] == "Yes" and not summary["specific_gap_evidence"].strip():
                incomplete.append(f"{case_id}: specific critical-gap evidence is blank.")
            if summary["evidence_status"] == "Insufficient" and not summary[
                "insufficient_evidence_reason"
            ].strip():
                incomplete.append(f"{case_id}: reason for insufficient evidence is blank.")
            scores = {
                dimension: (
                    rating["score"] if (rating := _rating_record(
                        conn, university, case_id, dimension, coder
                    )) else None
                )
                for dimension in DIMENSIONS
            }
            outcome_validation = validate_outcome_inputs(
                conn, university, case_id, coder, scores, dict(summary)
            )
            problems.extend(f"{case_id}: {error}" for error in outcome_validation[0])
            incomplete.extend(
                f"{case_id}: review fields: {warning}"
                for warning in outcome_validation[1]
            )
        if not str(risks.get(scenario_id, "")).strip():
            incomplete.append(f"{case_id}: critical-risk criteria are not adopted.")
    return problems, incomplete


def render_university_text(
    conn,
    root: Path,
    university: str,
    coder: str,
    coding_date: date | None = None,
) -> tuple[str, dict[str, Any]]:
    if university not in UNIVERSITIES:
        raise ValueError(f"Unknown university: {university}")
    coding_date = coding_date or date.today()
    cases, evidence_rows = _template_rows(root, university)
    problems, incomplete = validate_export(conn, root, university, coder)
    if problems:
        raise ExportValidationError(problems)

    scenario_resource = conn.execute(
        "SELECT scenarios_json FROM resources WHERE university=?", (university,)
    ).fetchone()
    scenarios = json.loads(scenario_resource["scenarios_json"]) if scenario_resource else {}
    lines: list[str] = []
    for case in cases:
        case_id = str(case["case_id"])
        scenario_id = str(case["scenario_id"])
        scenario = scenarios.get(scenario_id, {})
        scenario_label = (
            f"{scenario_id} — {scenario['title']}"
            if scenario.get("title")
            else f"{scenario_id} — Scenario title not recorded"
        )
        case_scores: dict[str, int | None] = {}
        for dimension in DIMENSIONS:
            rating = _rating_record(conn, university, case_id, dimension, coder)
            case_scores[dimension] = rating["score"] if rating else None
            logged_search_terms, logged_documents, no_result_searches = _logged_search_activity(
                conn, university, case_id, dimension, coder
            )
            passages, evidence_validation = _format_evidence(
                conn, university, case_id, dimension, coder
            )
            problems.extend(evidence_validation)
            previous = None
            if rating and rating["revision"] > 1:
                previous = conn.execute(
                    """SELECT score FROM rating_revisions
                    WHERE university=? AND case_id=? AND dimension=? AND coder=?
                    AND revision<? ORDER BY revision DESC LIMIT 1""",
                    (university, case_id, dimension, coder, rating["revision"]),
                ).fetchone()
            record_status = (
                "Not started" if rating is None
                else "Revised" if rating["revision"] > 1
                else "Original"
            )
            negative_notes = []
            if no_result_searches:
                negative_notes.append(
                    "No indexed pages matched these searches:\n"
                    + "\n".join(no_result_searches)
                )
            if rating and rating["uncertainty_note"]:
                negative_notes.append(rating["uncertainty_note"])
            if not negative_notes:
                negative_notes.append(
                    "No negative-search, access, or uncertainty note recorded."
                )
            lines.extend(
                [
                    f"University: {university}",
                    f"Case ID: {case_id}",
                    f"Scenario: {scenario_label}",
                    f"Dimension: {dimension} — {DIMENSION_LABELS[dimension][0]}",
                    f"Coder ID: {coder}",
                    f"Coding date: {coding_date.isoformat()}",
                    f"Record status: {record_status}",
                    f"Previous score, if revised: {_display(previous['score'] if previous else None)}",
                    "",
                    f"My score: {_display(rating['score'] if rating else None)}",
                    "",
                    "Search terms used:",
                    logged_search_terms or "No searches logged.",
                    "",
                    "Documents checked:",
                    logged_documents or "No documents recorded as checked.",
                    "",
                    "MAIN EVIDENCE",
                ]
            )
            if passages:
                lines.extend(_format_passage(passages[0]))
                for passage in passages[1:]:
                    lines.extend(["", "SUPPORTING EVIDENCE"])
                    lines.extend(_format_passage(passage))
            else:
                lines.append("No evidence passage selected.")
            lines.extend(
                [
                    "",
                    "WHY THE EVIDENCE FITS MY SCORE",
                    rating["reason"] if rating else "",
                    "",
                    "NEGATIVE SEARCH / ACCESS / UNCERTAINTY NOTES",
                    *negative_notes,
                    "",
                ]
            )
        summary = _summary_record(conn, university, case_id, coder)
        protocol = (
            conn.execute(
                "SELECT * FROM protocol_versions WHERE id=?",
                (summary["protocol_version_id"],),
            ).fetchone()
            if summary
            else latest_protocol(conn)
        )
        risk = json.loads(protocol["critical_risks_json"]).get(scenario_id, "")
        if risk:
            critical_risk = (
                f"{risk}\nProtocol v1.1 was adopted after preliminary review."
            )
        else:
            critical_risk = (
                "No pre-agreed critical risk is recorded for this scenario. "
                "The supplied template states Protocol v1.1 was adopted after preliminary review; "
                "its criterion is not recorded in the local protocol database."
            )
        if summary and summary["review_route"] == "Yes":
            route_citation, route_problems = _case_reference(
                conn,
                university,
                summary["review_route_evidence_id"],
                "Supporting source ID, bundle page and lines:",
            )
            problems.extend(route_problems)
        else:
            route_citation = [
                "Supporting source ID, bundle page and lines:",
                "Not applicable.",
            ]
        lines.extend(
            [
                f"University: {university}",
                f"Case ID: {case_id}",
                f"Scenario: {scenario_label}",
                "",
                f"Mode: {_display(summary['mode'] if summary else None)}",
                f"Review route: {_display(summary['review_route'] if summary else None)}",
                *route_citation,
                "Brief explanation of which decision the route reviews:",
                _display(summary["review_route_explanation"] if summary else None),
                "",
                f"Critical gap: {_display(summary['critical_gap'] if summary else None)}",
                "Relevant pre-agreed critical risk:",
                critical_risk,
                "Specific gap and supporting evidence/search record:",
                _display(summary["specific_gap_evidence"] if summary else None),
                "",
                f"Evidence status: {_display(summary['evidence_status'] if summary else None)}",
                "Reason if insufficient:",
                _display(summary["insufficient_evidence_reason"] if summary else None),
                "",
                f"Rule direction: {_display(summary['rule_direction'] if summary else None)}",
                "",
                "Conflict basis:",
            ]
        )
        if case_scores.get("D3") == 0 and summary:
            conflict_a, conflict_a_problems = _case_reference(
                conn, university, summary["conflict_a_id"], "Conflict provision A:"
            )
            conflict_b, conflict_b_problems = _case_reference(
                conn, university, summary["conflict_b_id"], "Conflict provision B:"
            )
            problems.extend(conflict_a_problems + conflict_b_problems)
            lines.extend(conflict_a)
            lines.extend(conflict_b)
            lines.append(_display(summary["conflict_explanation"]))
        else:
            lines.append(
                _display(summary["conflict_explanation"] if summary else None)
                or "Not applicable."
            )
        lines.extend(
            [
                "",
                "Case note:",
                _display(summary["case_note"] if summary else None),
                "Calculated outcome: Leave to the workbook formula.",
                "",
            ]
        )

    if problems:
        raise ExportValidationError(problems)

    human_ratings = conn.execute(
        """SELECT r.*, p.version FROM rating_revisions r
        JOIN protocol_versions p ON p.id=r.protocol_version_id
        WHERE r.university=? AND r.coder=? ORDER BY r.case_id, r.dimension, r.revision""",
        (university, coder),
    ).fetchall()
    human_summaries = conn.execute(
        """SELECT r.*, p.version FROM case_revisions r
        JOIN protocol_versions p ON p.id=r.protocol_version_id
        WHERE r.university=? AND r.coder=? ORDER BY r.case_id, r.revision""",
        (university, coder),
    ).fetchall()
    ai_rows = conn.execute(
        """SELECT id, case_id, dimension, prompt, suggestion, created_at
        FROM ai_suggestions WHERE university=? ORDER BY case_id, dimension, id""",
        (university,),
    ).fetchall()
    if human_ratings or human_summaries or ai_rows:
        lines.extend(["PROVENANCE RECORDS — SEPARATE FROM CURRENT CODING RECORDS"])
    for row in human_ratings:
        previous = conn.execute(
            """SELECT score FROM rating_revisions
            WHERE university=? AND case_id=? AND dimension=? AND coder=? AND revision<?
            ORDER BY revision DESC LIMIT 1""",
            (university, row["case_id"], row["dimension"], row["coder"], row["revision"]),
        ).fetchone()
        lines.extend(
            [
                "Record type: Human rating",
                f"University: {university}",
                f"Case ID: {row['case_id']}",
                f"Dimension: {row['dimension']} — {DIMENSION_LABELS[row['dimension']][0]}",
                f"Coder ID: {row['coder']}",
                f"Coding date: {str(row['created_at'])[:10]}",
                f"Record status: {'Revised' if row['revision'] > 1 else 'Original'}",
                f"Previous score, if revised: {_display(previous['score'] if previous else None)}",
                f"My score: {_display(row['score'])}",
                f"Search terms used:\n{row['search_terms']}",
                f"Documents checked:\n{row['documents_checked']}",
                f"Why the evidence fits my score:\n{row['reason']}",
                f"Negative search / access / uncertainty notes:\n{row['uncertainty_note']}",
                f"Protocol version: {row['version']}",
                f"Recorded at: {row['created_at']}",
                "",
            ]
        )
    if problems:
        raise ExportValidationError(problems)
    for row in human_summaries:
        route_reference, route_errors = (
            _case_reference(
                conn,
                university,
                row["review_route_evidence_id"],
                "Supporting source ID, bundle page and lines:",
            )
            if row["review_route_evidence_id"] is not None
            else ([], [])
        )
        problems.extend(route_errors)
        conflict_references: list[str] = []
        for label, evidence_id in (
            ("Conflict provision A:", row["conflict_a_id"]),
            ("Conflict provision B:", row["conflict_b_id"]),
        ):
            if evidence_id is not None:
                reference, reference_errors = _case_reference(
                    conn, university, evidence_id, label
                )
                conflict_references.extend(reference)
                problems.extend(reference_errors)
        lines.extend(
            [
                "Record type: Human case summary",
                f"University: {university}",
                f"Case ID: {row['case_id']}",
                f"Coder ID: {row['coder']}",
                f"Record status: {'Revised' if row['revision'] > 1 else 'Original'}",
                f"Revision: {row['revision']}",
                f"Mode: {row['mode']}",
                f"Review route: {row['review_route']}",
                *route_reference,
                f"Brief explanation of which decision the route reviews: {row['review_route_explanation']}",
                f"Critical gap: {row['critical_gap']}",
                f"Specific gap and supporting evidence/search record: {row['specific_gap_evidence']}",
                f"Evidence status: {row['evidence_status']}",
                f"Reason if insufficient: {row['insufficient_evidence_reason']}",
                f"Rule direction: {row['rule_direction']}",
                f"Conflict basis: {row['conflict_explanation']}",
                *conflict_references,
                f"Case note: {row['case_note']}",
                f"Protocol version: {row['version']}",
                f"Recorded at: {row['created_at']}",
                "",
            ]
        )
    if problems:
        raise ExportValidationError(problems)
    for row in ai_rows:
        lines.extend(
            [
                "AI PROPOSAL — SEPARATE FROM HUMAN CODING",
                f"University: {university}",
                f"Case ID: {row['case_id']}",
                f"Dimension: {row['dimension']} — {DIMENSION_LABELS[row['dimension']][0]}",
                "Coder ID: AI proposal — awaiting human verification",
                f"Proposal ID: {row['id']}",
                f"Recorded at: {row['created_at']}",
                f"Proposal:\n{row['suggestion']}",
                "",
            ]
        )

    outcome_previews = []
    for case in cases:
        case_id = str(case["case_id"])
        scores = {
            dimension: (
                rating["score"] if (rating := _rating_record(
                    conn, university, case_id, dimension, coder
                )) else None
            )
            for dimension in DIMENSIONS
        }
        summary = _summary_record(conn, university, case_id, coder)
        summary_values = dict(summary) if summary else {}
        outcome_errors, outcome_warnings, conflict_valid = (
            validate_outcome_inputs(
                conn, university, case_id, coder, scores, summary_values
            )
            if summary
            else ([], [], False)
        )
        outcome_previews.append(
            {
                "case_id": case_id,
                "preview": case_outcome(scores, summary_values, conflict_valid),
                "validation_errors": outcome_errors,
                "warnings": outcome_warnings,
            }
        )

    report = {
        "university": university,
        "coding_date": coding_date.isoformat(),
        "cases": len(cases),
        "dimensions": len(evidence_rows),
        "ai_proposals": len(ai_rows),
        "incomplete_fields": incomplete,
        "validation_status": "draft_incomplete" if incomplete else "passed",
        "record_order_validation": "passed",
        "citation_validation": "passed",
        "required_fields_validation": "incomplete" if incomplete else "passed",
        "outcome_previews": outcome_previews,
    }
    return "\n".join(lines).rstrip() + "\n", report


def export_all(root: Path, db_path: Path, output_dir: Path, coder: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    reports: dict[str, Any] = {}
    try:
        initialize_db(conn)
        for university in UNIVERSITIES:
            text, report = render_university_text(conn, root, university, coder)
            target = output_dir / f"{university}_coding.txt"
            target.write_text(text, encoding="utf-8", newline="\n")
            reports[university] = {**report, "path": str(target)}
        (output_dir / "validation_report.json").write_text(
            json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        conn.close()
    return reports


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export fixed-format university coding files.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--coder", default="Coder 1")
    args = parser.parse_args()
    root = args.root.resolve()
    output_dir = root / "data" / "exports"
    result = export_all(root, root / "data" / "policy_coding.sqlite3", output_dir, args.coder)
    print(json.dumps(result, indent=2, ensure_ascii=False))
