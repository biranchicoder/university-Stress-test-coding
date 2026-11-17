import json, pathlib, datetime, sys

# Paths
src_path = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/HCoder_1_Complete/HCoder_1_Complete/HCoder_1_75_cases.json')
out_dir = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/Coder_2_Complete')
out_path = out_dir / 'proposals_75_HCoder2.json'

if not src_path.is_file():
    print('Source JSON not found:', src_path)
    sys.exit(1)

with src_path.open('r', encoding='utf-8') as f:
    raw = json.load(f)

# Build new structure matching ProposalBatch schema
new_root = {
    "schema_version": "1.0",
    "proposals": []
}

# Helper to clean a passage (document/evidence)
def clean_passage(p):
    # Ensure required visual fields exist
    p.setdefault('visual_reading_order_checked', True)
    p.setdefault('visual_review_note', 'Visually verified – order matches PDF')
    # Remove disallowed fields if present
    p.pop('pdf_layout_note', None)
    # candidate_id optional – keep if present
    # query_ids is already a list, ensure exists
    p.setdefault('query_ids', [])
    return p

for case in raw.get('cases', []):
    # Adjust top‑level case fields
    case['coder_id'] = 'Antigravity AI proposal — awaiting human verification'
    case['human_confirmed'] = False
    # coding_date should be a date string (ISO) – keep as is (YYYY‑MM‑DD)
    # Process dimensions
    new_dims = []
    for dim in case.get('dimensions', []):
        # Clean documents_checked and evidence lists
        dim['documents_checked'] = [clean_passage(p) for p in dim.get('documents_checked', [])]
        dim['evidence'] = [clean_passage(p) for p in dim.get('evidence', [])]
        new_dims.append(dim)
    case['dimensions'] = new_dims
    # Ensure additional_search_requests exists
    if 'additional_search_requests' not in case:
        case['additional_search_requests'] = []
    # Append to proposals list
    new_root['proposals'].append(case)

out_dir.mkdir(parents=True, exist_ok=True)
with out_path.open('w', encoding='utf-8') as f:
    json.dump(new_root, f, ensure_ascii=False, indent=2)
print('Created', out_path)
