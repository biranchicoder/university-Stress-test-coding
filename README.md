# Local Policy Coding Application

## HCoder 2 version-checklist helper

Create and activate a virtual environment, then install the project and helper-suite tooling:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

In the workspace root, run `python generate_version_checklist.py`. Edit
`version_checklist.csv`, updating only the printed/viewer page and line columns; keep the passage
identity fields intact. After completing the visual PDF review, run
`python finalize_proposals.py`. It applies the locations to the matching HCoder 2 proposal passages,
removes fields outside the strict proposal schema, and writes
`Coder_2_Complete/proposals_75_HCoder2_final.json`. If a required operative-version citation is
missing, finalization stops with a validation error and does not overwrite the output.
For passages whose version is explicitly treated as operative by the recorded researcher cutoff
rule, it keeps the version location null rather than inserting a placeholder citation.

### Researcher-cutoff rule

`version_evidence_location = null` is intentional for records whose exact
`RESEARCHER_CUTOFF_EVIDENCE` rule in [proposal_validation.py](./policy_coder/proposal_validation.py)
explicitly applies the researcher cutoff to a retrieved webpage with no internal effective date.
The finalizer removes legacy `N/A` placeholder locations rather than treating them as citations.

Run the suite with `pytest --cov=generate_version_checklist --cov=finalize_proposals
--cov=policy_coder --cov-report=html`. A passing test run confirms that the final JSON validates
against `ProposalBatch`, contains no tested disallowed fields, and has the required visual-review
fields on every passage. The HTML coverage report is written to `htmlcov/`.

In VS Code, use **Terminal → Run Task** to select the CSV, apply, test, format, lint, or type-check
task. Select either helper script from **Run and Debug** to launch it with the Python debugger.
Install the configured Git hooks once with `pre-commit install`.

A local Streamlit application for human-reviewed coding of the five supplied university policy collections. The first run safely extracts each ZIP to `data/raw`, records SHA-256 hashes, and indexes the PDFs, source registers, codebook, scenarios, and workbook templates into a local SQLite database. Original ZIPs and workbook templates are read-only inputs.

The application keeps human ratings, evidence passages, case summaries, source reviews, protocol versions, and optional AI suggestions in separate records. Ratings and summaries are append-only revisions. A blank score is unfinished work, never zero. Evidence quotations retain their own source ID, PDF viewer page, printed Bundle page, and existing visible `L###` range.

## Requirements

- Windows 10 or later
- Python 3.11 or later
- PowerShell

## Install, extract, index, test, and launch (PowerShell)

Open PowerShell in this workspace:

```powershell
Set-Location 'C:\Users\biran\Desktop\University Coding'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m policy_coder.indexer --root .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m policy_coder.text_export --root .
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

If Python 3.12 is not installed but another supported Python 3.11+ is available, replace `py -3.12` with `py -3.11` (or the installed version). The commands use the virtual-environment interpreter directly, so PowerShell script-activation policy does not need to be changed.

Streamlit prints a local URL (normally `http://localhost:8501`). Open it in your browser. Binding to `127.0.0.1` keeps the app on this computer; the app makes no network/API calls for AI and requires no API key.

## Data locations

- `data/raw/<University>/`: files extracted from that university's original ZIP.
- `data/policy_coding.sqlite3`: PDF page index and local human-coding records.
- `data/derived/file_hashes.json`: original ZIP hashes and SHA-256 hashes for each extracted package file.
- `data/derived/index_issues.json`: extraction, line-label, footer, and ambiguous-table review flags.
- `data/derived/codebook_workbook_comparison.json`: original workbook layout/formula inventory and documented differences from the codebook.
- `data/exports/<University>_coding.txt`: fixed-template plain-text exports, one file per university, plus a validation report.

Run the indexer again after restoring or changing package files. It does not modify the source ZIPs; already saved coding records remain in SQLite. Keep a backup of `data/policy_coding.sqlite3` if coding records must be preserved across machine changes.

## Resumable evidence-preparation pilot

After extraction/indexing, run this command in PowerShell:

```powershell
Set-Location 'C:\Users\biran\Desktop\University Coding'
.\.venv\Scripts\python.exe -m policy_coder.preparation --root .
```

The preparation pipeline verifies the existing package/extraction hashes, reads the indexed SQLite database without writing to it, and saves a durable checkpoint after each PDF page and task. It indexes all five university PDFs and source registers, then runs the initial search pilot for Curtin S1, S6, and S13. It does not assign ratings or change the five existing coding exports.

If interrupted, run the same command again. It resumes completed tasks from `data\preparation\.preparation_checkpoint.sqlite3`; do not delete that checkpoint. Each task gets an initial attempt plus up to three retries. Persistent failures are recorded in the checkpoint, left visible in the CSV/report, and do not stop independent tasks. Existing completed outputs are never replaced; if a saved output is changed or an input hash no longer matches, the pipeline reports the problem instead of silently resuming over it.

