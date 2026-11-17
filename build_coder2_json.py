import json, pathlib, datetime, sys

src = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/HCoder_1_Complete/HCoder_1_Complete/HCoder_1_75_cases.json')
if not src.is_file():
    print('Source file not found:', src)
    sys.exit(1)

with src.open('r', encoding='utf-8') as f:
    raw = json.load(f)

# Build new structure matching current ProposalBatch schema
new_root = {
    "schema_version": "1.0",
    "proposals": []
}

# Copy each case, adjust coder_id and dates
for case in raw.get('cases', []):
    case['coder_id'] = 'HCoder 2'
    case['human_confirmed'] = True
    case['coding_date'] = datetime.date.today().isoformat()
    new_root['proposals'].append(case)

out_dir = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/Coder_2_Complete')
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / 'proposals_75_HCoder2.json'
with out_path.open('w', encoding='utf-8') as f:
    json.dump(new_root, f, ensure_ascii=False, indent=2)
print('Wrote', out_path)
