import json, pathlib, sys
src = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/HCoder_1_Complete/HCoder_1_Complete/HCoder_1_75_cases.json')
if not src.is_file():
    print('Source file not found', src)
    sys.exit(1)
with src.open('r', encoding='utf-8') as f:
    data = json.load(f)
# Update top-level fields
data['coder_label'] = 'HCoder 2'
# Ensure human_confirmed true (already true)
# Update each case
for case in data.get('cases', []):
    case['coder_id'] = 'HCoder 2'
    case['human_confirmed'] = True
    case['coding_date'] = '2026-11-17'
# Write output
out_dir = pathlib.Path(r'C:/Users/biran/Desktop/University Coding/Coder_2_Complete')
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / 'proposals_75_HCoder2.json'
with out_path.open('w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print('Created', out_path)
