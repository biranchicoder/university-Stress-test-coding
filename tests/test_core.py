from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pymupdf as fitz
import pytest
from docx import Document
from openpyxl import Workbook, load_workbook

from policy_coder.core import (
    DIMENSIONS,
    UNIVERSITIES,
    Citation,
    HumanRating,
    case_outcome,
    codebook_workbook_report,
    connect,
    initialize_db,
    latest_protocol,
    validate_citation,
    validate_outcome_inputs,
)
from policy_coder.indexer import SOURCE_RE, _table_flags, build_index, extract_packages, page_lines


@pytest.fixture
def db(tmp_path: Path):
    conn = connect(tmp_path / "coding.sqlite3")
    initialize_db(conn)
    yield conn
    conn.close()


def insert_source_and_page(conn, university: str, source_id: str, viewer_page: int = 1) -> None:
    conn.execute(
        "INSERT INTO documents(university, source_id, title, register_json) VALUES (?, ?, ?, ?)",
        (university, source_id, f"{university} policy", json.dumps({"retrieved": "2026-11-12"})),
    )
    conn.execute(
        "INSERT INTO source_review(university, source_id) VALUES (?, ?)",
        (university, source_id),
    )
    conn.execute(
        """INSERT INTO pages
        (university, viewer_page, bundle_page, source_id, text, lines_json, flags_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            university,
            viewer_page,
            34,
            source_id,
            "Policy provision\nSecond line",
            json.dumps({"L001": "Policy provision", "L002": "Second line"}),
            "[]",
        ),
    )


def test_extract_packages_hashes_all_files_without_changing_archives(tmp_path: Path) -> None:
    originals = {}
    for university in UNIVERSITIES:
        archive = tmp_path / f"{university}_One_Coder_Package.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr(f"{university}/source.txt", f"content-{university}")
        originals[archive.name] = hashlib.sha256(archive.read_bytes()).hexdigest()

    manifest, issues = extract_packages(tmp_path)

    assert issues == []
    assert set(manifest["packages"]) == set(UNIVERSITIES)
    for university in UNIVERSITIES:
        file_data = manifest["packages"][university]["files"]["source.txt"]
        extracted = tmp_path / file_data["path"]
        assert extracted.read_text(encoding="utf-8") == f"content-{university}"
        assert file_data["sha256"] == hashlib.sha256(extracted.read_bytes()).hexdigest()
        assert manifest["packages"][university]["archive_sha256"] == originals[
            f"{university}_One_Coder_Package.zip"
        ]
        assert hashlib.sha256(
            (tmp_path / f"{university}_One_Coder_Package.zip").read_bytes()
        ).hexdigest() == originals[f"{university}_One_Coder_Package.zip"]


def test_extract_rejects_path_traversal(tmp_path: Path) -> None:
    for university in UNIVERSITIES:
        archive = tmp_path / f"{university}_One_Coder_Package.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            entry = f"{university}/../../escape.txt" if university == "Curtin" else f"{university}/ok.txt"
            zipped.writestr(entry, "no")
    with pytest.raises(ValueError, match="Unsafe archive path"):
        extract_packages(tmp_path)
    assert not (tmp_path / "escape.txt").exists()


def test_initialize_db_migrates_existing_coding_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "legacy.sqlite3")
    conn.executescript(
        """
        CREATE TABLE evidence_passages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, university TEXT NOT NULL,
            case_id TEXT NOT NULL, dimension TEXT NOT NULL, coder TEXT NOT NULL,
            source_id TEXT NOT NULL, viewer_page INTEGER NOT NULL,
            line_start TEXT NOT NULL, line_end TEXT NOT NULL, kind TEXT NOT NULL,
            quotation_text TEXT NOT NULL DEFAULT '', paraphrase_text TEXT NOT NULL DEFAULT '',
            operative_at_cutoff INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE case_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, university TEXT NOT NULL,
            case_id TEXT NOT NULL, coder TEXT NOT NULL, revision INTEGER NOT NULL,
            protocol_version_id INTEGER NOT NULL, mode TEXT NOT NULL,
            review_route TEXT NOT NULL, critical_gap TEXT NOT NULL,
            evidence_status TEXT NOT NULL, rule_direction TEXT NOT NULL,
            conflict_a_id INTEGER, conflict_b_id INTEGER,
            conflict_confirmed INTEGER NOT NULL DEFAULT 0,
            conflict_explanation TEXT NOT NULL, case_note TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE search_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT, coder TEXT NOT NULL,
            university TEXT NOT NULL, case_id TEXT NOT NULL, dimension TEXT NOT NULL,
            action TEXT NOT NULL, query TEXT NOT NULL DEFAULT '', source_id TEXT,
            viewer_page INTEGER, bundle_page INTEGER, created_at TEXT NOT NULL
        );
        """
    )

    initialize_db(conn)

    evidence_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(evidence_passages)")
    }
    summary_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(case_revisions)")
    }
    search_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(search_activity)")
    }
    assert {"operative_version_evidence", "reading_order_verified"} <= evidence_columns
    assert {
        "review_route_evidence_id",
        "review_route_explanation",
        "specific_gap_evidence",
        "insufficient_evidence_reason",
    } <= summary_columns
    assert "result_count" in search_columns
    conn.close()


def make_test_package(root: Path, university: str) -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((8, 100), "L001", fontsize=9)
    page.insert_text((85, 96), "Policy provision for indexing", fontsize=10)
    page.insert_text((80, 780), f"{university} University | N001 | Source reading page 1", fontsize=8)
    page.insert_text((80, 810), "Bundle page 1 | N001 | source reading page 1", fontsize=8)
    pdf_bytes = pdf.tobytes()
    pdf.close()

    codebook = Document()
    codebook.add_paragraph("Human coding book")
    for _ in range(6):
        table = codebook.add_table(rows=5, cols=2)
        table.cell(0, 0).text = "Score"
        table.cell(0, 1).text = "Meaning"
        for score in range(4):
            table.cell(score + 1, 0).text = str(score)
            table.cell(score + 1, 1).text = f"Meaning {score}"
    codebook_bytes = io.BytesIO()
    codebook.save(codebook_bytes)

    scenario = Document()
    scenario.add_paragraph("S1 Test scenario")
    scenario.add_paragraph("Boundary: test")
    scenario.add_paragraph("S6 Representation test")
    scenario.add_paragraph("Boundary: no human present")
    scenario.add_paragraph("S13 Memory test")
    scenario.add_paragraph("Boundary: scoped to one course")
    scenario_bytes = io.BytesIO()
    scenario.save(scenario_bytes)

    workbook = Workbook()
    cases = workbook.active
    cases.title = "Cases"
    cases.append(["title"])
    cases.append(["instructions"])
    cases.append([])
    cases.append(["case_id", "university", "scenario_id", "family", "level",
                  "D1", "D2", "D3", "D4", "D5", "D6", "mode", "review_route",
                  "critical_gap", "evidence_status", "rule_direction", "conflict_basis",
                  "case_note", "computed_outcome"])
    for index in range(1, 16):
        cases.append([
            f"{university}-S{index}", university, f"S{index}", "Test", index,
            None, None, None, None, None, None,
            None, None, None, None, None, None, None,
            f'=IF(O{index + 4}="Insufficient","Unassessable")',
        ])
    evidence = workbook.create_sheet("Evidence")
    evidence.append(["title"])
    evidence.append(["instructions"])
    evidence.append([])
    evidence.append(["case_id", "university", "scenario_id", "dimension", "search_terms",
                    "documents_checked", "score", "your_source_ids", "your_pages_lines",
                    "quotation_or_paraphrase", "reason_for_score", "negative_search_or_access_note"])
    for index in range(1, 16):
        for dimension_index in range(1, 7):
            evidence.append([
                f"{university}-S{index}", university, f"S{index}",
                f"D{dimension_index}",
            ])
    workbook_bytes = io.BytesIO()
    workbook.save(workbook_bytes)

    register = (
        "source_id,university,title,retrieved,human_inclusion,inclusion_reason,authority,"
        "effective_date,operative_version_at_freeze,supersedes_or_duplicate_of,freeze_date,reviewer\n"
        f"N001,{university},Test policy,2026-11-12,,,,,,,,\n"
    )
    archive = root / f"{university}_One_Coder_Package.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        prefix = f"{university}/"
        zipped.writestr(prefix + "Test.pdf", pdf_bytes)
        zipped.writestr(prefix + "Human_Codebook.docx", codebook_bytes.getvalue())
        zipped.writestr(prefix + f"Human_Coder_1_{university}.xlsx", workbook_bytes.getvalue())
        zipped.writestr(prefix + "Scenarios.docx", scenario_bytes.getvalue())
        zipped.writestr(prefix + f"Source_Register_{university}.csv", register)
        zipped.writestr(prefix + "START_HERE.txt", "Start")


def test_full_index_build_creates_source_page_lines_and_search(tmp_path: Path) -> None:
    from app import page_search
    from policy_coder.text_export import export_all, render_university_text

    for university in UNIVERSITIES:
        make_test_package(tmp_path, university)
    result = build_index(tmp_path)
    assert result["university_count"] == 5

    conn = connect(tmp_path / "data" / "policy_coding.sqlite3")
    row = conn.execute(
        "SELECT source_id, bundle_page, viewer_page, lines_json FROM pages WHERE university='Curtin'"
    ).fetchone()
    assert (row["source_id"], row["bundle_page"], row["viewer_page"]) == ("N001", 1, 1)
    assert json.loads(row["lines_json"])["L001"] == "Policy provision for indexing"
    assert page_search(conn, "Curtin", "provision")[0]["source_id"] == "N001"
    report = json.loads(
        (tmp_path / "data" / "derived" / "codebook_workbook_comparison.json").read_text(encoding="utf-8")
    )
    assert report["formula_consistency"]["outcome_formulas_identical_across_universities"]
    text, validation = render_university_text(
        conn, tmp_path, "Curtin", "Coder 1", coding_date=date(2026, 11, 15)
    )
    assert validation["cases"] == 15
    assert validation["dimensions"] == 90
    assert validation["validation_status"] == "draft_incomplete"
    assert text.count("Calculated outcome: Leave to the workbook formula.") == 15
    assert text.index("Dimension: D1 — Scope") < text.index("Dimension: D2 — Authority")
    assert text.index("Dimension: D3 — Coherence") < text.index("Dimension: D4 — Procedure")
    assert text.index("Dimension: D6 — Adaptation") < text.index("Mode: ")
    assert "My score: \n" in text
    assert text.index("Mode: ") < text.index("Review route: ") < text.index("Critical gap: ")
    assert text.index("Critical gap: ") < text.index("Evidence status: ") < text.index("Rule direction: ")
    assert text.index("Rule direction: ") < text.index("Conflict basis:") < text.index("Case note:")
    assert "case_id:" not in text and "reason_for_score:" not in text
    assert len(validation["outcome_previews"]) == 15
    assert "Pending" not in text
    exports = export_all(
        tmp_path, tmp_path / "data" / "policy_coding.sqlite3",
        tmp_path / "data" / "exports", "Coder 1",
    )
    assert len(exports) == 5
    assert all((tmp_path / "data" / "exports" / f"{university}_coding.txt").exists()
               for university in UNIVERSITIES)
    conn.close()


def test_text_export_uses_logged_activity_exact_quotation_and_separate_provenance(tmp_path) -> None:
    from policy_coder.core import indexed_quote
    from policy_coder.text_export import render_university_text

    for university in UNIVERSITIES:
        make_test_package(tmp_path, university)
    from policy_coder.indexer import build_index
    build_index(tmp_path)
    conn = connect(tmp_path / "data" / "policy_coding.sqlite3")
    protocol = latest_protocol(conn)
    page = conn.execute(
        "SELECT lines_json FROM pages WHERE university='Curtin' AND viewer_page=1"
    ).fetchone()
    quotation = indexed_quote(json.loads(page["lines_json"]), "L001", "L001")
    conn.execute(
        """INSERT INTO evidence_passages
        (university, case_id, dimension, coder, source_id, viewer_page, line_start,
         line_end, kind, quotation_text, operative_at_cutoff,
         operative_version_evidence, created_at)
        VALUES ('Curtin', 'Curtin-S1', 'D1', 'Coder 1', 'N001', 1, 'L001', 'L001',
                'quotation', ?, 1, 'Version 1.0, dated 2026-11-01, shown in footer', '2026-11-15')""",
        (quotation,),
    )
    conn.executemany(
        """INSERT INTO search_activity
        (coder, university, case_id, dimension, action, query, source_id,
         viewer_page, bundle_page, result_count, created_at)
        VALUES ('Coder 1', 'Curtin', 'Curtin-S1', 'D1', ?, ?, ?, ?, ?, ?, '2026-11-15')""",
        [
            ("search", "assessment AI", None, None, None, 1),
            ("document_checked", "assessment AI", "N001", 1, 1, None),
            ("search", "missing-control", None, None, None, 0),
        ],
    )
    for revision, score in ((1, 1), (2, 3)):
        conn.execute(
            """INSERT INTO rating_revisions
            (university, case_id, dimension, coder, revision, protocol_version_id,
             score, search_terms, documents_checked, reason, uncertainty_note, created_at)
            VALUES ('Curtin', 'Curtin-S1', 'D1', 'Coder 1', ?, ?, ?, ?, ?, ?, '', ?)""",
            (revision, protocol["id"], score, "assessment AI", "N001 p1", f"reason {score}", f"2026-11-15T00:00:0{revision}"),
        )
    conn.execute(
        """INSERT INTO ai_suggestions
        (university, case_id, dimension, coder, prompt, suggestion, created_at)
        VALUES ('Curtin', 'Curtin-S1', 'D1', 'Coder 1', 'prompt', 'AI-only suggestion', '2026-11-15')"""
    )
    conn.commit()
    text, _ = render_university_text(
        conn, tmp_path, "Curtin", "Coder 1", coding_date=date(2026, 11, 15)
    )
    d1 = text.split("Dimension: D1 — Scope\n", 1)[1].split("\n\nUniversity: Curtin\nCase ID: Curtin-S1\nScenario:", 1)[0]
    assert "Search terms used:\nassessment AI\nmissing-control" in d1
    assert "No indexed pages matched these searches:\nmissing-control" in d1
    assert "Documents checked:\nN001 — Test policy, printed bundle pages checked: 1" in d1
    assert "My score: 3" in d1
    assert "Source ID: N001\nDocument title: Test policy\nPrinted bundle page: 1\nLine numbers: L001-L001" in d1
    assert f"Evidence type: Exact quotation\nExact quotation:\n{quotation}" in d1
    assert "Record status: Revised\nPrevious score, if revised: 1" in d1
    assert "Record type: Human rating" in text
    assert "My score: 1" in text and "My score: 3" in text
    assert "AI PROPOSAL — SEPARATE FROM HUMAN CODING" in text
    assert "Coder ID: AI proposal — awaiting human verification" in text
    assert "Coding date: 2026-11-15" in text
    conn.close()


def test_case_summary_export_uses_exact_fields_and_printed_citation(tmp_path: Path) -> None:
    from policy_coder.indexer import build_index
    from policy_coder.text_export import render_university_text

    for university in UNIVERSITIES:
        make_test_package(tmp_path, university)
    build_index(tmp_path)
    conn = connect(tmp_path / "data" / "policy_coding.sqlite3")
    protocol = latest_protocol(conn)
    conn.execute(
        """INSERT INTO evidence_passages
        (university, case_id, dimension, coder, source_id, viewer_page, line_start,
         line_end, kind, paraphrase_text, created_at)
        VALUES ('Curtin', 'Curtin-S1', 'D4', 'Coder 1', 'N001', 1, 'L001', 'L001',
                'paraphrase', 'The route reviews the original decision.', '2026-11-15')"""
    )
    evidence_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO case_revisions
        (university, case_id, coder, revision, protocol_version_id, mode,
         review_route, review_route_evidence_id, review_route_explanation,
         critical_gap, specific_gap_evidence, evidence_status,
         insufficient_evidence_reason, rule_direction, conflict_explanation,
         case_note, created_at)
        VALUES ('Curtin', 'Curtin-S1', 'Coder 1', 1, ?, 'Delegating',
                'Yes', ?, 'It reviews the initial decision.', 'No', '',
                'Sufficient', '', 'Conditional', '', 'A concise case note.',
                '2026-11-15')""",
        (protocol["id"], evidence_id),
    )
    conn.commit()
    text, report = render_university_text(
        conn, tmp_path, "Curtin", "Coder 1", coding_date=date(2026, 11, 15)
    )
    assert "University: Curtin\nCase ID: Curtin-S1\nScenario: S1 — Test scenario" in text
    assert "Dimension: D4 — Procedure" in text
    assert "Brief explanation of which decision the route reviews:\nIt reviews the initial decision." in text
    assert (
        "Supporting source ID, bundle page and lines:\n"
        "Source ID: N001\nDocument title: Test policy\nPrinted bundle page: 1\n"
        "Line numbers: L001-L001"
    ) in text
    assert "Calculated outcome: Leave to the workbook formula." in text
    assert len(report["outcome_previews"]) == 15
    assert report["outcome_previews"][0]["preview"] == "Pending"
    assert "Current computed outcome" not in text
    conn.close()


