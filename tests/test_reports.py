import csv
from types import SimpleNamespace

from keep_fixture import FILLER, build
from pipeline import reports
from pipeline.classify.client import FakeClient
from pipeline.classify.rules import apply_keep_rules
from test_classify import FILING, answer, classify, prepare


def summary_for(form, pieces):
    markdown, sections, rows = build(pieces)
    refs = apply_keep_rules(rows, sections, markdown)
    return reports.filing_summary(SimpleNamespace(filing_id="X/1", form=form, year=2001), rows, refs, "HIGH"), rows


def test_filing_summary_counts_kept_tokens_per_item_and_reason():
    summary, rows = summary_for("10-K", [
        ({"section_id": "s1", "item": "7", "label": "KEEP"}, "See Note 2. " + FILLER),
        ({"section_id": "s1", "item": "7"}, FILLER),
        ({"section_id": "s2", "item": "8", "note": "1"}, "Note 1 " + FILLER),
        ({"section_id": "s3", "item": "8", "note": "2"}, "Note 2 " + FILLER),
        ({"section_id": "s4", "item": "9", "label": "UNCERTAIN", "failed": True}, FILLER),
    ])
    tokens = [r["tokens"] for r in rows]
    parts = {p["part"]: p for p in summary["by_part"]}
    assert parts["Item 7"]["tokens_kept"] == tokens[0] + tokens[1] == parts["Item 7"]["tokens_total"]
    assert parts["Item 8"]["tokens_total"] == tokens[2] + tokens[3] and parts["Item 8"]["tokens_kept"] == tokens[3]
    assert parts["Item 9"]["failures"] == 1 and summary["failures"] == 1
    assert summary["tokens_kept_by_reason"] == {"label": tokens[0] + tokens[4], "section": tokens[1],
                                                "reference": tokens[3]}
    assert summary["tokens_kept"] == sum(tokens) - tokens[2]
    assert summary["kept_share"] == round(summary["tokens_kept"] / sum(tokens), 4)
    assert summary["references"]["followed"] == 1 and summary["alerts"] == []


def test_alerts_for_mdna_or_statements_with_nothing_kept_or_not_found():
    summary, _ = summary_for("10-K", [
        ({"section_id": "s1", "item": "7", "label": "KEEP"}, FILLER),
        ({"section_id": "s2", "item": "9"}, FILLER),
    ])
    assert summary["alerts"] == [{"item": "8", "problem": "not found", "ex13_tokens_kept": 0}]
    summary, rows = summary_for("10-K", [
        ({"section_id": "s1", "item": "7"}, FILLER),
        ({"section_id": "s2", "item": "8", "label": "KEEP"}, FILLER),
    ])
    assert summary["alerts"] == [{"item": "7", "problem": "nothing kept", "tokens_total": rows[0]["tokens"],
                                  "ex13_tokens_kept": 0}]
    summary, _ = summary_for("10-KSB", [  # 10-KSB: MD&A is Item 6, statements are Item 7
        ({"section_id": "s1", "item": "6", "label": "KEEP"}, FILLER),
        ({"section_id": "s2", "item": "7", "label": "KEEP"}, FILLER),
    ])
    assert summary["alerts"] == []


def test_corpus_report_is_weighted_and_lists_alerts(small_corpus):
    filings = prepare(small_corpus)
    chosen = [filings[FILING], filings["AAA_Alpha_Inc/1998_b"]]
    classify(small_corpus, chosen, FakeClient(lambda m: answer("KEEP")))
    folder = small_corpus.outputs_dir / "classify" / "fake"
    report = reports.write_filter_report(small_corpus, "fake", log=lambda *_: None)
    per_filing = list(csv.DictReader((folder / "filter_report.csv").open(encoding="utf-8")))
    assert report["filings"] == 2 == len(per_filing)
    total = sum(int(r["tokens_total"]) for r in per_filing)
    assert report["tokens_total"] == total == report["tokens_kept"] and report["kept_share_weighted"] == 1.0
    assert sum(p["tokens_total"] for p in report["by_part"]) == total
    alerts = list(csv.DictReader((folder / "filter_alerts.csv").open(encoding="utf-8")))
    # neither fixture has Item 8; the 2020 one has no Item 7 either
    assert {(a["filing_id"], a["item"], a["problem"]) for a in alerts} >= {(FILING, "8", "not found")}
    assert report["alerts"]["filings"] == len({a["filing_id"] for a in alerts})
    assert (folder / "unresolved_references.csv").is_file()
    assert "not billing" in report["note"]
