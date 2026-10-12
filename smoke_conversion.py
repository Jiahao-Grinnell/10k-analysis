"""Reproducible integration checks against representative local filing formats."""
import json
import os
from pathlib import Path

import tiktoken

from convertmd import ROOT, conversion_job, discover, write_summary


SAMPLES = [
    ('MSFT_', '2026_'),       # Modern inline XBRL
    ('AAPL_', '2025_'),       # Modern inline XBRL from another renderer
    ('AAPL_', '2018_'),       # HTML in a SEC DOCUMENT/TEXT wrapper
    ('AAPL_', '2000_'),       # Primary fixed-width TXT
    ('AAPL_', '1999_'),       # Complete submission, text exhibits
    ('ADBE_', '1999_'),       # HTML fragment/bare HTML
    ('LLY_', '2000_'),        # Complete submission, HTML exhibits
    ('DHR_', '1993_'),        # SEC header-only incomplete download
    ('O_', '2017_'),          # Largest original file
]


def main():
    os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(ROOT / '.cache/tiktoken'))
    encodings = {'o200k_base': tiktoken.get_encoding('o200k_base')}
    jobs, _ = discover((ROOT / '10K_Filings').resolve())
    output = ROOT / 'test_outputs/corpus_smoke'
    reports = []
    for company_prefix, year_prefix in SAMPLES:
        matches = [(source, relative, metadata) for source, relative, metadata in jobs
                   if source.parent.name.startswith(company_prefix) and source.name.startswith(year_prefix)]
        if len(matches) != 1:
            raise RuntimeError(f'Expected one smoke sample: {company_prefix} {year_prefix}, got {len(matches)}')
        source, relative, metadata = matches[0]
        report = conversion_job(source, output / relative, encodings, metadata)
        report['source'] = str(source.relative_to(ROOT / '10K_Filings'))
        report['markdown'] = str(relative)
        reports.append(report)
        print(f"{source.parent.name} {source.name}: {report['quality_status']}", flush=True)
    summary = write_summary(output, reports, list(encodings))
    print(json.dumps(summary, indent=2), flush=True)
    if summary['missing_numeric_occurrences'] or summary['tables_failing_preservation'] or summary['documents_with_text_mismatch']:
        raise SystemExit('Integration preservation failures: inspect audit records')


if __name__ == '__main__':
    main()