def test_text_export_rejects_scored_rating_without_actual_search_logs(tmp_path) -> None:
    from datetime import date
    from policy_coder.indexer import build_index
    from policy_coder.text_export import ExportValidationError, render_university_text

    for university in UNIVERSITIES:
        make_test_package(tmp_path, university)
    build_index(tmp_path)
    conn = connect(tmp_path / "data" / "policy_coding.sqlite3")
    protocol = latest_protocol(conn)
    conn.execute(
        """INSERT INTO rating_revisions
        (university, case_id, dimension, coder, revision, protocol_version_id,
         score, search_terms, documents_checked, reason, uncertainty_note, created_at)
        VALUES ('Curtin', 'Curtin-S1', 'D1', 'Coder 1', 1, ?, 0, 'not a log',
                'not a log', 'reason', '', '2026-11-15')""",
        (protocol["id"],),
    )
    conn.commit()
    with pytest.raises(ExportValidationError, match="no search activity"):
        render_university_text(conn, tmp_path, "Curtin", "Coder 1", date(2026, 11, 15))
    conn.close()


def test_pdf_visible_labels_are_used_without_synthesizing_numbers() -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((8, 100), "L001", fontsize=9)
    page.insert_text((85, 96), "Actual policy text", fontsize=10)
    page.insert_text((8, 120), "L002", fontsize=9)
    page.insert_text((85, 116), "Second visual row", fontsize=10)

    lines = page_lines(page)

    assert set(lines) == {"L001", "L002"}
    assert "Actual policy text" in lines["L001"]
    assert "Second visual row" in lines["L002"]