Outputs are written to `data\preparation\`: the page index, source audit, actual query/result log, unreviewed candidate passages, three scenario handoffs, and a validation report listing completed, failed, and pending tasks. A candidate is not confirmed evidence; the report distinguishes verified PDF mappings from pages with missing line labels, ambiguous tables, or extraction issues. Source retrieval dates are never used as effective dates.

## Main-dataset Antigravity preparation

The main preparation run reuses the hash-verified pilot PDF index, source audit, and the 21 completed Curtin S1/S6/S13 searches. It adds targeted table-reading-order checks, searches all five universities for all 15 scenarios, and writes 75 case handoffs. It does not create ratings or modify the pilot files, human records, original ZIPs, or existing exports.

Run or resume it in PowerShell:

```powershell
Set-Location 'C:\Users\biran\Desktop\University Coding'
.\.venv\Scripts\python.exe -m policy_coder.main_preparation --root .
```

The versioned handoff folder is `data\preparation\main_dataset_v1\`. Its `progress.txt` and `progress.json` are refreshed after each task; open or refresh them to see completed percentage, failures, pending work, and estimated remaining time. If interrupted, run the same command again. The checkpoint and completed outputs are kept in that folder and are not overwritten.

To display a live PowerShell progress bar in a second terminal while the preparation command runs:

```powershell
$progressPath = 'data\preparation\main_dataset_v1\progress.json'
while (-not (Test-Path $progressPath)) { Start-Sleep -Seconds 2 }
while ($true) {
    $progress = Get-Content $progressPath -Raw | ConvertFrom-Json
    $status = "$($progress.completed)/$($progress.total) tasks; failed $($progress.failed); pending $($progress.pending); ETA seconds $($progress.eta_seconds)"
    Write-Progress -Activity 'Policy evidence preparation' -Status $status -PercentComplete ([int]$progress.finished_percent)
    if ($progress.status -eq 'complete') { break }
    Start-Sleep -Seconds 2
}
Write-Progress -Activity 'Policy evidence preparation' -Completed
```

Use the supplied `additional_search_requests.json` structure to request more local searches, then execute a populated request file with:

```powershell
.\.venv\Scripts\python.exe -m policy_coder.main_preparation --root . --additional-search-requests 'C:\path\to\additional_search_requests.json'
```

Additional results are written to new, request-hash-named CSV/report files in the handoff folder. A request is not described as an executed search until this command succeeds and logs it.

After Antigravity returns one JSON file matching `proposal_return_schema.json`, validate and export it without importing anything into human ratings:

```powershell
.\.venv\Scripts\python.exe -m policy_coder.proposal_validation --root . --proposals 'C:\path\to\antigravity_proposals.json'
```

Successful AI-only exports and the combined validation report go into a new hash-named `validated_ai_proposals_*` folder. Validation errors block text export; outcome previews remain in `validation_report.json`, while each text file leaves the calculated outcome to the workbook formula. The exact instruction to paste into Antigravity is saved in `ANTIGRAVITY_START_HERE.txt`.

## Citation and outcome safeguards

- “PDF viewer page” and printed “Bundle page” are separate fields. Missing printed labels remain blank; viewer-page numbers are never substituted.
- The index records existing visible `L###` labels from PDF page layout. It does not synthesize line numbers. Pages without usable visible labels cannot be selected as line-range evidence.
- Pages with no extractable text and table-like/ambiguous layouts are flagged for review. The page image is shown next to the extracted text.
- Quotations from flagged table pages require a visible check against the original PDF; the coder can correct a scrambled machine-extracted reading order. An index text match alone does not count as verification.
- Marking evidence operative at the policy cut-off requires recorded version evidence (such as the version/date shown on the instrument). Retrieval dates are never accepted as version evidence; otherwise the export labels operative status “Unverified.”
- Retrieval dates remain source-register metadata; effective dates start blank and require human review at the fixed policy cut-off, **12 November 2026**.
- D3 = 0 requires two distinct citations marked operative at cut-off, an explicit incompatibility/unresolved-conflict confirmation, and a written explanation. A missing citation or explanation blocks that outcome.
- Original workbook formulas are not overwritten. Exported `.xlsx` files are new copies. The app checks formula cells before and after mapping user data.
- Search terms and checked documents in text exports come from persisted full-text search and explicit “Record this source page as checked” activity. The export can identify searches that returned zero indexed pages; it does not infer that results were irrelevant. Manually entered score text does not fabricate search history.
- Text exports use the supplied dimension and case-summary headings and order: D1–D6, then the case summary. Each passage repeats its source ID, title, printed Bundle page, and line range. Computed outcome previews are only in `validation_report.json`; the text retains the fixed instruction to leave the calculated outcome to the workbook formula.
- The prompt download and any pasted AI response are stored separately from human ratings. AI text never populates scores or approves human coding.

## Tests

The pytest suite covers safe extraction and hashing, visible-line citation validation, source/university separation, append-only rating revisions, workbook formula preservation, and each outcome-rule branch.
