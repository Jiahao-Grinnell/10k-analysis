"""Experiment: merge every converted 10-K of a company into one Markdown file.

Reads Markdown_10K/ (never modified) and the filing registry in runs/pipeline.sqlite.
Writes Merged_10K/<company>.md, one file per company, filings in fiscal-year order,
each preceded by a clear boundary header. Also writes Merged_10K/merged_index.csv.

Usage:
  & '.\\.venv\\Scripts\\python.exe' experiments\\merge_company_markdown.py [--company AAPL] [--force]
"""
import argparse
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN = ROOT / "Markdown_10K"
OUT = ROOT / "Merged_10K"
DB = ROOT / "runs" / "pipeline.sqlite"


def boundary(row, n, total):
    return (
        f"\n\n<!-- ===================== FILING {n} OF {total} ===================== -->\n"
        f"# FILING {n} of {total}: fiscal year {row['year']} | form {row['form']} | "
        f"period end {row['period'] or 'unknown'} | filed {row['filing_date']} | "
        f"accession {row['accession']}\n\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", help="ticker or folder prefix, e.g. AAPL")
    parser.add_argument("--force", action="store_true", help="rewrite files that already exist")
    args = parser.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM filings WHERE markdown_path IS NOT NULL ORDER BY company_dir, year, filing_date"
    ).fetchall()
    companies = {}
    for row in rows:
        companies.setdefault(row["company_dir"], []).append(row)

    OUT.mkdir(exist_ok=True)
    index = []
    for company, filings in companies.items():
        if args.company and not company.startswith(args.company + "_"):
            continue
        target = OUT / f"{company}.md"
        tokens = sum(f["markdown_tokens"] or 0 for f in filings)
        index.append({
            "company": company, "filings": len(filings),
            "first_year": filings[0]["year"], "last_year": filings[-1]["year"],
            "markdown_tokens_o200k": tokens, "file": target.name,
        })
        if target.exists() and not args.force:
            continue
        tmp = target.with_suffix(".md.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as out:
            out.write(f"# {company}: all converted 10-K filings ({len(filings)} filings, "
                      f"fiscal years {filings[0]['year']}-{filings[-1]['year']})\n")
            for n, f in enumerate(filings, 1):
                out.write(boundary(f, n, len(filings)))
                out.write((MARKDOWN / f["markdown_path"]).read_bytes().decode("utf-8"))
            out.write(f"\n\n<!-- ===================== END OF ALL {len(filings)} FILINGS ===================== -->\n")
        tmp.replace(target)
        print(f"{company}: {len(filings)} filings, {tokens:,} tokens")

    if not args.company:
        with (OUT / "merged_index.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(index[0]))
            writer.writeheader()
            writer.writerows(index)


if __name__ == "__main__":
    main()