def test_scrambled_table_reading_order_is_flagged() -> None:
    document = fitz.open()
    page = document.new_page()
    page.draw_rect(fitz.Rect(80, 100, 550, 240))
    page.draw_line((300, 100), (300, 240))
    page.draw_line((80, 170), (550, 170))
    page.insert_text((8, 130), "L001", fontsize=9)
    page.insert_text((90, 130), "Left cell text", fontsize=10)
    page.insert_text((350, 130), "Right cell text", fontsize=10)

    assert _table_flags(page) == ["ambiguous_table", "scrambled_reading_order"]
    document.close()


def test_source_footer_parser_preserves_reading_edition_id() -> None:
    match = SOURCE_RE.search("Curtin University | N209 | Source reading page 1")
    assert match is not None
    assert match.group(1) == "N209"


def test_dimension_score_descriptions_come_from_extracted_codebook() -> None:
    from app import dimension_score_criteria

    text = (
        "Shared directions\n"
        "Score | Meaning\n0 | D1 zero\n1 | D1 one\n2 | D1 two\n3 | D1 three\n"
        "Score | Meaning\n0 | D2 zero\n1 | D2 one\n2 | D2 two\n3 | D2 three\n"
    )
    assert dimension_score_criteria(text, "D1") == [
        "0 — D1 zero", "1 — D1 one", "2 — D1 two", "3 — D1 three"
    ]
    assert dimension_score_criteria(text, "D2")[0] == "0 — D2 zero"


