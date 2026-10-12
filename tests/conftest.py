from contextlib import closing
import csv
from pathlib import Path

import pytest

from pipeline import config, corpus, state

HTML_10K = ('<html><body><p>PART I</p><p>Item 1. Business</p><p>Research costs were $12 million.</p>'
            + ''.join(f'<p>Paragraph {i}: the company sells widgets in region {i} and reports segment results.</p>'
                      for i in range(12))
            + '<p>Amounts in millions</p><table><tr><th>Expense</th><th>2020</th><th>2019</th></tr>'
            '<tr><td>R&amp;D</td><td>12</td><td>(9)</td></tr><tr><td>Advertising</td><td>5</td><td>4</td></tr></table>'
            '<table><tr><td rowspan="2">Licenses</td><td colspan="2">Years</td></tr><tr><td>3</td><td>4</td></tr></table>'
            '</body></html>')
LEGACY_10K = ('<SEC-DOCUMENT>x\n<SEC-HEADER>Metadata</SEC-HEADER>\n<DOCUMENT>\n<TYPE>10-K\n<FILENAME>a.txt\n<TEXT>\n'
              'ITEM 7. MANAGEMENT\'S DISCUSSION\n\n'
              + ''.join(f'   Line {i} of fixed-width discussion about operations in plant {i}.\n' for i in range(25))
              + '\n<TABLE>\n   Advertising      1998     1997\n   Expense          (12)       34\n</TABLE>\n'
              '</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n')
HEADER_ONLY = '<SEC-DOCUMENT>id\n<SEC-HEADER>Metadata 1993</SEC-HEADER>\n</SEC-DOCUMENT>'


class TinyTokenizer:
    def encode_ordinary(self, value):
        return value.encode("utf-8")


MANIFEST_FIELDS = ["company_id", "ticker", "cik", "report_year", "report_date", "filing_date", "form",
                   "accession", "file", "sha256", "status"]


def write_company(raw: Path, company: str, entries: dict[str, str], rows: dict[str, dict] | None = None) -> None:
    """Write one company folder like the downloader does. rows: per-file manifest overrides."""
    folder = raw / company
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "filings.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for name, content in entries.items():
            if content is not None:
                (folder / name).write_text(content, encoding="utf-8")
            year = name[:4] if name[:4].isdigit() else "2000"
            row = {"company_id": company.lower(), "ticker": company.split("_")[0], "cik": "0000000001",
                   "report_year": year, "report_date": f"{year}-12-31", "filing_date": f"{int(year) + 1}-03-01",
                   "form": "10-K", "accession": f"acc-{company}-{name}", "file": name, "sha256": "", "status": "downloaded"}
            row.update((rows or {}).get(name, {}))
            writer.writerow(row)


def registered(cfg) -> dict:
    """Scan the fixture corpus and return {filing_id: Filing}."""
    with closing(state.connect(cfg.db_path)) as db:
        corpus.scan(cfg, db, log=lambda *_: None)
        return {f.filing_id: f for f in corpus.select(cfg, db)}


def build_corpus(root: Path, files: dict[str, dict[str, str]]) -> config.Config:
    """files: {company_dir: {file name: content}}. Writes raw files, manifests and config.toml."""
    for company, entries in files.items():
        write_company(root / "raw", company, entries)
    project = config.ROOT.as_posix()
    (root / "config.toml").write_text(
        '[paths]\nraw = "raw"\nmarkdown = "md"\noutputs = "outputs"\nruns = "runs"\n'
        f'[tokens]\ncache_dir = "{project}/.cache/tiktoken"\n'
        '[segment]\nchunk_max_tokens = 60\n'
        f'[classify]\nbackend = "fake"\nparallel_requests = 3\nprompts_dir = "{project}/prompts"\n',
        encoding="utf-8")
    return config.load(root / "config.toml")


@pytest.fixture
def small_corpus(tmp_path):
    return build_corpus(tmp_path, {
        "AAA_Alpha_Inc": {"2020_a.htm": HTML_10K, "1998_b.txt": LEGACY_10K},
        "BBB_Beta_Corp": {"1993_c.txt": HEADER_ONLY},
    })
