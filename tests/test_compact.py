from collections import Counter
from contextlib import closing
import json

import pytest

from pipeline import config, state
from pipeline.classify import stage as classify_stage
from pipeline.classify.client import FakeClient
from pipeline.compact import rules as R, stage as compact_stage, tables as T
from pipeline.compact.validate import check, compare_tables, visible_tokens
from pipeline.segment.blocks import Block
from conftest import build_corpus
from test_classify import answer, classify, prepare

SEC_TABLE = ('<table><tr><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>'
             '<tr><td colspan="3"></td><td colspan="2">2021</td><td></td><td colspan="2">2020</td></tr>'
             '<tr><td colspan="3">Research and development</td><td>$</td><td>12</td><td></td><td>$</td><td>(9</td></tr>'
             '<tr><td colspan="3">Advertising</td><td colspan="2">5</td><td></td><td colspan="2">4</td></tr></table>')
SEC_10K = ('<html><body><p>PART I</p><p>Item 1. Business</p><p>We sell widgets in many regions of the world.</p>'
           '<p>PART II</p><p>Item 8. Financial Statements</p><p>Amounts in millions</p>' + SEC_TABLE + '</body></html>')
FILING = "CCC_Gamma_Inc/2021_d"

ALL = set(R.RULES)


def table(source, rules=ALL):
    counts = Counter()
    out = R.compact_table(source, set(rules), counts)
    return out, counts


def assert_same(before, after):
    assert check(before, after) == []


# ---- one test per rule, with the cases each must leave alone ---------------------------------

def test_html_minify_drops_layout_whitespace_and_attributes():
    source = '<table border="1">\n <tr style="x"> <td class="a">Revenue</td> <td>12</td></tr></table>'
    out, counts = table(source, {"html_minify"})
    assert out == "<table><tr><td>Revenue</td><td>12</td></tr></table>" and counts["html_minify"] == 1
    assert_same(source, out)


def test_cell_whitespace_collapses_runs_inside_cells():
    out, _ = table("<table><tr><td>  Research \n and   development </td><td> 5 </td></tr></table>", {"cell_whitespace"})
    assert out == "<table><tr><td>Research and development</td><td>5</td></tr></table>"


def test_remove_empty_wrappers_keeps_wrappers_with_text():
    out, counts = table("<table><tr><td><p></p><p>Total</p></td><td><span> </span>7</td></tr></table>",
                        {"remove_empty_wrappers"})
    assert out == "<table><tr><td><p>Total</p></td><td> 7</td></tr></table>" and counts["remove_empty_wrappers"] == 2


def test_unwrap_single_wrapper_but_keep_several_paragraphs():
    out, _ = table("<table><tr><td><p>Balance at year end</p></td>"
                   "<td><div>First paragraph.</div><div>Second paragraph.</div></td></tr></table>",
                   {"unwrap_cell_wrappers", "table_to_pipe"})
    assert "<td>Balance at year end</td>" in out
    assert "<div>First paragraph.</div><div>Second paragraph.</div>" in out
    assert out.startswith("<table>")  # a cell with several paragraphs never becomes a pipe table


def test_merge_symbol_cells_joins_dollar_percent_and_parenthesis():
    source = ("<table><tr><td>R&amp;D</td><td>$</td><td>34,550</td><td>10</td><td>%</td>"
              "<td>(9,834</td><td>)</td><td>$</td><td>Total</td></tr></table>")
    out, counts = table(source, {"merge_symbol_cells"})
    cells = [c.text for c in T.parse_html(out)[0][0]]
    assert cells == ["R&D", "$34,550", "10%", "(9,834)", "$", "Total"]  # "$" before text is not merged
    assert counts["merge_symbol_cells"] == 3
    assert_same(source, out)