def test_citation_validation_requires_existing_source_page_lines_and_exact_quote(db) -> None:
    insert_source_and_page(db, "Curtin", "N001")
    valid = Citation(
        university="Curtin",
        source_id="N001",
        viewer_page=1,
        line_start="L001",
        line_end="L002",
        kind="quotation",
        quotation_text="Policy provision\nSecond line",
    )
    assert validate_citation(db, valid) == []

    forged_quote = valid.model_copy(update={"quotation_text": "Edited quotation"})
    assert any("differs from the text copied" in item for item in validate_citation(db, forged_quote))
    missing_line = valid.model_copy(update={"line_end": "L003"})
    assert any("existing visible L-number" in item for item in validate_citation(db, missing_line))
    other_university = valid.model_copy(update={"university": "ECU"})
    assert any("does not belong" in item for item in validate_citation(db, other_university))


def test_scrambled_table_quote_requires_visual_review_and_version_evidence(db) -> None:
    insert_source_and_page(db, "Curtin", "N001")
    db.execute(
        "UPDATE pages SET flags_json=? WHERE university='Curtin' AND viewer_page=1",
        (json.dumps(["ambiguous_table", "scrambled_reading_order"]),),
    )
    candidate = Citation(
        university="Curtin",
        source_id="N001",
        viewer_page=1,
        line_start="L001",
        line_end="L002",
        kind="quotation",
        quotation_text="Second line",
    )
    assert any("visual reading-order verification" in item for item in validate_citation(db, candidate))
    verified = candidate.model_copy(update={"reading_order_verified": True})
    assert validate_citation(db, verified) == []

    operative = verified.model_copy(update={"operative_at_cutoff": True})
    assert any("recorded version evidence" in item for item in validate_citation(db, operative))
    with_version = operative.model_copy(
        update={"operative_version_evidence": "Version 2.1, dated 2026-10-01, shown in footer"}
    )
    assert validate_citation(db, with_version) == []


