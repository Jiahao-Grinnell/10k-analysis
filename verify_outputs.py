"""Read-only verification of a completed conversion against the raw corpus."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from convertmd import CODE_HASH, COMPATIBLE_CHECKED_HASHES, ROOT, atomic_text, digest, discover


def verify(input_path: Path, output: Path) -> dict:
    jobs, _ = discover(input_path)
    summary = json.loads((output / "conversion_summary.json").read_text(encoding="utf-8"))
    reports, problems = [], []
    statuses, image_statuses, warnings = Counter(), Counter(), Counter()
    expected_markdown = {relative for _, relative, _ in jobs}
    for source, relative, metadata in jobs:
        destination = output / relative
        audit_path = destination.with_suffix(".audit.json")
        if not destination.is_file() or not audit_path.is_file():
            problems.append({"source": str(source), "problem": "Missing Markdown or audit record"})
            continue
        report = json.loads(audit_path.read_text(encoding="utf-8"))
        reports.append(report)
        raw_hash = digest(source.read_bytes())
        checks = {
            "Raw source hash matches audit": raw_hash == report["source_sha256"],
            "Raw source hash matches download manifest": not metadata.get("sha256") or raw_hash == metadata["sha256"],
            "Markdown hash matches audit": digest(destination.read_bytes()) == report["markdown_sha256"],
            # Outputs from a compatible earlier converter stay valid; if they were reused under
            # resume, the version that verified them must itself be current or compatible.
            "Converter code hash matches audit": (report["converter_sha256"] == CODE_HASH
                or report["converter_sha256"] in COMPATIBLE_CHECKED_HASHES
                and (not report.get("compatible_converter_reuse")
                     or report.get("verified_by_converter_sha256") in COMPATIBLE_CHECKED_HASHES | {CODE_HASH})),
        }
        for name, passed in checks.items():
            if not passed:
                problems.append({"source": str(source), "problem": name})
        statuses[report["quality_status"]] += 1
        warnings.update(report.get("warnings", []))
        for doc in report.get("documents", []):
            image_statuses.update(image["status"] for image in doc["images"])
            warnings.update(doc["warnings"])
            if not doc["visible_text_equal_ignoring_whitespace"] or doc["missing_numeric_occurrences"] or doc["tables_failing_preservation"]:
                problems.append({"source": str(source), "document": doc["filename"], "problem": "Document preservation check failed",
                                 "text_equal": doc["visible_text_equal_ignoring_whitespace"],
                                 "missing_numeric_occurrences": doc["missing_numeric_occurrences"],
                                 "bad_tables": doc["tables_failing_preservation"]})
    actual_markdown = {path.relative_to(output) for path in output.rglob("*.md") if path.parent != output}
    # A single-file conversion may have its Markdown directly in the output root.
    actual_markdown.update(path for path in expected_markdown if (output / path).is_file())
    extra = sorted(str(path) for path in actual_markdown - expected_markdown)
    if extra:
        problems.append({"problem": "Unexpected filing Markdown files", "paths": extra})
    if len(reports) != summary["source_files"] or dict(statuses) != summary["quality_status_counts"]:
        problems.append({"problem": "Summary source/status totals differ from audit files"})
    for name, totals in summary["tokens"].items():
        valid = [r for r in reports if r["quality_status"] != "SOURCE_INCOMPLETE"]
        for key in ("raw_source", "included_documents_raw", "markdown"):
            observed = sum(r["tokens"][name][key] for r in valid)
            if observed != totals[key]:
                problems.append({"problem": "Token total differs from audit records", "encoding": name, "field": key})
    return {
        "expected_filings": len(jobs), "audits_verified": len(reports),
        "filing_markdown_files": len(actual_markdown), "quality_status_counts": dict(statuses),
        "source_and_output_hash_checks_passed": not any("hash" in p["problem"] for p in problems),
        "images_by_status": dict(image_statuses),
        "most_common_warnings": [{"warning": warning, "count": count} for warning, count in warnings.most_common(12)],
        "incomplete_sources": [r["source"] for r in reports if r["quality_status"] == "SOURCE_INCOMPLETE"],
        "problems": problems, "passed": not problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "10K_Filings")
    parser.add_argument("--output", type=Path, default=ROOT / "Markdown_10K")
    args = parser.parse_args()
    result = verify(args.input.resolve(), args.output.resolve())
    atomic_text(args.output / "corpus_validation.json", json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
