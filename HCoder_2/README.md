# Second-Coder Validation & Consensus Package (`HCoder_2`)

This folder contains the finalized second-coder human validation and consensus records for the Five-University AI Policy Stress Test (75 cases, 450 dimension records: 446 numeric scores and 4 unscored judgments in `Murdoch-S5`).

## Contents

| File | Description |
|------|-------------|
| `proposals_75_HCoder2_final.json` | Final human-validated 75-case JSON dataset (`coder_id = "HCoder 2"`, `human_confirmed = true`). |
| `Coder2_25pct_SpotCheck_5unis_113cells.csv` | 25% stratified spot-check table across the five study universities (113 unique dimension records) recording final reconciled consensus between `HCoder 1` and `HCoder 2`. |
| `SECONDARY_VALIDATION.md` | Completed and signed-off validation and consensus log. |
| `SECONDARY_VALIDATION_TEMPLATE.md` | Blank validation log template. |
| `sample_script.py` | Helper script used for stratified/random sampling of records. |

## Summary of Final Outcomes & Consensus

- **Total cases:** 75 across 5 universities (Curtin, ECU, Murdoch, Notre Dame, UWA) × 15 scenarios (`S1`–`S15`).
- **Total dimension records:** 450 (446 numeric scores and 4 unscored judgments within `Murdoch-S5`).
- **Outcome distribution:** **3 Resolved · 5 Structured discretion · 66 Indeterminate · 1 Unassessable (`Murdoch-S5`)**.
- **Consensus status:** 100% final consensus across all 450 dimension records and all 113 spot-checked cells after reconciliation between primary (`HCoder 1`) and secondary (`HCoder 2`) human coders. Because the saved dataset reflects reconciled consensus rather than separate archived pre-reconciliation ratings, this is reported as **final consensus** rather than independent pre-reconciliation inter-coder agreement.