def test_drop_empty_rows_and_columns_keep_numbers_under_their_headings():
    source = ('<table><tr><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>'
              '<tr><td colspan="3"></td><td colspan="2">2025</td><td></td><td>2024</td></tr>'
              '<tr><td colspan="3">Research and development</td><td>$</td><td>34,550 </td><td></td><td>31,370</td></tr>'
              '<tr><td colspan="7"></td></tr>'
              '<tr><td colspan="3">Total</td><td colspan="2">62,151</td><td></td><td>57,467</td></tr></table>')
    out, counts = table(source)
    assert out == ("||2025|2024|\n|---|---|---|\n|Research and development|$34,550|31,370|\n|Total|62,151|57,467|")
    assert counts["drop_empty_rows"] == 2 and counts["drop_empty_columns"] >= 3
    assert_same(source, out)


def test_row_spanning_tables_keep_their_layout():
    source = ('<table><tr><td rowspan="2">Stock</td><td>Shares</td><td></td></tr>'
              '<tr><td></td><td></td></tr><tr><td>Balance</td><td>$</td><td>10</td></tr></table>')
    out, counts = table(source)
    assert counts["table_rowspan_kept"] == 1 and 'rowspan="2"' in out
    assert len(T.parse_html(out)[0]) == 3  # no row removed under the row span
    assert_same(source, out)


def test_table_to_pipe_only_when_the_grid_is_exact():
    out, _ = table("<table><tr><th>Year</th><th>Amount</th></tr><tr><td>2025</td><td>a|b</td></tr></table>",
                   {"table_to_pipe"})
    assert out == "|Year|Amount|\n|---|---|\n|2025|a\\|b|"
    kept, _ = table('<table><tr><td colspan="2">Heading over both</td></tr><tr><td>1</td><td>2</td></tr></table>',
                    {"table_to_pipe"})
    assert kept.startswith("<table>") and 'colspan="2"' in kept


def test_empty_table_is_removed_and_nested_tables_are_left_alone():
    assert table("<table><tr><td></td><td> </td></tr></table>")[0] == ""
    nested = "<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>"
    out, counts = table(nested)
    assert out is None and counts["table_skipped: nested or several tables"] == 1


def test_html_whitespace_and_tables_inside_a_large_html_block():
    source = ('<div>  <p>Item 8.   Financial Statements</p>\n  <table> <tr> <td>Revenue</td> <td></td> <td>12</td></tr>'
              '</table>  <pre>keep   this   spacing</pre></div>')
    block = Block(0, len(source), "html")
    untouched = R.compact_block(source, block, ALL, Counter())
    assert untouched == source  # a block with <pre> is never touched
    source = source.replace("<pre>keep   this   spacing</pre>", "")
    out = R.compact_block(source, Block(0, len(source), "html"), ALL, Counter())
    assert "<p>Item 8. Financial Statements</p>" in out and "<table><tr><td>Revenue</td><td>12</td></tr></table>" in out
    assert_same(source, out)


def test_prose_whitespace_never_touches_fixed_width_or_code():
    prose = "Research   costs rose.  \nSee   below."
    assert R.compact_block(prose, Block(0, len(prose), "paragraph"), ALL, Counter()) == "Research costs rose.\nSee below."
    code = "Use `a   b` as is."
    assert R.compact_block(code, Block(0, len(code), "paragraph"), ALL, Counter()) == code
    fenced = "```text\n   Advertising      1998     1997\n```"
    assert R.compact_block(fenced, Block(0, len(fenced), "fenced"), ALL, Counter()) == fenced


# ---- validation ----------------------------------------------------------------------------