def test_university_records_are_separated_even_for_same_source_id(db) -> None:
    insert_source_and_page(db, "Curtin", "N001")
    insert_source_and_page(db, "ECU", "N001")

    cur = db.execute(
        "SELECT title FROM documents WHERE university=? AND source_id=?",
        ("Curtin", "N001"),
    ).fetchone()
    ecu = db.execute(
        "SELECT title FROM documents WHERE university=? AND source_id=?",
        ("ECU", "N001"),
    ).fetchone()
    assert cur["title"] == "Curtin policy"
    assert ecu["title"] == "ECU policy"
    assert db.execute("SELECT COUNT(*) FROM pages WHERE university='Curtin'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM pages WHERE university='ECU'").fetchone()[0] == 1


def score_set(**updates):
    values = {dimension: 2 for dimension in DIMENSIONS}
    values.update(updates)
    return values


def summary_set(**updates):
    values = {
        "mode": "Prescribing",
        "review_route": "Yes",
        "critical_gap": "No",
        "evidence_status": "Sufficient",
        "rule_direction": "Conditional",
        "conflict_explanation": "",
    }
    values.update(updates)
    return values


def test_outcome_evidence_and_unfinished_branches() -> None:
    assert case_outcome(score_set(), summary_set(evidence_status="Insufficient")) == "Unassessable"
    assert case_outcome(score_set(), summary_set(critical_gap="Uncertain")) == "Unassessable"
    assert case_outcome(score_set(D6=None), summary_set(critical_gap="Uncertain")) == "Unassessable"
    assert case_outcome(score_set(), summary_set(evidence_status="")) == "Pending"
    assert case_outcome(score_set(D6=None), summary_set()) == "Pending"
    assert case_outcome(score_set(), summary_set(mode="")) == "Pending"


