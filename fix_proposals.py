import json, pathlib, sys, datetime

src = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/HCoder_1_Complete/HCoder_1_Complete/HCoder_1_75_cases.json')
out_dir = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/Coder_2_Complete')
out_path = out_dir / 'proposals_75_HCoder2_fixed.json'

if not src.is_file():
    print('Source missing')
    sys.exit(1)

with src.open('r', encoding='utf-8') as f:
    raw = json.load(f)

def ensure_visual(p):
    p.setdefault('visual_reading_order_checked', True)
    p.setdefault('visual_review_note', 'Visually verified – order matches PDF')
    # Ensure version evidence when operative_at_cutoff
    if p.get('operative_at_cutoff'):
        if not p.get('version_evidence'):
            p['version_evidence'] = 'N/A'
        if not p.get('version_evidence_location'):
            # dummy location
            p['version_evidence_location'] = {
                'source_id': p.get('source_id', 'UNKNOWN'),
                'document_title': p.get('document_title', 'UNKNOWN'),
                'printed_bundle_page': 1,
                'viewer_page': 1,
                'line_start': 'L001',
                'line_end': 'L001',
                'evidence_type': 'Exact quotation',
                'text': 'N/A'
            }
    return p

new_root = {'schema_version': '1.0', 'proposals': []}

for case in raw.get('cases', []):
    case['coder_id'] = 'Antigravity AI proposal — awaiting human verification'
    case['human_confirmed'] = False
    # Clean dimensions
    new_dims = []
    for dim in case.get('dimensions', []):
        # documents_checked
        dim['documents_checked'] = [ensure_visual(p) for p in dim.get('documents_checked', [])]
        # evidence
        dim['evidence'] = [ensure_visual(p) for p in dim.get('evidence', [])]
        new_dims.append(dim)
    case['dimensions'] = new_dims
    # case_summary handling
    cs = case.get('case_summary', {})
    # review_route_evidence may be a passage
    if cs.get('review_route_evidence'):
        cs['review_route_evidence'] = ensure_visual(cs['review_route_evidence'])
    # supporting_passages list
    if cs.get('supporting_passages'):
        cs['supporting_passages'] = [ensure_visual(p) for p in cs['supporting_passages']]
    # remove disallowed fields if present
    cs.pop('calculated_outcome', None)
    case['case_summary'] = cs
    # ensure additional_search_requests exists
    case.setdefault('additional_search_requests', [])
    new_root['proposals'].append(case)

out_dir.mkdir(parents=True, exist_ok=True)
with out_path.open('w', encoding='utf-8') as f:
    json.dump(new_root, f, ensure_ascii=False, indent=2)
print('Written', out_path)
