from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pymupdf as fitz
import streamlit as st
from openpyxl import load_workbook

from policy_coder.core import (
    DIMENSIONS,
    DIMENSION_LABELS,
    POLICY_CUTOFF,
    SCENARIO_IDS,
    UNIVERSITIES,
    CaseSummary,
    Citation,
    HumanRating,
    case_outcome,
    connect,
    indexed_quote,
    initialize_db,
    latest_protocol,
    latest_rating,
    latest_summary,
    safe_json,
    utc_now,
    validate_citation,
    validate_outcome_inputs,
)
from policy_coder.text_export import (
    ExportValidationError,
    export_all,
    render_university_text,
    validate_export,
)


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "policy_coding.sqlite3"
RAW_PATH = ROOT / "data" / "raw"


def database() -> sqlite3.Connection:
    conn = connect(DB_PATH)
    initialize_db(conn)
    return conn


def available_universities(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0] for row in conn.execute(
            "SELECT DISTINCT university FROM documents ORDER BY university"
        )
    ]


def scenarios_for(conn: sqlite3.Connection, university: str) -> dict[str, dict[str, str]]:
    row = conn.execute(
        "SELECT scenarios_json FROM resources WHERE university=?", (university,)
    ).fetchone()
    return json.loads(row[0]) if row else {}


def render_page_image(university: str, viewer_page: int) -> None:
    pdfs = list((RAW_PATH / university).glob("*.pdf"))
    if not pdfs:
        st.warning("The indexed PDF is missing from data/raw.")
        return
    with fitz.open(pdfs[0]) as pdf:
        if viewer_page < 1 or viewer_page > len(pdf):
            st.error("Indexed viewer-page number is outside the original PDF.")
            return
        image = pdf[viewer_page - 1].get_pixmap(matrix=fitz.Matrix(1.45, 1.45), alpha=False)
        st.image(image.tobytes("png"), caption=f"Original PDF viewer page {viewer_page}")


def page_details(conn: sqlite3.Connection, university: str, viewer_page: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pages WHERE university=? AND viewer_page=?",
        (university, viewer_page),
    ).fetchone()


def citation_display(row: sqlite3.Row) -> str:
    operative = (
        "operative at cut-off (version evidence recorded)"
        if row["operative_at_cutoff"] and row["operative_version_evidence"].strip()
        else "operative status unverified"
    )
    return (
        f"#{row['id']} | {row['source_id']} | PDF viewer p. {row['viewer_page']} | "
        f"{row['line_start']}-{row['line_end']} | {row['kind']} | "
        f"{operative}"
    )


def case_search(conn: sqlite3.Connection, university: str, case_id: str, coder: str) -> dict[str, Any]:
    return {
        row["dimension"]: row
        for row in conn.execute(
            """SELECT * FROM rating_revisions
            WHERE university=? AND case_id=? AND coder=?
            AND revision=(SELECT MAX(r2.revision) FROM rating_revisions r2
                WHERE r2.university=rating_revisions.university
                AND r2.case_id=rating_revisions.case_id
                AND r2.dimension=rating_revisions.dimension
                AND r2.coder=rating_revisions.coder)""",
            (university, case_id, coder),
        )
    }