def test_blank_rating_is_not_zero_and_scored_ratings_require_audit_fields() -> None:
    blank = HumanRating(
        university="Curtin", case_id="Curtin-S1", dimension="D1", coder="Coder 1"
    )
    assert blank.score is None
    with pytest.raises(ValueError, match="requires search terms"):
        HumanRating(
            university="Curtin", case_id="Curtin-S1", dimension="D1",
            coder="Coder 1", score=0,
        )


@pytest.mark.parametrize(
    "scores,summary",
    [
        ({"D1": 0}, {}),
        ({"D2": 1}, {}),
        ({"D4": 0}, {}),
        ({"D5": 0}, {}),
        ({}, {"critical_gap": "Yes"}),
    ],
)
def test_outcome_strict_indeterminate_triggers(scores, summary) -> None:
    assert case_outcome(score_set(**scores), summary_set(**summary)) == "Indeterminate"


def test_outcome_documentary_conflict_requires_validation() -> None:
    values = summary_set(conflict_explanation="The provisions directly conflict.")
    assert case_outcome(score_set(D3=0), values, conflict_valid=False) == "Pending"
    assert case_outcome(score_set(D3=0), values, conflict_valid=True) == "Documentary conflict"


def test_outcome_resolved_structured_discretion_and_fallback() -> None:
    resolved = score_set(D1=3, D2=3, D3=2, D4=3, D5=3, D6=0)
    assert case_outcome(resolved, summary_set()) == "Resolved"
    structured = score_set(D1=1, D2=2, D3=1, D4=2, D5=2, D6=3)
    assert case_outcome(structured, summary_set(mode="Delegating")) == "Structured discretion"
    assert case_outcome(score_set(), summary_set(mode="None")) == "Indeterminate"


