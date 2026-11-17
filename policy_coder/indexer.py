from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import pymupdf as fitz
from docx import Document
from openpyxl import load_workbook

from policy_coder.core import (
    UNIVERSITIES,
    codebook_workbook_report,
    connect,
    initialize_db,
    safe_json,
    utc_now,
)


PACKAGE_SUFFIX = "_One_Coder_Package.zip"
MAX_MEMBER_SIZE = 512 * 1024 * 1024
MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
LINE_RE = re.compile(r"L\d{3}")
SOURCE_RE = re.compile(
    r"\|\s*([NA]\d{3})\s*\|\s*source (?:reading )?page", re.IGNORECASE
)
BUNDLE_RE = re.compile(r"Bundle page\s+(\d+)", re.IGNORECASE)
SCENARIO_RE = re.compile(r"^(S\d{1,2})\s+(.+)$")
Word = tuple[float, float, float, float, str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_member_path(member: zipfile.ZipInfo, expected_root: str) -> Path:
    name = member.filename
    posix = PurePosixPath(name)
    if (
        posix.is_absolute()
        or ".." in posix.parts
        or "\x00" in name
        or "\\" in name
        or any(":" in part for part in posix.parts)
    ):
        raise ValueError(f"Unsafe archive path rejected: {name!r}")
    if not posix.parts or posix.parts[0] != expected_root:
        raise ValueError(f"Unexpected top-level archive directory: {name!r}")
    if member.external_attr >> 16 & 0o170000 == 0o120000:
        raise ValueError(f"Symbolic link in archive rejected: {name!r}")
    return Path(*posix.parts[1:])


def extract_packages(root: Path) -> tuple[dict[str, Any], list[str]]:
    raw_root = root / "data" / "raw"
    derived_root = root / "data" / "derived"
    raw_root.mkdir(parents=True, exist_ok=True)
    derived_root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"created_at": utc_now(), "packages": {}}
    issues: list[str] = []
    archives = sorted(root.glob(f"*{PACKAGE_SUFFIX}"))
    if len(archives) != len(UNIVERSITIES):
        raise FileNotFoundError(
            f"Expected {len(UNIVERSITIES)} {PACKAGE_SUFFIX} archives in {root}; found {len(archives)}."
        )

    for archive in archives:
        university = archive.name[: -len(PACKAGE_SUFFIX)]
        if university not in UNIVERSITIES:
            issues.append(f"Unrecognized package filename: {archive.name}")
            continue
        package_record: dict[str, Any] = {
            "archive": archive.name,
            "archive_sha256": sha256_file(archive),
            "files": {},
        }
        with zipfile.ZipFile(archive) as zipped:
            entries = [member for member in zipped.infolist() if not member.is_dir()]
            if not entries:
                raise ValueError(f"No files in {archive.name}")
            total_size = sum(member.file_size for member in entries)
            if total_size > MAX_ARCHIVE_SIZE:
                raise ValueError(f"Archive exceeds the {MAX_ARCHIVE_SIZE}-byte extraction limit: {archive.name}")
            first_parts = {PurePosixPath(item.filename).parts[0] for item in entries}
            if len(first_parts) != 1:
                raise ValueError(f"Archive has mixed top-level directories: {archive.name}")
            expected_root = next(iter(first_parts))
            destination_root = raw_root / university
            destination_root.mkdir(parents=True, exist_ok=True)
            seen_paths: set[Path] = set()

            for member in entries:
                relative = _safe_member_path(member, expected_root)
                if not relative.parts:
                    raise ValueError(f"Archive member has no filename: {member.filename}")
                if relative in seen_paths:
                    raise ValueError(f"Duplicate archive output path rejected: {member.filename!r}")
                seen_paths.add(relative)
                if member.file_size > MAX_MEMBER_SIZE:
                    raise ValueError(f"Archive member exceeds the extraction limit: {member.filename!r}")
                if member.file_size and (
                    not member.compress_size
                    or member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ValueError(f"Suspicious compression ratio rejected: {member.filename!r}")
                destination = destination_root.joinpath(*relative.parts)
                if not destination.resolve().is_relative_to(destination_root.resolve()):
                    raise ValueError(f"Archive member escapes extraction directory: {member.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", prefix=destination.name + ".", suffix=".extracting",
                        dir=destination.parent, delete=False,
                    ) as target:
                        temporary = Path(target.name)
                        with zipped.open(member) as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                digest.update(chunk)
                                target.write(chunk)
                    if temporary.stat().st_size != member.file_size:
                        raise ValueError(f"Extracted size mismatch for archive member: {member.filename!r}")
                    temporary.replace(destination)
                finally:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()
                package_record["files"][relative.as_posix()] = {
                    "path": str(destination.relative_to(root)),
                    "sha256": digest.hexdigest(),
                    "size": member.file_size,
                }
        manifest["packages"][university] = package_record

    manifest_path = derived_root / "file_hashes.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (derived_root / "index_issues.json").write_text(
        json.dumps(issues, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest, issues


def docx_text(data: bytes) -> str:
    document = Document(io.BytesIO(data))
    parts = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            values = [cell.text.strip().replace("\n", " / ") for cell in row.cells]
            if any(values):
                parts.append(" | ".join(values))
    return "\n".join(parts)


def parse_scenarios(data: bytes) -> dict[str, dict[str, str]]:
    document = Document(io.BytesIO(data))
    scenarios: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        match = SCENARIO_RE.match(text)
        if match:
            scenario_id, title = match.groups()
            current = {"scenario_id": scenario_id, "title": title, "text": text}
            scenarios[scenario_id] = current
        elif current is not None:
            current["text"] += "\n" + text
    return scenarios


def _table_flags(page: fitz.Page) -> list[str]:
    flags: list[str] = []
    try:
        find_tables = getattr(page, "find_tables", None)
        tables = find_tables().tables if find_tables else []
        if tables:
            flags.append("ambiguous_table")
            if _has_interleaved_table_columns(page, tables):
                flags.append("scrambled_reading_order")
    except Exception as exc:
        flags.append(f"table_detection_error: {type(exc).__name__}")
    return flags


def _page_words(page: fitz.Page) -> list[Word]:
    words: list[Word] = []
    for word in page.get_text("words", sort=False):
        if len(word) < 5:
            continue
        x0, y0, x1, y1, text = word[:5]
        if not all(isinstance(value, (int, float)) for value in (x0, y0, x1, y1)):
            continue
        if isinstance(text, str):
            words.append((float(x0), float(y0), float(x1), float(y1), text))
    return words


def _has_interleaved_table_columns(page: fitz.Page, tables: list[Any]) -> bool:
    words = _page_words(page)
    labels = {
        word[4]: (word[1] + word[3]) / 2
        for word in words
        if word[0] < 50 and LINE_RE.fullmatch(word[4])
    }
    if not labels:
        return False

    for table in tables:
        cells = [cell for cell in table.cells if cell]
        boundaries = sorted(
            {round(float(edge), 2) for cell in cells for edge in (cell[0], cell[2])}
        )
        if len(boundaries) < 3:
            continue
        x0, y0, x1, y1 = table.bbox
        table_words = [
            word
            for word in words
            if x0 <= (word[0] + word[2]) / 2 <= x1
            and y0 <= (word[1] + word[3]) / 2 <= y1
        ]
        for label_y in labels.values():
            occupied_columns: set[int] = set()
            for word in table_words:
                word_x0, word_y0, word_x1, word_y1, text = word
                if word_x0 < 50 or LINE_RE.fullmatch(text):
                    continue
                center_x = (word_x0 + word_x1) / 2
                center_y = (word_y0 + word_y1) / 2
                if not (x0 <= center_x <= x1 and y0 <= center_y <= y1):
                    continue
                if abs(label_y - center_y) > 14:
                    continue
                for column, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
                    if left <= center_x < right:
                        occupied_columns.add(column)
                        break
                if len(occupied_columns) > 1:
                    return True
    return False


def page_lines(page: fitz.Page) -> dict[str, str]:
    words = _page_words(page)
    label_words = [
        word for word in words
        if word[0] < 50 and LINE_RE.fullmatch(word[4])
    ]
    labels: dict[str, float] = {}
    for word in label_words:
        labels[word[4]] = (word[1] + word[3]) / 2
    grouped: dict[str, list[tuple[float, str]]] = {label: [] for label in labels}
    for word in words:
        x0, y0, _x1, y1, text = word
        if x0 < 50 or LINE_RE.fullmatch(text):
            continue
        center = (y0 + y1) / 2
        if labels:
            label = min(labels, key=lambda key: abs(labels[key] - center))
            if abs(labels[label] - center) <= 14:
                grouped[label].append((x0, text))
    return {
        label: " ".join(text for _, text in sorted(row, key=lambda item: item[0])).strip()
        for label, row in sorted(grouped.items(), key=lambda item: int(item[0][1:]))
    }


def index_university(root: Path, university: str, conn) -> list[dict[str, Any]]:
    folder = root / "data" / "raw" / university
    pdfs = list(folder.glob("*.pdf"))
    registers = list(folder.glob("Source_Register_*.csv"))
    codebooks = list(folder.glob("Human_Codebook.docx"))
    scenario_docs = list(folder.glob("Scenarios.docx"))
    templates = list(folder.glob("Human_Coder_1_*.xlsx"))
    expected = (pdfs, registers, codebooks, scenario_docs, templates)
    if any(len(group) != 1 for group in expected):
        raise ValueError(f"{university}: expected one PDF, register, codebook, scenarios, and workbook.")

    with registers[0].open("r", encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream))
    if not records:
        raise ValueError(f"{university}: source register is empty.")
    source_ids: set[str] = set()
    for record in records:
        source_id = (record.get("source_id") or "").strip()
        if not source_id or source_id in source_ids:
            raise ValueError(f"{university}: missing or duplicate source ID {source_id!r}.")
        source_ids.add(source_id)
        record_university = (record.get("university") or "").strip()
        if record_university and record_university not in {university, UNIVERSITIES[university]}:
            raise ValueError(f"{university}: register contains university {record_university!r}.")
        conn.execute(
            """INSERT OR REPLACE INTO documents(university, source_id, title, register_json)
            VALUES (?, ?, ?, ?)""",
            (university, source_id, (record.get("title") or "").strip(), safe_json(record)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO source_review(university, source_id) VALUES (?, ?)",
            (university, source_id),
        )

    codebook = docx_text(codebooks[0].read_bytes())
    scenarios = parse_scenarios(scenario_docs[0].read_bytes())
    conn.execute(
        "INSERT OR REPLACE INTO resources(university, codebook_text, scenarios_json) VALUES (?, ?, ?)",
        (university, codebook, safe_json(scenarios)),
    )

    conn.execute("DELETE FROM page_fts WHERE university=?", (university,))
    conn.execute("DELETE FROM pages WHERE university=?", (university,))
    pdf = fitz.open(pdfs[0])
    page_issues: list[dict[str, Any]] = []
    for zero_page, page in enumerate(pdf):
        viewer_page = zero_page + 1
        flags = _table_flags(page)
        try:
            raw_text = page.get_text("text", sort=False)
            lines = page_lines(page)
        except Exception as exc:
            raw_text = ""
            lines = {}
            flags.append(f"extraction_failure: {type(exc).__name__}: {exc}")

        text = LINE_RE.sub("", raw_text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            flags.append("no_extractable_text")
        bundle_match = BUNDLE_RE.search(raw_text)
        source_match = SOURCE_RE.search(raw_text)
        bundle_page = int(bundle_match.group(1)) if bundle_match else None
        source_id = source_match.group(1).upper() if source_match else None
        if bundle_page is not None and bundle_page <= 4:
            pass
        elif bundle_page is not None and not lines:
            flags.append("printed_bundle_page_has_no_extracted_L_labels")
        if source_id and source_id not in source_ids:
            flags.append("footer_source_id_missing_from_register")
        if source_id is None and lines:
            flags.append("L_labels_present_but_source_id_not_extracted")
        if flags:
            page_issues.append(
                {"university": university, "viewer_page": viewer_page, "flags": flags}
            )
        conn.execute(
            """INSERT OR REPLACE INTO pages
            (university, viewer_page, bundle_page, source_id, text, lines_json, flags_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (university, viewer_page, bundle_page, source_id, text, safe_json(lines), safe_json(flags)),
        )
        conn.execute(
            "INSERT INTO page_fts(university, viewer_page, source_id, text) VALUES (?, ?, ?, ?)",
            (university, viewer_page, source_id or "", text),
        )

    return page_issues


def record_template_findings(root: Path, university: str, report: dict[str, Any]) -> None:
    templates = list((root / "data" / "raw" / university).glob("Human_Coder_1_*.xlsx"))
    workbook = load_workbook(templates[0], data_only=False)
    sheet_report: dict[str, Any] = {}
    for sheet_name in ("Cases", "Evidence"):
        if sheet_name not in workbook.sheetnames:
            sheet_report[sheet_name] = {"missing": True}
            continue
        sheet = workbook[sheet_name]
        header_row = next(
            (row for row in range(1, min(sheet.max_row, 10) + 1)
             if any(sheet.cell(row, col).value == "case_id" for col in range(1, sheet.max_column + 1))),
            None,
        )
        headers = (
            {sheet.cell(header_row, col).value: col for col in range(1, sheet.max_column + 1)}
            if header_row else {}
        )
        formulas = {
            sheet.cell(row, col).coordinate: sheet.cell(row, col).value
            for row in range(1, sheet.max_row + 1)
            for col in range(1, sheet.max_column + 1)
            if isinstance(sheet.cell(row, col).value, str)
            and sheet.cell(row, col).value.startswith("=")
        }
        sheet_report[sheet_name] = {
            "header_row": header_row,
            "headers": headers,
            "formula_count": len(formulas),
            "first_formulas": dict(list(formulas.items())[:12]),
            "dimensions": [sheet.max_row, sheet.max_column],
        }
        if sheet_name == "Cases" and header_row:
            outcome_column = headers.get("computed_outcome")
            sheet_report[sheet_name]["outcome_formula"] = (
                sheet.cell(5, outcome_column).value if outcome_column else None
            )
    report["workbooks"][university] = sheet_report


def build_index(root: Path) -> dict[str, Any]:
    _, issues = extract_packages(root)
    db_path = root / "data" / "policy_coding.sqlite3"
    conn = connect(db_path)
    initialize_db(conn)
    all_page_issues: list[dict[str, Any]] = []
    workbook_report = codebook_workbook_report(conn)
    for university in UNIVERSITIES:
        all_page_issues.extend(index_university(root, university, conn))
        record_template_findings(root, university, workbook_report)
    outcome_formulas = {
        university: workbook_report["workbooks"][university]["Cases"].get("outcome_formula")
        for university in UNIVERSITIES
    }
    workbook_report["formula_consistency"] = {
        "outcome_formulas_identical_across_universities": len(set(outcome_formulas.values())) == 1,
        "outcome_formula_by_university": outcome_formulas,
        "codebook_comparison": (
            "The five templates use the same outcome formula."
            if len(set(outcome_formulas.values())) == 1
            else "Outcome formulas differ across templates; inspect the per-university formulas before relying on exports."
        ),
    }
    conn.commit()
    conn.close()
    derived = root / "data" / "derived"
    (derived / "index_issues.json").write_text(
        json.dumps({"package": issues, "pages": all_page_issues}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (derived / "codebook_workbook_comparison.json").write_text(
        json.dumps(workbook_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "database": str(db_path),
        "university_count": len(UNIVERSITIES),
        "page_issue_count": len(all_page_issues),
        "issues": all_page_issues,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Safely extract and index local coder packages.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = build_index(args.root.resolve())
    print(json.dumps(result, indent=2, ensure_ascii=False))

