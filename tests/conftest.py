import csv
from pathlib import Path

import pytest

from pipeline import config

HTML_10K = '<html><body><p>PART I</p><p>Item 1. Business</p><p>Research costs were $12 million.</p></body></html>'
LEGACY_10K = ('<SEC-DOCUMENT>x\n<SEC-HEADER>Metadata</SEC-HEADER>\n<DOCUMENT>\n<TYPE>10-K\n<FILENAME>a.txt\n<TEXT>\n'
              'ITEM 7. MANAGEMENT\'S DISCUSSION\n\n   Advertising      1998     1997\n   Expense          (12)       34\n'
              '</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n')
HEADER_ONLY = '<SEC-DOCUMENT>id\n<SEC-HEADER>Metadata 1993</SEC-HEADER>\n</SEC-DOCUMENT>'


class TinyTokenizer:
    def encode_ordinary(self, value):
        return value.encode("utf-8")


def build_corpus(root: Path, files: dict[str, dict[str, str]]) -> config.Config:
    """files: {company_dir: {file name: content}}. Writes raw files, manifests and config.toml."""
    for company, entries in files.items():
        folder = root / "raw" / company
        folder.mkdir(parents=True)
        with (folder / "filings.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["company_id", "ticker", "report_year", "form", "file", "status"])
            writer.writeheader()
            for name, content in entries.items():
                (folder / name).write_text(content, encoding="utf-8")
                writer.writerow({"company_id": company.lower(), "ticker": company.split("_")[0],
                                 "report_year": name[:4], "form": "10-K", "file": name, "status": "downloaded"})
    (root / "config.toml").write_text(
        '[paths]\nraw = "raw"\nmarkdown = "md"\noutputs = "outputs"\nruns = "runs"\n', encoding="utf-8")
    return config.load(root / "config.toml")


@pytest.fixture
def small_corpus(tmp_path):
    return build_corpus(tmp_path, {
        "AAA_Alpha_Inc": {"2020_a.htm": HTML_10K, "1998_b.txt": LEGACY_10K},
        "BBB_Beta_Corp": {"1993_c.txt": HEADER_ONLY},
    })