def test_d3_zero_requires_two_distinct_operative_citations_and_explanation(db) -> None:
    insert_source_and_page(db, "Curtin", "N001", 1)
    db.execute(
        """INSERT INTO pages
        (university, viewer_page, bundle_page, source_id, text, lines_json, flags_json)
        VALUES ('Curtin', 2, 35, 'N001', 'Policy provision', ?, '[]')""",
        (json.dumps({"L001": "Policy provision"}),),
    )
    latest_protocol(db)
    created = "2026-11-15T00:00:00+00:00"
    for page in (1, 2):
        db.execute(
            """INSERT INTO evidence_passages
            (university, case_id, dimension, coder, source_id, viewer_page,
             line_start, line_end, kind, quotation_text, operative_at_cutoff,
             operative_version_evidence, created_at)
            VALUES ('Curtin', 'Curtin-S1', 'D3', 'Coder 1', 'N001', ?, 'L001', 'L001',
                    'quotation', 'Policy provision', 1, 'Version 1.0, dated 2026-11-01', ?)""",
            (page, created),
        )
    ids = [
        row[0] for row in db.execute(
            "SELECT id FROM evidence_passages WHERE case_id='Curtin-S1' ORDER BY id"
        )
    ]
    base = summary_set(
        review_route_evidence_id=ids[0],
        conflict_a_id=ids[0],
        conflict_b_id=ids[1],
        conflict_confirmed=True,
        conflict_explanation="The provisions impose incompatible requirements and neither controls.",
    )
    errors, warnings, valid = validate_outcome_inputs(
        db, "Curtin", "Curtin-S1", "Coder 1", score_set(D3=0), base
    )
    assert errors == []
    assert warnings == []
    assert valid is True
    assert case_outcome(score_set(D3=0), base, valid) == "Documentary conflict"

    base["conflict_confirmed"] = False
    errors, _, valid = validate_outcome_inputs(
        db, "Curtin", "Curtin-S1", "Coder 1", score_set(D3=0), base
    )
    assert any("Confirm that the two cited" in item for item in errors)
    assert valid is False
    base["conflict_confirmed"] = True
    db.execute(
        "UPDATE evidence_passages SET viewer_page=1 WHERE id=?",
        (ids[1],),
    )
    errors, _, valid = validate_outcome_inputs(
        db, "Curtin", "Curtin-S1", "Coder 1", score_set(D3=0), base
    )
    assert any("two distinct cited provisions" in item for item in errors)
    assert valid is False


