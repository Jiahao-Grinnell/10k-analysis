from contextlib import closing
import csv
import json

from pipeline import cli, config, publish, state
from pipeline.classify import stage as classify_stage
from pipeline.classify.client import FakeClient
from pipeline.compact import rules as R, stage as compact_stage
from pipeline.compact.validate import visible_tokens
from pipeline.segment import stage as segment_stage
from test_classify import answer, classify, prepare
from test_compact import FILING, corpus_with_rules  # noqa: F401  (fixture)


def run_all(cfg, *extra):
    assert cli.main(["--config", str(cfg.path), "all", "--fake-model", *extra]) == 0


def index(cfg):
    path = cfg.outputs_dir / "clean" / "fake" / "clean_index.csv"
    return {r["filing_id"]: r for r in csv.DictReader(path.open(encoding="utf-8"))}


def test_clean_file_has_a_header_and_every_part_maps_back_to_the_markdown(corpus_with_rules):
    run_all(corpus_with_rules)
    filing = prepare(corpus_with_rules)[FILING]
    out = publish.output_paths(corpus_with_rules, "fake", filing)
    clean = out["clean"].read_bytes().decode("utf-8")
    head = clean.split("---\n")[1]
    fields = dict(line.split(": ", 1) for line in head.strip().splitlines())
    for key in ("filing_id", "company", "cik", "ticker", "form", "fiscal_year", "period", "filing_date", "accession",
                "is_amendment", "amends", "source", "conversion_quality", "kept_sections", "tokens_clean", "compaction"):
        assert key in fields
    assert json.loads(fields["company"]) == "Gamma Inc" and json.loads(fields["compaction"]) == "applied"
    assert "note" not in fields
    markdown = segment_stage.read_markdown(filing.markdown_path)
    mapping = json.loads(out["map"].read_text(encoding="utf-8"))
    assert mapping["parts"] and mapping["header_chars"] == len(clean) - len(
        compact_stage.output_paths(corpus_with_rules, "fake", filing)["body"].read_bytes().decode("utf-8"))
    for part in mapping["parts"]:
        piece = clean[part["clean_start"]:part["clean_end"]]
        original = markdown[part["markdown_start"]:part["markdown_end"]]
        assert visible_tokens(piece) == visible_tokens(original)
        assert part["chunks"] and all(c["kept_reason"] for c in part["chunks"])
    row = index(corpus_with_rules)[FILING]
    assert row["status"] == "ready" and row["clean_path"].endswith(".clean.md") and int(row["tokens_clean"]) > 0


def test_nothing_kept_is_said_in_the_header_and_the_index(small_corpus):
    filing = prepare(small_corpus)["AAA_Alpha_Inc/1998_b"]
    classify(small_corpus, [filing], FakeClient(lambda m: answer("DROP")))
    with closing(state.connect(small_corpus.db_path)) as db:
        compact_stage.run(small_corpus, db, [filing], "fake", log=lambda *_: None)
        assert publish.run(small_corpus, db, [filing], "fake", log=lambda *_: None)["counts"] == {"nothing_kept": 1}
    clean = publish.output_paths(small_corpus, "fake", filing)["clean"].read_text(encoding="utf-8")
    assert f'note: "{publish.NOTHING_KEPT}"' in clean and "kept_sections: 0" in clean
    assert clean.endswith("---\n\n")  # header only
    rows = index(small_corpus)
    assert rows["AAA_Alpha_Inc/1998_b"]["status"] == "nothing_kept"
    assert rows["BBB_Beta_Corp/1993_c"]["status"] == "source_incomplete"
    assert rows["AAA_Alpha_Inc/2020_a"]["status"] == "pending"  # registered, not processed yet


def test_compaction_rule_change_reruns_compact_and_publish_only(corpus_with_rules, capsys):
    run_all(corpus_with_rules)
    rules = [r for r in R.RULES if r != "table_to_pipe"]
    corpus_with_rules.path.write_text(corpus_with_rules.path.read_text(encoding="utf-8").replace(
        json.dumps(R.RULES), json.dumps(rules)), encoding="utf-8")
    cfg = config.load(corpus_with_rules.path)
    capsys.readouterr()
    run_all(cfg)
    printed = capsys.readouterr().out
    assert "segment: {'already_done': 1}" in printed
    assert "classify [fake]: {'already_done': 1}" in printed
    assert "compact [fake]: {'applied': 1}" in printed and "publish [fake]: {'ready': 1}" in printed
    body = compact_stage.output_paths(cfg, "fake", prepare(cfg)[FILING])["body"].read_text(encoding="utf-8")
    assert "<table>" in body and "|---|" not in body  # tables stay HTML without the pipe rule
    capsys.readouterr()
    run_all(cfg)
    printed = capsys.readouterr().out
    assert "compact [fake]: {'already_done': 1}" in printed and "publish [fake]: {'already_done': 1}" in printed


def test_summary_reports_weighted_savings_at_each_step(corpus_with_rules):
    run_all(corpus_with_rules)
    summary = json.loads((corpus_with_rules.outputs_dir / "clean" / "fake" / "clean_summary.json").read_text(encoding="utf-8"))
    row = index(corpus_with_rules)[FILING]
    raw, md, kept, clean = (int(row[k]) for k in ("tokens_raw", "tokens_markdown", "tokens_kept", "tokens_clean"))
    tokens = summary["all"]["tokens"]
    assert (tokens["raw"], tokens["markdown"], tokens["kept"], tokens["clean"]) == (raw, md, kept, clean)
    shares = summary["all"]["saved_share"]
    assert shares["markdown_to_clean"] == round((md - clean) / md, 4)
    assert shares["raw_to_clean"] == round((raw - clean) / raw, 4)
    assert shares["kept_to_clean"] == round((tokens["kept_text_with_headings"] - clean) / tokens["kept_text_with_headings"], 4)
    assert summary["all"]["compaction"] == {"applied": 1} and "not billing" in summary["note"]