def test_validation_catches_changed_numbers_signs_spacing_and_alignment():
    before = "Costs were 12 34 in total.\n\n|A|B|\n|---|---|\n|x|(5)|\n\n```text\n  a    b\n```\n"
    assert check(before, before.replace("12 34", "1234"))
    assert check(before, before.replace("(5)", "5"))
    assert any("fixed-width" in p for p in check(before, before.replace("  a    b", "  a b")))
    header = '<table><tr><td></td><td>2025</td><td>2024</td></tr><tr><td>R&amp;D</td><td>5</td><td>4</td></tr></table>'
    shifted = '<table><tr><td></td><td>2025</td><td>2024</td></tr><tr><td>R&amp;D</td><td></td><td>5</td><td>4</td></tr></table>'
    assert compare_tables(T.parse_html(header)[0], T.parse_html(shifted)[0]) == "column alignment differs"
    assert visible_tokens("$ 34,550") == visible_tokens("$34,550") == ["$", "34,550"]


@pytest.fixture
def corpus_with_rules(tmp_path):
    """One SEC-style filing (spacer cells, '$' cells, spans) and every compaction rule switched on."""
    cfg = build_corpus(tmp_path, {"CCC_Gamma_Inc": {"2021_d.htm": SEC_10K}})
    cfg.path.write_text(cfg.path.read_text(encoding="utf-8") + '[compact]\nrules = ' + json.dumps(R.RULES) + "\n",
                        encoding="utf-8")
    return config.load(cfg.path)


def compact(cfg, filings, **kwargs):
    with closing(state.connect(cfg.db_path)) as db:
        return compact_stage.run(cfg, db, filings, "fake", log=lambda *_: None, **kwargs)


def test_failed_check_falls_back_to_the_uncompacted_text(corpus_with_rules, monkeypatch):
    filing = prepare(corpus_with_rules)[FILING]
    classify(corpus_with_rules, [filing], FakeClient(lambda m: answer("KEEP")))
    original = R.compact_block
    broken = lambda text, block, rules, counts: original(text, block, rules, counts).replace("12", "13") \
        if rules else original(text, block, rules, counts)  # a rule that changes a number
    monkeypatch.setattr(R, "compact_block", broken)
    assert compact(corpus_with_rules, [filing])["counts"] == {"fallback": 1}
    out = compact_stage.output_paths(corpus_with_rules, "fake", filing)
    summary = json.loads(out["summary"].read_text(encoding="utf-8"))
    assert summary["reason"].startswith("validation failed") and summary["tokens_body"] == summary["tokens_kept_text"]
    body = out["body"].read_text(encoding="utf-8")
    assert "13" not in body and "12" in body


def test_no_gain_keeps_the_uncompacted_text(small_corpus):  # no rules configured
    filing = prepare(small_corpus)["AAA_Alpha_Inc/1998_b"]
    classify(small_corpus, [filing], FakeClient(lambda m: answer("KEEP")))
    assert compact(small_corpus, [filing])["counts"] == {"no_gain": 1}


def test_compaction_saves_tokens_and_passes_on_the_fixture(corpus_with_rules):
    filing = prepare(corpus_with_rules)[FILING]
    classify(corpus_with_rules, [filing], FakeClient(lambda m: answer("KEEP")))
    assert compact(corpus_with_rules, [filing])["counts"] == {"applied": 1}
    out = compact_stage.output_paths(corpus_with_rules, "fake", filing)
    summary = json.loads(out["summary"].read_text(encoding="utf-8"))
    assert summary["tokens_body"] < summary["tokens_kept_text"] and summary["problems"] == []
    body = out["body"].read_text(encoding="utf-8")
    assert "|Research and development|$12|$(9|" in body and "|Advertising|5|4|" in body, body


def test_filter_results_from_an_older_classify_version_are_not_compacted(corpus_with_rules):
    filing = prepare(corpus_with_rules)[FILING]
    classify(corpus_with_rules, [filing], FakeClient(lambda m: answer("KEEP")))
    record = classify_stage.output_paths(corpus_with_rules, "fake", filing)["record"]
    value = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(json.dumps({**value, "stage_version": "classify-1"}), encoding="utf-8")  # made before the keep rules
    assert compact(corpus_with_rules, [filing])["counts"] == {"waiting": 1}