def test_rating_revisions_remain_separate_and_append_only(db) -> None:
    protocol = latest_protocol(db)
    for revision, score in ((1, 1), (2, 3)):
        db.execute(
            """INSERT INTO rating_revisions
            (university, case_id, dimension, coder, revision, protocol_version_id,
             score, search_terms, documents_checked, reason, uncertainty_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', '', ?)""",
            ("Curtin", "Curtin-S1", "D1", "Coder 1", revision, protocol["id"], score, f"t{revision}"),
        )
    rows = db.execute(
        """SELECT revision, score FROM rating_revisions
        WHERE university='Curtin' AND case_id='Curtin-S1' AND dimension='D1' AND coder='Coder 1'
        ORDER BY revision"""
    ).fetchall()
    assert [(row["revision"], row["score"]) for row in rows] == [(1, 1), (2, 3)]


def test_comparison_report_records_formula_codebook_differences(db) -> None:
    report = codebook_workbook_report(db)
    rules = {item["rule"] for item in report["differences"]}
    assert "D3 = 0 documentary conflict" in rules
    assert "Critical gap uncertainty precedes incomplete score fields" in rules
    assert "Score values must be 0 through 3" in rules


def test_export_workbook_preserves_formulas_and_maps_human_inputs(tmp_path, monkeypatch) -> None:
    import app as application

    raw = tmp_path / "raw"
    university_dir = raw / "Curtin"
    university_dir.mkdir(parents=True)
    template = Workbook()
    cases = template.active
    cases.title = "Cases"
    case_headers = [
        "case_id", "university", "scenario_id", "family", "level", "D1", "D2", "D3",
        "D4", "D5", "D6", "mode", "review_route", "critical_gap", "evidence_status",
        "rule_direction", "conflict_basis", "case_note", "computed_outcome",
    ]
    cases.append(["title"])
    cases.append(["instructions"])
    cases.append([])
    cases.append(case_headers)
    cases.append([
        "Curtin-S1", "Curtin", "S1", "Assessment", 1,
        "=IF(ISNUMBER(Evidence!G5),Evidence!G5,\"\")",
        None, None, None, None, None, None, None, None, None, None, None, None,
        '=IF(O5="Insufficient","Unassessable","Pending")',
    ])
    evidence = template.create_sheet("Evidence")
    evidence.append(["title"])
    evidence.append(["instructions"])
    evidence.append([])
    evidence.append([
        "case_id", "university", "scenario_id", "dimension", "search_terms",
        "documents_checked", "score", "your_source_ids", "your_pages_lines",
        "quotation_or_paraphrase", "reason_for_score", "negative_search_or_access_note",
    ])
    evidence.append(["Curtin-S1", "Curtin", "S1", "D1", None, None, None, None, None, None, None, None])
    template_path = university_dir / "Human_Coder_1_Curtin.xlsx"
    template.save(template_path)
    monkeypatch.setattr(application, "RAW_PATH", raw)

    conn = connect(tmp_path / "export.sqlite3")
    initialize_db(conn)
    protocol = latest_protocol(conn)
    conn.execute(
        """INSERT INTO rating_revisions
        (university, case_id, dimension, coder, revision, protocol_version_id,
         score, search_terms, documents_checked, reason, uncertainty_note, created_at)
        VALUES ('Curtin', 'Curtin-S1', 'D1', 'Coder 1', 1, ?, 3, 'appeal', 'Policy', 'reason', '', 'now')""",
        (protocol["id"],),
    )
    conn.commit()
    content = application.export_workbook(conn, "Curtin", "Coder 1")
    exported = load_workbook(io.BytesIO(content), data_only=False)
    assert exported["Cases"]["F5"].value == '=IF(ISNUMBER(Evidence!G5),Evidence!G5,"")'
    assert exported["Cases"]["S5"].value == '=IF(O5="Insufficient","Unassessable","Pending")'
    assert exported["Evidence"]["G5"].value == 3
    conn.close()