def current_evidence(
    conn: sqlite3.Connection, university: str, case_id: str, coder: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM evidence_passages
        WHERE university=? AND case_id=? AND coder=? ORDER BY id""",
        (university, case_id, coder),
    ).fetchall()


def current_scores(conn: sqlite3.Connection, university: str, case_id: str, coder: str) -> dict[str, int | None]:
    latest = case_search(conn, university, case_id, coder)
    return {dimension: latest[dimension]["score"] if dimension in latest else None for dimension in DIMENSIONS}


def get_case_id(university: str, scenario_id: str) -> str:
    return f"{university}-{scenario_id}"


def dimension_score_criteria(codebook_text: str, dimension: str) -> list[str]:
    tables = codebook_text.split("Score | Meaning")[1:]
    table_index = int(dimension[1:]) - 1
    if table_index >= len(tables):
        return []
    criteria = []
    for line in tables[table_index].splitlines():
        match = re.match(r"^\s*([0-3])\s*\|\s*(.+?)\s*$", line)
        if match:
            criteria.append(f"{match.group(1)} — {match.group(2)}")
        elif criteria:
            break
    return criteria


def required_field_issues(conn: sqlite3.Connection, university: str, coder: str) -> list[str]:
    problems: list[str] = []
    for scenario_id in SCENARIO_IDS:
        case_id = get_case_id(university, scenario_id)
        for dimension in DIMENSIONS:
            rating = latest_rating(conn, university, case_id, dimension, coder)
            if rating is None:
                problems.append(f"{case_id} {dimension}: no rating record yet.")
            else:
                if rating["score"] is None:
                    problems.append(f"{case_id} {dimension}: score is blank (unfinished, not zero).")
                for field, label in (
                    ("search_terms", "search terms"),
                    ("documents_checked", "documents checked"),
                    ("reason", "reason"),
                ):
                    if not rating[field].strip():
                        problems.append(f"{case_id} {dimension}: {label} is blank.")
        summary = latest_summary(conn, university, case_id, coder)
        if summary is None:
            problems.append(f"{case_id}: case summary is not recorded.")
        else:
            for field, label in (
                ("mode", "mode"),
                ("review_route", "review route"),
                ("critical_gap", "critical gap"),
                ("evidence_status", "evidence status"),
                ("rule_direction", "rule direction"),
            ):
                if not summary[field]:
                    problems.append(f"{case_id}: {label} is blank.")
    return problems


def page_search(conn: sqlite3.Connection, university_filter: str, query: str, limit: int = 100) -> list[sqlite3.Row]:
    tokens = re.findall(r"[\w'-]+", query, flags=re.UNICODE)
    if not tokens:
        return []
    match_query = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
    if university_filter == "All universities":
        return conn.execute(
            """SELECT page_fts.university, CAST(page_fts.viewer_page AS INTEGER) AS viewer_page,
            page_fts.source_id, snippet(page_fts, 3, '[', ']', ' … ', 18) AS snippet,
            p.bundle_page, p.flags_json, d.title
            FROM page_fts JOIN pages p
              ON p.university=page_fts.university
              AND p.viewer_page=CAST(page_fts.viewer_page AS INTEGER)
            LEFT JOIN documents d
              ON d.university=page_fts.university AND d.source_id=page_fts.source_id
            WHERE page_fts MATCH ?
            ORDER BY page_fts.university, CAST(page_fts.viewer_page AS INTEGER) LIMIT ?""",
            (match_query, limit),
        ).fetchall()
    return conn.execute(
        """SELECT page_fts.university, CAST(page_fts.viewer_page AS INTEGER) AS viewer_page,
        page_fts.source_id, snippet(page_fts, 3, '[', ']', ' … ', 18) AS snippet,
        p.bundle_page, p.flags_json, d.title
        FROM page_fts JOIN pages p
          ON p.university=page_fts.university
          AND p.viewer_page=CAST(page_fts.viewer_page AS INTEGER)
        LEFT JOIN documents d
          ON d.university=page_fts.university AND d.source_id=page_fts.source_id
        WHERE page_fts MATCH ? AND page_fts.university=?
        ORDER BY CAST(page_fts.viewer_page AS INTEGER) LIMIT ?""",
        (match_query, university_filter, limit),
    ).fetchall()


def search_activity_values(
    conn: sqlite3.Connection,
    university: str,
    case_id: str,
    dimension: str,
    coder: str,
) -> tuple[str, str]:
    rows = conn.execute(
        """SELECT a.*, d.title FROM search_activity a
        LEFT JOIN documents d ON d.university=a.university AND d.source_id=a.source_id
        WHERE a.university=? AND a.case_id=? AND a.dimension=? AND a.coder=?
        ORDER BY a.id""",
        (university, case_id, dimension, coder),
    ).fetchall()
    queries = list(dict.fromkeys(row["query"] for row in rows if row["action"] == "search" and row["query"]))
    checked = []
    seen = set()
    for row in rows:
        if row["action"] != "document_checked":
            continue
        key = (row["source_id"], row["viewer_page"])
        if key in seen:
            continue
        seen.add(key)
        checked.append(
            f"{row['source_id'] or 'Unlabelled'} — {row['title'] or 'Title unavailable'}; "
            f"printed Bundle page {row['bundle_page'] or ''}; PDF viewer page {row['viewer_page'] or ''}"
        )
    return "; ".join(queries), "\n".join(checked)


def render_search(
    conn: sqlite3.Connection,
    context_university: str,
    scenario_id: str,
    dimension: str,
    coder: str,
) -> None:
    st.header("Full-text policy search")
    choices = ["All universities"] + available_universities(conn)
    selected_university = st.selectbox(
        "University collection", choices, key="search_university"
    )
    st.caption(
        f"Search activity will be recorded for {context_university} "
        f"{get_case_id(context_university, scenario_id)} {dimension} ({coder})."
    )
    saved_context = (
        context_university,
        get_case_id(context_university, scenario_id),
        dimension,
        coder,
    )
    active_context = st.session_state.get("policy_search_context")
    if active_context and active_context != saved_context:
        st.info("The coding context changed. Submit the search again before recording additional search activity.")
    with st.form("policy_full_text_search"):
        query = st.text_input(
            "Search terms",
            value=st.session_state.get("policy_search_query", ""),
            placeholder="For example: appeal review generative AI",
        )
        submitted = st.form_submit_button("Search policy collection")
    if submitted and query.strip():
        cursor = conn.execute(
            """INSERT INTO search_activity
            (coder, university, case_id, dimension, action, query, created_at)
            VALUES (?, ?, ?, ?, 'search', ?, ?)""",
            (
                coder,
                context_university,
                get_case_id(context_university, scenario_id),
                dimension,
                query.strip(),
                utc_now(),
            ),
        )
        conn.commit()
        st.session_state["policy_search_query"] = query.strip()
        st.session_state["policy_search_collection"] = selected_university
        st.session_state["policy_search_context"] = saved_context
        st.session_state["policy_search_activity_id"] = cursor.lastrowid

    active_context = st.session_state.get("policy_search_context")
    if active_context and active_context != saved_context:
        return
    active_query = st.session_state.get("policy_search_query", "")
    active_collection = st.session_state.get("policy_search_collection", selected_university)
    if not active_query:
        st.info("Search covers all indexed PDF text, including source pages and surrounding provisions.")
        return
    try:
        results = page_search(conn, active_collection, active_query)
    except sqlite3.OperationalError as exc:
        st.error(f"Search could not be completed: {exc}")
        return
    activity_id = st.session_state.get("policy_search_activity_id")
    if activity_id and re.findall(r"[\w'-]+", active_query, flags=re.UNICODE):
        conn.execute(
            "UPDATE search_activity SET result_count=? WHERE id=? AND coder=?",
            (len(results), activity_id, coder),
        )
        conn.commit()
        st.session_state["policy_search_activity_id"] = None
    if not results:
        st.info("No pages matched all entered terms.")
        return
    keys = []
    for row in results:
        location = f"{row['university']} | {row['source_id'] or 'unlabelled page'} | "
        location += f"PDF viewer p. {row['viewer_page']}"
        if row["bundle_page"] is not None:
            location += f" | printed Bundle page {row['bundle_page']}"
        if row["title"]:
            location += f" | {row['title']}"
        st.markdown(f"**{location}**")
        st.write(row["snippet"])
        keys.append(f"{row['university']}|{row['viewer_page']}")
    selected = st.selectbox("Open a result page", keys, key="search_open_page")
    university, viewer_page_text = selected.split("|", 1)
    viewer_page = int(viewer_page_text)
    selected_row = page_details(conn, university, viewer_page)
    if selected_row:
        if selected_row["bundle_page"] is None:
            st.caption("No printed Bundle page label was extracted; viewer page is shown separately.")
        st.markdown("**Indexed page text**")
        st.text(selected_row["text"])
        prev_page = conn.execute(
            """SELECT viewer_page, bundle_page, text FROM pages
            WHERE university=? AND source_id IS ? AND viewer_page<?
            ORDER BY viewer_page DESC LIMIT 1""",
            (university, selected_row["source_id"], viewer_page),
        ).fetchone()
        next_page = conn.execute(
            """SELECT viewer_page, bundle_page, text FROM pages
            WHERE university=? AND source_id IS ? AND viewer_page>?
            ORDER BY viewer_page LIMIT 1""",
            (university, selected_row["source_id"], viewer_page),
        ).fetchone()
        with st.expander("Surrounding provisions from this source"):
            for label, page in (("Previous source page", prev_page), ("Next source page", next_page)):
                if page:
                    st.markdown(f"**{label}: viewer p. {page['viewer_page']} | Bundle p. {page['bundle_page'] or 'not extracted'}**")
                    st.text(page["text"])
        flags = json.loads(selected_row["flags_json"])
        if flags:
            st.warning("Index review flags: " + "; ".join(flags))
        render_page_image(university, viewer_page)
        if selected_row["source_id"] and university == context_university:
            if st.button(
                "Record this source page as checked for the selected case and dimension",
                key=f"mark_checked_{context_university}_{scenario_id}_{dimension}_{university}_{viewer_page}",
            ):
                conn.execute(
                    """INSERT INTO search_activity
                    (coder, university, case_id, dimension, action, query,
                     source_id, viewer_page, bundle_page, created_at)
                    VALUES (?, ?, ?, ?, 'document_checked', ?, ?, ?, ?, ?)""",
                    (
                        coder,
                        context_university,
                        get_case_id(context_university, scenario_id),
                        dimension,
                        active_query,
                        selected_row["source_id"],
                        viewer_page,
                        selected_row["bundle_page"],
                        utc_now(),
                    ),
                )
                conn.commit()
                st.success("Recorded in the search activity log.")
        elif not selected_row["source_id"]:
            st.info("This page has no source ID footer and cannot be recorded as a checked source.")
        else:
            st.warning("Only source pages from the selected coding university can be logged as checked.")


def render_dimension(
    conn: sqlite3.Connection,
    university: str,
    scenario_id: str,
    dimension: str,
    coder: str,
    scenarios: dict[str, dict[str, str]],
    codebook_text: str,
) -> None:
    case_id = get_case_id(university, scenario_id)
    scenario = scenarios.get(scenario_id, {})
    st.subheader(f"{scenario_id}: {scenario.get('title', '')} | {dimension} {DIMENSION_LABELS[dimension][0]}")
    left, right = st.columns(2)
    with left:
        st.markdown("**Scenario**")
        st.text(scenario.get("text", "Scenario text was not found in the extracted package."))
        st.markdown("**Codebook criterion**")
        st.write(DIMENSION_LABELS[dimension][1])
        for score_criterion in dimension_score_criteria(codebook_text, dimension):
            st.write(score_criterion)
        st.caption("Full package codebook is available below.")
        with st.expander("Complete extracted codebook"):
            st.text(codebook_text)
    latest = latest_rating(conn, university, case_id, dimension, coder)
    logged_search_terms, logged_documents_checked = search_activity_values(
        conn, university, case_id, dimension, coder
    )
    with right:
        st.markdown("**Human rating (separate from AI suggestions)**")
        with st.form(f"rating_{university}_{scenario_id}_{dimension}"):
            current_score = "" if latest is None or latest["score"] is None else str(latest["score"])
            score = st.selectbox(
                "Score (blank means unfinished; it is not zero)",
                ["", "0", "1", "2", "3"],
                index=["", "0", "1", "2", "3"].index(current_score),
            )
            st.text_area("Search terms from activity log", value=logged_search_terms, disabled=True)
            st.text_area("Documents checked from activity log", value=logged_documents_checked, disabled=True)
            reason = st.text_area("Reason for score", value=latest["reason"] if latest else "")
            uncertainty = st.text_area("Uncertainty / access / version notes", value=latest["uncertainty_note"] if latest else "")
            if st.form_submit_button("Save human rating as a new revision"):
                try:
                    record = HumanRating(
                        university=university,
                        case_id=case_id,
                        dimension=dimension,
                        coder=coder,
                        score=int(score) if score else None,
                        search_terms=logged_search_terms,
                        documents_checked=logged_documents_checked,
                        reason=reason,
                        uncertainty_note=uncertainty,
                    )
                except Exception as exc:
                    st.error(f"Rating validation failed: {exc}")
                else:
                    revision = conn.execute(
                        """SELECT COALESCE(MAX(revision),0)+1 FROM rating_revisions
                        WHERE university=? AND case_id=? AND dimension=? AND coder=?""",
                        (university, case_id, dimension, coder),
                    ).fetchone()[0]
                    protocol = latest_protocol(conn)
                    conn.execute(
                        """INSERT INTO rating_revisions
                        (university, case_id, dimension, coder, revision, protocol_version_id,
                         score, search_terms, documents_checked, reason, uncertainty_note, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (university, case_id, dimension, coder, revision, protocol["id"],
                         record.score, record.search_terms, record.documents_checked,
                         record.reason, record.uncertainty_note, utc_now()),
                    )
                    conn.commit()
                    st.success(f"Saved immutable human-rating revision {revision}.")
                    st.rerun()
    rating_history = conn.execute(
        """SELECT revision, score, search_terms, documents_checked, reason,
        uncertainty_note, created_at, protocol_version_id
        FROM rating_revisions WHERE university=? AND case_id=? AND dimension=? AND coder=?
        ORDER BY revision""",
        (university, case_id, dimension, coder),
    ).fetchall()
    if rating_history:
        with st.expander("Human-rating revision history"):
                    st.write([dict(row) for row in rating_history])

    st.divider()
    st.markdown("**Select evidence from the verified index**")
    source_rows = conn.execute(
        "SELECT source_id, title FROM documents WHERE university=? ORDER BY source_id",
        (university,),
    ).fetchall()
    source_labels = {
        row["source_id"]: f"{row['source_id']} — {row['title']}" for row in source_rows
    }
    source_id = st.selectbox(
        "Source register ID", list(source_labels), format_func=lambda value: source_labels[value],
        key=f"ev_source_{university}_{scenario_id}_{dimension}",
    )
    pages = conn.execute(
        """SELECT viewer_page, bundle_page, flags_json FROM pages
        WHERE university=? AND source_id=? ORDER BY viewer_page""",
        (university, source_id),
    ).fetchall()
    if not pages:
        st.warning("No PDF pages were indexed with this source ID footer.")
        return
    page_choices = [row["viewer_page"] for row in pages]
    viewer_page = st.selectbox(
        "Original PDF viewer page (not the printed Bundle page)",
        page_choices,
        format_func=lambda p: (
            f"viewer p. {p} | printed Bundle p. "
            f"{next(row['bundle_page'] for row in pages if row['viewer_page'] == p) or 'not extracted'}"
        ),
        key=f"ev_page_{university}_{scenario_id}_{dimension}",
    )
    page = page_details(conn, university, viewer_page)
    if page is None:
        st.error("Selected page is missing from the index.")
        return
    lines: dict[str, str] = json.loads(page["lines_json"])
    labels = sorted(lines, key=lambda value: int(value[1:]))
    if not labels:
        st.error("This page has no extracted visible L-number labels; do not invent line numbers.")
        render_page_image(university, viewer_page)
        return
    start_col, end_col = st.columns(2)
    with start_col:
        line_start = st.selectbox(
            "Visible line range start", labels,
            key=f"ev_start_{university}_{scenario_id}_{dimension}",
        )
    start_index = labels.index(line_start)
    end_labels = labels[start_index:]
    with end_col:
        line_end = st.selectbox(
            "Visible line range end", end_labels,
            key=f"ev_end_{university}_{scenario_id}_{dimension}",
        )
    quote = indexed_quote(lines, line_start, line_end)
    page_flags = json.loads(page["flags_json"])
    reading_order_review_required = (
        "scrambled_reading_order" in page_flags or "ambiguous_table" in page_flags
    )
    st.markdown(f"**Selected source passage: {source_id}, viewer p. {viewer_page}, {line_start}-{line_end}**")
    kind = st.radio(
        "How will you record this passage?",
        ["quotation", "paraphrase"],
        horizontal=True,
        key=f"ev_kind_{university}_{scenario_id}_{dimension}",
    )
    if reading_order_review_required and kind == "quotation":
        st.warning(
            "This page has a table-layout review flag. The indexed line text may interleave columns; "
            "check the original PDF and correct the quotation's reading order before saving."
        )
        quote = st.text_area(
            "Quotation candidate from index — visually verify and correct",
            value=quote,
            key=(
                f"ev_quote_{university}_{scenario_id}_{dimension}_"
                f"{source_id}_{viewer_page}_{line_start}_{line_end}"
            ),
        )
        reading_order_verified = st.checkbox(
            "I checked this exact quotation against the original PDF page.",
            key=f"ev_reading_order_{university}_{scenario_id}_{dimension}",
        )
    else:
        st.text_area(
            "Quotation copied directly from the verified index (read-only)",
            value=quote,
            disabled=True,
        )
        reading_order_verified = False
    paraphrase = ""
    if kind == "paraphrase":
        paraphrase = st.text_area("Your paraphrase (distinct from a quotation)", key=f"ev_paraphrase_{university}_{scenario_id}_{dimension}")
    operative = st.checkbox(
        f"I have independently verified this provision is operative at {POLICY_CUTOFF.isoformat()}",
        key=f"ev_operative_{university}_{scenario_id}_{dimension}",
    )
    operative_version_evidence = ""
    if operative:
        operative_version_evidence = st.text_area(
            "Recorded version evidence (identify the version/date and where it appears; not its retrieval date)",
            key=f"ev_version_evidence_{university}_{scenario_id}_{dimension}",
        )
    if page_flags:
        st.warning("Page has index review flags: " + "; ".join(page_flags))
    render_page_image(university, viewer_page)
    if st.button("Add this evidence passage", key=f"ev_save_{university}_{scenario_id}_{dimension}"):
        citation = Citation(
            university=university,
            source_id=source_id,
            viewer_page=viewer_page,
            line_start=line_start,
            line_end=line_end,
            kind=kind,
            quotation_text=quote if kind == "quotation" else "",
            paraphrase_text=paraphrase if kind == "paraphrase" else "",
            operative_at_cutoff=operative,
            operative_version_evidence=operative_version_evidence,
            reading_order_verified=reading_order_verified,
        )
        problems = validate_citation(conn, citation)
        if problems:
            for problem in problems:
                st.error(problem)
        else:
            conn.execute(
                """INSERT INTO evidence_passages
                (university, case_id, dimension, coder, source_id, viewer_page,
                 line_start, line_end, kind, quotation_text, paraphrase_text,
                 operative_at_cutoff, operative_version_evidence,
                 reading_order_verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (university, case_id, dimension, coder, source_id, viewer_page,
                 line_start, line_end, kind, citation.quotation_text, citation.paraphrase_text,
                 int(operative), citation.operative_version_evidence,
                 int(citation.reading_order_verified), utc_now()),
            )
            conn.commit()
            st.success("Evidence passage saved with its own source, PDF page, and visible line range.")
            st.rerun()
    saved = conn.execute(
        """SELECT * FROM evidence_passages
        WHERE university=? AND case_id=? AND dimension=? AND coder=? ORDER BY id DESC""",
        (university, case_id, dimension, coder),
    ).fetchall()
    if saved:
        st.markdown("**Saved evidence passages**")
        for row in saved:
            st.markdown(f"- {citation_display(row)}")
            st.text(row["quotation_text"] if row["kind"] == "quotation" else row["paraphrase_text"])


def render_case_summary(conn: sqlite3.Connection, university: str, scenario_id: str, coder: str) -> None:
    case_id = get_case_id(university, scenario_id)
    previous = latest_summary(conn, university, case_id, coder)
    protocol = latest_protocol(conn)
    risk_map = json.loads(protocol["critical_risks_json"])
    agreed_risks = risk_map.get(scenario_id, "").strip()
    critical_options = ["", "Yes", "No", "Uncertain"] if agreed_risks else ["", "Uncertain"]
    passages = current_evidence(conn, university, case_id, coder)
    labels = {None: "— Select evidence passage —"}
    labels.update({row["id"]: citation_display(row) for row in passages})
    st.markdown("### Case summary and outcome")
    st.write(f"Protocol version {protocol['version']} critical risks: {agreed_risks or 'not yet agreed'}")
    if not agreed_risks:
        st.warning("Agree and save critical-risk criteria before coding this field; otherwise mark it Uncertain.")
    with st.form(f"summary_{university}_{scenario_id}"):
        mode = st.selectbox("Mode", ["", "Prescribing", "Delegating", "None"],
                            index=["", "Prescribing", "Delegating", "None"].index(previous["mode"] if previous else ""))
        review_route = st.selectbox("Applicable review route", ["", "Yes", "No"],
                                    index=["", "Yes", "No"].index(previous["review_route"] if previous else ""))
        route_evidence = st.selectbox(
            "Supporting source for the review route",
            list(labels),
            format_func=lambda value: labels[value],
            index=(
                list(labels).index(previous["review_route_evidence_id"])
                if previous and previous["review_route_evidence_id"] in labels
                else 0
            ),
        )
        review_route_explanation = st.text_area(
            "Brief explanation of which decision the route reviews",
            value=previous["review_route_explanation"] if previous else "",
        )
        prior_critical_gap = previous["critical_gap"] if previous else ""
        critical_gap = st.selectbox(
            "Critical gap against the agreed protocol criteria",
            critical_options,
            index=critical_options.index(prior_critical_gap) if prior_critical_gap in critical_options else 0,
        )
        specific_gap_evidence = st.text_area(
            "Specific gap and supporting evidence/search record",
            value=previous["specific_gap_evidence"] if previous else "",
        )
        evidence_status = st.selectbox("Evidence status", ["", "Sufficient", "Insufficient"],
                                       index=["", "Sufficient", "Insufficient"].index(previous["evidence_status"] if previous else ""))
        insufficient_evidence_reason = st.text_area(
            "Reason if evidence is insufficient",
            value=previous["insufficient_evidence_reason"] if previous else "",
        )
        rule_direction = st.selectbox("Rule direction", ["", "Permission", "Prohibition", "Conditional", "Unclear"],
                                      index=["", "Permission", "Prohibition", "Conditional", "Unclear"].index(previous["rule_direction"] if previous else ""))
        conflict_a = st.selectbox("D3 conflict provision A", list(labels),
                                  format_func=lambda value: labels[value],
                                  index=list(labels).index(previous["conflict_a_id"]) if previous and previous["conflict_a_id"] in labels else 0)
        conflict_b = st.selectbox("D3 conflict provision B", list(labels),
                                  format_func=lambda value: labels[value],
                                  index=list(labels).index(previous["conflict_b_id"]) if previous and previous["conflict_b_id"] in labels else 0)
        conflict_confirmed = st.checkbox(
            "I confirm both cited provisions are operative, incompatible, and not reconciled by hierarchy, exception, or supersession.",
            value=bool(previous["conflict_confirmed"]) if previous else False,
        )
        conflict_explanation = st.text_area(
            "Conflict explanation (why hierarchy, exceptions, or supersession do not resolve it)",
            value=previous["conflict_explanation"] if previous else "",
        )
        case_note = st.text_area("Case note / case-summary fields", value=previous["case_note"] if previous else "")
        submitted = st.form_submit_button("Save case summary as a new revision")
    if submitted:
        try:
            record = CaseSummary(
                university=university,
                case_id=case_id,
                coder=coder,
                mode=mode,
                review_route=review_route,
                review_route_evidence_id=route_evidence,
                review_route_explanation=review_route_explanation,
                critical_gap=critical_gap,
                specific_gap_evidence=specific_gap_evidence,
                evidence_status=evidence_status,
                insufficient_evidence_reason=insufficient_evidence_reason,
                rule_direction=rule_direction,
                conflict_a_id=conflict_a,
                conflict_b_id=conflict_b,
                conflict_confirmed=conflict_confirmed,
                conflict_explanation=conflict_explanation,
                case_note=case_note,
            )
        except Exception as exc:
            st.error(f"Case summary validation failed: {exc}")
        else:
            scores = current_scores(conn, university, case_id, coder)
            summary_values = record.model_dump()
            errors, _warnings, conflict_valid = validate_outcome_inputs(
                conn, university, case_id, coder, scores, summary_values
            )
            if errors:
                for error in errors:
                    st.error(error)
            else:
                revision = conn.execute(
                    """SELECT COALESCE(MAX(revision),0)+1 FROM case_revisions
                    WHERE university=? AND case_id=? AND coder=?""",
                    (university, case_id, coder),
                ).fetchone()[0]
                protocol = latest_protocol(conn)
                conn.execute(
                    """INSERT INTO case_revisions
                    (university, case_id, coder, revision, protocol_version_id, mode,
                     review_route, review_route_evidence_id, review_route_explanation,
                     critical_gap, specific_gap_evidence, evidence_status,
                     insufficient_evidence_reason, rule_direction,
                     conflict_a_id, conflict_b_id, conflict_confirmed,
                     conflict_explanation, case_note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (university, case_id, coder, revision, protocol["id"], record.mode,
                     record.review_route, record.review_route_evidence_id,
                     record.review_route_explanation, record.critical_gap,
                     record.specific_gap_evidence, record.evidence_status,
                     record.insufficient_evidence_reason, record.rule_direction,
                     record.conflict_a_id, record.conflict_b_id,
                     int(record.conflict_confirmed), record.conflict_explanation,
                     record.case_note, utc_now()),
                )
                conn.commit()
                st.success(
                    f"Saved case-summary revision {revision}. Current outcome: "
                    f"{case_outcome(scores, summary_values, conflict_valid)}"
                )
                st.rerun()

    summary = latest_summary(conn, university, case_id, coder)
    scores = current_scores(conn, university, case_id, coder)
    summary_values = dict(summary) if summary else {}
    errors, warnings, conflict_valid = validate_outcome_inputs(
        conn, university, case_id, coder, scores, summary_values
    ) if summary else ([], [], False)
    outcome = case_outcome(scores, summary_values, conflict_valid)
    st.metric("Current computed outcome", outcome)
    for warning in warnings:
        st.warning(warning)
    for error in errors:
        st.error(error)
    st.caption("Scores are blank until entered; missing evidence never defaults to a zero.")
    if summary:
        revisions = conn.execute(
            "SELECT revision, created_at, protocol_version_id FROM case_revisions WHERE university=? AND case_id=? AND coder=? ORDER BY revision",
            (university, case_id, coder),
        ).fetchall()
        with st.expander("Case-summary revision history"):
            st.write([dict(row) for row in revisions])
    incomplete_scores = [dimension for dimension, score in scores.items() if score is None]
    if incomplete_scores:
        st.info("Unfinished scores (not zeros): " + ", ".join(incomplete_scores))


def render_protocol(conn: sqlite3.Connection) -> str:
    st.header("Protocol and critical-risk criteria")
    protocol = latest_protocol(conn)
    st.write(f"Policy cut-off: **{POLICY_CUTOFF.isoformat()}** (fixed)")
    st.write(f"Current protocol version: **{protocol['version']}**")
    risk_map = json.loads(protocol["critical_risks_json"])
    scenario_id = st.selectbox("Scenario whose critical risks you are defining", SCENARIO_IDS, key="protocol_scenario")
    with st.form("protocol_form"):
        coder = st.text_input("Coder identifier", value=protocol["coder"])
        current_risks = risk_map.get(scenario_id, "")
        risks = st.text_area(
            "Agreed critical risks (leave blank until explicitly agreed)",
            value=current_risks,
            help="The package's 'Risks to check' are prompts, not pre-agreed critical risks.",
        )
        notes = st.text_area("Protocol version notes", value=protocol["notes"])
        save = st.form_submit_button("Save a new protocol version")
    if save:
        risk_map[scenario_id] = risks
        version_match = re.fullmatch(r"(\d+)\.(\d+)", protocol["version"])
        next_version = f"{version_match.group(1)}.{int(version_match.group(2)) + 1}" if version_match else "1.1"
        conn.execute(
            """INSERT INTO protocol_versions
            (version, cutoff_date, coder, critical_risks_json, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (next_version, POLICY_CUTOFF.isoformat(), coder, safe_json(risk_map), notes, utc_now()),
        )
        conn.commit()
        st.success(f"Saved protocol version {next_version}; prior records remain unchanged.")
        st.rerun()
    st.caption("Package retrieval dates remain source metadata and are never treated as effective dates.")
    return protocol["coder"]


def render_source_review(conn: sqlite3.Connection, university: str) -> None:
    st.header("Source review")
    st.caption(f"Review authority and operative status at the {POLICY_CUTOFF.isoformat()} policy cut-off.")
    rows = conn.execute(
        """SELECT d.source_id, d.title, d.register_json, r.*
        FROM documents d JOIN source_review r USING (university, source_id)
        WHERE d.university=? ORDER BY d.source_id""",
        (university,),
    ).fetchall()
    if not rows:
        st.info("No source register has been indexed.")
        return
    source_ids = [row["source_id"] for row in rows]
    selected = st.selectbox(
        "Source register entry",
        source_ids,
        format_func=lambda value: f"{value} — {next(row['title'] for row in rows if row['source_id'] == value)}",
    )
    row = next(item for item in rows if item["source_id"] == selected)
    register = json.loads(row["register_json"])
    st.markdown(f"**{row['title']}** (`{selected}`)")
    st.write("Official URL:", register.get("official_url") or "Not supplied")
    st.write("Retrieved:", register.get("retrieved") or "Not supplied")
    st.caption("Retrieved is preserved as retrieval metadata; it is not copied into effective date.")
    with st.form(f"source_review_{university}_{selected}"):
        inclusion = st.selectbox("Human inclusion", ["", "Include", "Exclude", "Pending"],
                                 index=["", "Include", "Exclude", "Pending"].index(row["inclusion"]))
        inclusion_reason = st.text_area("Inclusion / exclusion reason", value=row["inclusion_reason"])
        authority = st.text_input("Authority", value=row["authority"])
        effective_date = st.text_input("Effective date (YYYY-MM-DD or blank)", value=row["effective_date"])
        operative_version = st.text_input("Operative version at cut-off", value=row["operative_version"])
        supersedes = st.text_input("Supersedes / duplicate of", value=row["supersedes"])
        reviewer = st.text_input("Reviewer", value=row["reviewer"])
        missing_instruments = st.text_area("Missing instruments / access limitations", value=row["missing_instruments"])
        save = st.form_submit_button("Save source review")
    if save:
        if effective_date:
            try:
                date.fromisoformat(effective_date)
            except ValueError:
                st.error("Effective date must be YYYY-MM-DD or blank.")
                return
        conn.execute(
            """UPDATE source_review SET inclusion=?, inclusion_reason=?, authority=?,
            effective_date=?, operative_version=?, supersedes=?, reviewer=?,
            missing_instruments=?, updated_at=? WHERE university=? AND source_id=?""",
            (inclusion, inclusion_reason, authority, effective_date, operative_version,
             supersedes, reviewer, missing_instruments, utc_now(), university, selected),
        )
        conn.commit()
        st.success("Source review saved.")
        st.rerun()


def make_ai_prompt(
    conn: sqlite3.Connection,
    university: str,
    case_id: str,
    scenario_id: str,
    dimension: str,
) -> str:
    resource = conn.execute(
        "SELECT codebook_text, scenarios_json FROM resources WHERE university=?", (university,)
    ).fetchone()
    scenario = json.loads(resource["scenarios_json"]).get(scenario_id, {}).get("text", "")
    evidence = conn.execute(
        """SELECT * FROM evidence_passages WHERE university=? AND case_id=?
        AND dimension=? ORDER BY id""",
        (university, case_id, dimension),
    ).fetchall()
    passages = "\n\n".join(
        f"[{row['source_id']}, PDF viewer p. {row['viewer_page']}, "
        f"{row['line_start']}-{row['line_end']}; kind={row['kind']}]\n"
        + (row["quotation_text"] if row["kind"] == "quotation" else row["paraphrase_text"])
        for row in evidence
    )
    return (
        "Optional external assistance only. Do not claim to be the human coder. "
        "Do not create a human rating or approval. Identify uncertainty and cite only the supplied evidence.\n\n"
        f"University: {university}\nCase: {case_id}\nDimension: {dimension} {DIMENSION_LABELS[dimension][0]}\n\n"
        f"Scenario:\n{scenario}\n\nCodebook:\n{resource['codebook_text']}\n\n"
        f"Human-selected evidence:\n{passages or '[No evidence passages selected]'}"
    )


def render_ai(conn: sqlite3.Connection, university: str, scenario_id: str, dimension: str, coder: str) -> None:
    case_id = get_case_id(university, scenario_id)
    st.header("Optional external AI assistance")
    st.warning("AI suggestions are stored separately and never become a human score, human approval, or outcome input.")
    prompt = make_ai_prompt(conn, university, case_id, scenario_id, dimension)
    st.download_button(
        "Download evidence-and-codebook prompt",
        prompt.encode("utf-8"),
        file_name=f"{case_id}_{dimension}_external_ai_prompt.txt",
        mime="text/plain",
    )
    with st.form("ai_suggestion_form"):
        suggestion = st.text_area("Optional AI response (paste manually; no API key or API call is used)")
        save = st.form_submit_button("Save as a separate AI suggestion")
    if save and suggestion.strip():
        conn.execute(
            """INSERT INTO ai_suggestions
            (university, case_id, dimension, coder, prompt, suggestion, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (university, case_id, dimension, coder, prompt, suggestion.strip(), utc_now()),
        )
        conn.commit()
        st.success("Saved in the AI suggestions table, separate from human coding.")
    rows = conn.execute(
        "SELECT id, created_at, suggestion FROM ai_suggestions WHERE university=? AND case_id=? AND dimension=? ORDER BY id DESC",
        (university, case_id, dimension),
    ).fetchall()
    for row in rows:
        with st.expander(f"AI suggestion #{row['id']} | {row['created_at']}"):
            st.text(row["suggestion"])


def validate_case_citations(conn: sqlite3.Connection, university: str) -> list[str]:
    problems: list[str] = []
    rows = conn.execute(
        "SELECT * FROM evidence_passages WHERE university=? ORDER BY id", (university,)
    ).fetchall()
    for row in rows:
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
        problems.extend(
            f"Evidence #{row['id']}: {problem}" for problem in validate_citation(conn, citation)
        )
    return problems


def validate_case_outcomes(conn: sqlite3.Connection, university: str, coder: str) -> list[str]:
    problems: list[str] = []
    for scenario_id in SCENARIO_IDS:
        case_id = get_case_id(university, scenario_id)
        summary = latest_summary(conn, university, case_id, coder)
        if summary is None:
            continue
        scores = current_scores(conn, university, case_id, coder)
        errors, _warnings, _conflict_valid = validate_outcome_inputs(
            conn, university, case_id, coder, scores, dict(summary)
        )
        problems.extend(f"{case_id}: {problem}" for problem in errors)
    return problems


def export_text(conn: sqlite3.Connection, university: str, coder: str) -> str:
    text, _report = render_university_text(conn, ROOT, university, coder)
    return text


def export_workbook(conn: sqlite3.Connection, university: str, coder: str) -> bytes:
    templates = list((RAW_PATH / university).glob("Human_Coder_1_*.xlsx"))
    if len(templates) != 1:
        raise FileNotFoundError(f"Expected one original workbook for {university}.")
    workbook = load_workbook(templates[0], data_only=False)
    original_formula_map = {
        sheet.title: {
            cell.coordinate: cell.value
            for row in sheet.iter_rows()
            for cell in row
            if isinstance(cell.value, str) and cell.value.startswith("=")
        }
        for sheet in workbook.worksheets
    }
    cases = workbook["Cases"]
    evidence_sheet = workbook["Evidence"]
    case_header = {cases.cell(4, col).value: col for col in range(1, cases.max_column + 1)}
    evidence_header = {evidence_sheet.cell(4, col).value: col for col in range(1, evidence_sheet.max_column + 1)}
    case_rows = {
        cases.cell(row, case_header["case_id"]).value: row
        for row in range(5, cases.max_row + 1)
        if cases.cell(row, case_header["case_id"]).value
    }
    evidence_rows = {
        (evidence_sheet.cell(row, evidence_header["case_id"]).value,
         evidence_sheet.cell(row, evidence_header["dimension"]).value): row
        for row in range(5, evidence_sheet.max_row + 1)
        if evidence_sheet.cell(row, evidence_header["case_id"]).value
    }
    for case_id, row_num in case_rows.items():
        summary = latest_summary(conn, university, case_id, coder)
        if summary:
            for field, column in (
                ("mode", "mode"), ("review_route", "review_route"),
                ("critical_gap", "critical_gap"), ("evidence_status", "evidence_status"),
                ("rule_direction", "rule_direction"), ("conflict_explanation", "conflict_basis"),
                ("case_note", "case_note"),
            ):
                if column in case_header:
                    cases.cell(row_num, case_header[column]).value = summary[field]
        for dimension in DIMENSIONS:
            target_row = evidence_rows.get((case_id, dimension))
            if target_row is None:
                continue
            rating = latest_rating(conn, university, case_id, dimension, coder)
            if rating:
                for field, column in (
                    ("search_terms", "search_terms"),
                    ("documents_checked", "documents_checked"),
                    ("score", "score"),
                    ("reason", "reason_for_score"),
                    ("uncertainty_note", "negative_search_or_access_note"),
                ):
                    if column in evidence_header:
                        evidence_sheet.cell(target_row, evidence_header[column]).value = rating[field]
            passages = conn.execute(
                """SELECT * FROM evidence_passages WHERE university=? AND case_id=?
                AND dimension=? AND coder=? ORDER BY id""",
                (university, case_id, dimension, coder),
            ).fetchall()
            if passages:
                citation_parts = []
                source_parts = []
                excerpt_parts = []
                for item in passages:
                    page = page_details(conn, university, item["viewer_page"])
                    bundle = page["bundle_page"] if page else None
                    source_parts.append(item["source_id"])
                    citation_parts.append(
                        f"viewer p. {item['viewer_page']}; Bundle p. "
                        f"{bundle if bundle is not None else 'not extracted'}; "
                        f"{item['line_start']}-{item['line_end']}"
                    )
                    excerpt_parts.append(
                        f"[{item['kind']}] " + (
                            item["quotation_text"] if item["kind"] == "quotation" else item["paraphrase_text"]
                        )
                    )
                for column, value in (
                    ("your_source_ids", "; ".join(dict.fromkeys(source_parts))),
                    ("your_pages_lines", "; ".join(citation_parts)),
                    ("quotation_or_paraphrase", "\n\n".join(excerpt_parts)),
                ):
                    if column in evidence_header:
                        evidence_sheet.cell(target_row, evidence_header[column]).value = value
    for sheet in workbook.worksheets:
        after = {
            cell.coordinate: cell.value
            for row in sheet.iter_rows()
            for cell in row
            if isinstance(cell.value, str) and cell.value.startswith("=")
        }
        if after != original_formula_map[sheet.title]:
            raise RuntimeError(f"Export unexpectedly changed formulas in workbook sheet {sheet.title}.")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def render_exports(conn: sqlite3.Connection, university: str, coder: str) -> None:
    st.header("Exports and validation")
    citation_problems = validate_case_citations(conn, university)
    outcome_problems = validate_case_outcomes(conn, university, coder)
    format_problems, incomplete = validate_export(conn, ROOT, university, coder)
    problems = citation_problems + outcome_problems + format_problems
    if problems:
        st.error("Citation or codebook-condition validation found problems. Resolve these before exporting:")
        for problem in problems:
            st.write(f"- {problem}")
    else:
        st.success("All saved evidence citations validate against this university's index.")
    required = required_field_issues(conn, university, coder)
    required.extend(incomplete)
    if required:
        st.warning(
            f"{len(required)} required fields remain incomplete. Draft exports are allowed; "
            "blank scores stay blank."
        )
        with st.expander("Required-field checklist"):
            st.write(required[:100])
            if len(required) > 100:
                st.caption(f"{len(required) - 100} additional items are not shown here.")
    report_path = ROOT / "data" / "derived" / "codebook_workbook_comparison.json"
    if report_path.exists():
        with st.expander("Codebook vs. original workbook formulas"):
            st.json(json.loads(report_path.read_text(encoding="utf-8")))
    if problems:
        return
    try:
        text_content = export_text(conn, university, coder)
    except ExportValidationError as exc:
        for problem in exc.problems:
            st.error(problem)
        return
    st.download_button(
        f"Download {university} plain-text D1–D6 plus case summaries",
        text_content.encode("utf-8"),
        file_name=f"{university}_coding.txt",
        mime="text/plain",
    )
    if st.button("Save one fixed-format text file per university to data/exports"):
        try:
            reports = export_all(ROOT, DB_PATH, ROOT / "data" / "exports", coder)
        except ExportValidationError as exc:
            for problem in exc.problems:
                st.error(problem)
        else:
            st.success("Saved all five text files and validation report.")
            st.json(reports)
    workbook_bytes = export_workbook(conn, university, coder)
    st.download_button(
        f"Download NEW {university} workbook copy",
        workbook_bytes,
        file_name=f"{university}_coding_copy.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    all_validation_problems = [
        f"{institution}: {problem}"
        for institution in UNIVERSITIES
        for problem in (
            validate_case_citations(conn, institution)
            + validate_case_outcomes(conn, institution, coder)
            + validate_export(conn, ROOT, institution, coder)[0]
        )
    ]
    if all_validation_problems:
        st.error("All-university archives are withheld until every citation and codebook condition validates.")
        with st.expander("All-university validation issues"):
            st.write(all_validation_problems)
    else:
        text_zip = io.BytesIO()
        workbook_zip = io.BytesIO()
        with zipfile.ZipFile(text_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for institution in UNIVERSITIES:
                archive.writestr(
                    f"{institution}_coding.txt",
                    export_text(conn, institution, coder),
                )
        with zipfile.ZipFile(workbook_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for institution in UNIVERSITIES:
                archive.writestr(
                    f"{institution}_coding_copy.xlsx",
                    export_workbook(conn, institution, coder),
                )
        st.download_button(
            "Download all five university text files (.zip)",
            text_zip.getvalue(),
            file_name="all_universities_coding_text.zip",
            mime="application/zip",
        )
        st.download_button(
            "Download all five NEW workbook copies (.zip)",
            workbook_zip.getvalue(),
            file_name="all_universities_workbook_copies.zip",
            mime="application/zip",
        )
    st.caption("The package workbook and original ZIPs are never edited; formulas are checked before the copy is offered.")


def render_index_health(conn: sqlite3.Connection) -> None:
    st.header("Index health and provenance")
    path = ROOT / "data" / "derived" / "file_hashes.json"
    if path.exists():
        st.json(json.loads(path.read_text(encoding="utf-8")))
    issues_path = ROOT / "data" / "derived" / "index_issues.json"
    if issues_path.exists():
        issues = json.loads(issues_path.read_text(encoding="utf-8"))
        st.write(f"Flagged extraction/table review items: {len(issues.get('pages', []))}")
        for item in issues.get("pages", []):
            st.write(item)
    else:
        st.warning("Index issue report is missing.")
    stats = conn.execute(
        """SELECT university, COUNT(*) AS page_count,
        SUM(CASE WHEN bundle_page IS NOT NULL THEN 1 ELSE 0 END) AS printed_pages,
        SUM(CASE WHEN json_array_length(flags_json)>0 THEN 1 ELSE 0 END) AS flagged_pages,
        SUM(CASE WHEN lines_json!='{}' THEN 1 ELSE 0 END) AS pages_with_L_labels
        FROM pages GROUP BY university ORDER BY university"""
    ).fetchall()
    st.dataframe([dict(row) for row in stats], use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Policy Coding", layout="wide")
    st.title("Local, human-reviewed policy coding")
    if not DB_PATH.exists():
        st.error("Policy index is not built. Run `python -m policy_coder.indexer` from the workspace first.")
        return
    conn = database()
    universities = available_universities(conn)
    if not universities:
        st.error("No indexed university packages found.")
        return

    with st.sidebar:
        st.header("Workspace")
        university = st.selectbox("University", universities)
        scenario_id = st.selectbox("Scenario", SCENARIO_IDS)
        dimension = st.selectbox("Dimension", DIMENSIONS)
        st.caption(f"Policy cut-off: {POLICY_CUTOFF.isoformat()}")
        page_name = st.radio(
            "Screen",
            ["Code a dimension", "Full-text search", "Source review", "Protocol & risks",
             "AI prompt", "Exports", "Index health"],
        )

    protocol = latest_protocol(conn)
    coder = protocol["coder"]
    if page_name == "Code a dimension":
        resource = conn.execute(
            "SELECT codebook_text, scenarios_json FROM resources WHERE university=?",
            (university,),
        ).fetchone()
        if resource:
            render_dimension(
                conn, university, scenario_id, dimension, coder,
                json.loads(resource["scenarios_json"]), resource["codebook_text"],
            )
            st.divider()
            render_case_summary(conn, university, scenario_id, coder)
    elif page_name == "Full-text search":
        render_search(conn, university, scenario_id, dimension, coder)
    elif page_name == "Source review":
        render_source_review(conn, university)
    elif page_name == "Protocol & risks":
        render_protocol(conn)
    elif page_name == "AI prompt":
        render_ai(conn, university, scenario_id, dimension, coder)
    elif page_name == "Exports":
        render_exports(conn, university, coder)
    else:
        render_index_health(conn)


if __name__ == "__main__":
    main()
