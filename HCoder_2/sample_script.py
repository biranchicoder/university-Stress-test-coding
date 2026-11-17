import json, random, pathlib, csv

# Configuration ---------------------------------------------------
JSON_PATH = pathlib.Path("Coder_2_Complete/proposals_75_HCoder2_final.json")
OUTPUT_JSON = pathlib.Path("validation_sample.json")
OUTPUT_XLSX = pathlib.Path("validation_checklist.xlsx")
SAMPLE_FRACTION = 0.25  # 25% of passages (~750 of 2979)
# ---------------------------------------------------------------


def flatten_passages(data):
    """Yield (case_id, path, passage_dict) for every passage that has visual flags."""
    def walk(node, case_id=None, path=""):
        if isinstance(node, dict):
            if node.get("visual_reading_order_checked") is not None:
                yield case_id, path, node
            for k, v in node.items():
                new_path = f"{path}.{k}" if path else k
                yield from walk(v, case_id, new_path)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                new_path = f"{path}[{i}]"
                yield from walk(v, case_id, new_path)
    for proposal in data.get("proposals", []):
        cid = proposal.get("case_id")
        yield from walk(proposal, cid)


def main():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    all_passages = list(flatten_passages(data))
    sample_size = max(1, int(len(all_passages) * SAMPLE_FRACTION))
    sample = random.sample(all_passages, sample_size)

    # Write JSON sample
    sample_json = [{
        "case_id": cid,
        "path": path,
        "passage": passage
    } for cid, path, passage in sample]
    OUTPUT_JSON.write_text(json.dumps(sample_json, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(sample_json)} sampled passages to {OUTPUT_JSON}")

    # Write XLSX (CSV fallback – Excel will open CSV fine)
    csv_path = OUTPUT_XLSX.with_suffix('.csv')
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["case_id", "json_path", "page", "line_start", "visual_reading_order_checked", "visual_review_note"])
        for cid, path, passage in sample:
            writer.writerow([
                cid,
                path,
                passage.get('page'),
                passage.get('line_start'),
                passage.get('visual_reading_order_checked'),
                passage.get('visual_review_note')
            ])
    print(f"Wrote CSV checklist to {csv_path} (rename to .xlsx if needed)")

if __name__ == "__main__":
    main()
