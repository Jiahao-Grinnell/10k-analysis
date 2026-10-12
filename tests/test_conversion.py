from pathlib import Path
import json
from unittest.mock import patch

from lxml import html
import pytest

from convertmd import (HtmlConverter, RENDER, audit_conversion, compact,
                       conversion_job, convert_legacy, decode_source, discover,
                       select_documents, split_submission, visible_text, write_summary)


def check_html(value, tmp_path):
    result = HtmlConverter(tmp_path).convert(value)
    audit = audit_conversion(result)
    assert audit['missing_numeric_occurrences'] == 0, audit
    assert audit['visible_text_equal_ignoring_whitespace'], (result.markdown, audit)
    assert audit['tables_failing_preservation'] == 0, audit
    return result


def test_inline_xbrl_does_not_change_sign_scale_or_value(tmp_path):
    value = '''<html><head><title>Metadata 999</title></head><body>
    <div style="display: none"><ix:header><ix:hidden>9999</ix:hidden></ix:header></div>
    <p>Research expense: <span>(</span><ix:nonFraction scale="6" sign="-">1,234</ix:nonFraction><span>)</span> million.</p>
    <ix:nonNumeric>Training costs are included in SG&amp;A.</ix:nonNumeric>
    <ix:continuation>Continued financial note: 2025.</ix:continuation></body></html>'''
    result = check_html(value, tmp_path)
    assert '(1,234)' in result.markdown
    assert '9999' not in result.markdown
    assert 'Training costs' in result.markdown and 'Continued financial note' in result.markdown


def test_rectangular_table_year_amount_relationship(tmp_path):
    value = '''<p>Amounts in millions.</p><table>
    <tr><th>Expense</th><th>2025</th><th>2024</th></tr>
    <tr><td>R&amp;D</td><td>$123</td><td>$(45)</td></tr>
    <tr><td>Advertising</td><td>0</td><td>—</td></tr></table>'''
    result = check_html(value, tmp_path)
    assert '|R&amp;' not in result.markdown
    rendered = html.fragment_fromstring(RENDER(result.markdown), create_parent='div')
    rows = list(rendered.iter('tr'))
    assert [visible_text(c).strip() for c in rows[1]] == ['R&D', '$123', '$(45)']
    assert result.details['tables'][0]['mode'] == 'markdown'


def test_merged_multilevel_header_preserves_geometry(tmp_path):
    value = '''<table><tr><th rowspan="2">Expense</th><th colspan="2">Year ended June 30</th></tr>
    <tr><th>2025</th><th>2024</th></tr><tr><td>R&amp;D</td><td>(12)</td><td>34</td></tr></table>'''
    result = check_html(value, tmp_path)
    assert 'rowspan="2"' in result.markdown and 'colspan="2"' in result.markdown


def test_nested_table_and_footnote_not_flattened(tmp_path):
    value = '''<table><tr><td><p>Note 7: millions</p><table>
    <tr><td>2025</td><td>2024</td></tr><tr><td>12</td><td>34</td></tr></table>
    <p>Includes acquired licenses (1).</p></td></tr></table>'''
    result = check_html(value, tmp_path)
    assert result.markdown.count('<table>') == 2
    assert 'Includes acquired licenses (1).' in result.markdown


def test_irregular_table_keeps_blank_cells(tmp_path):
    result = check_html('<table><tr><td>Year</td><td>2025</td><td>2024</td></tr><tr><td>123</td><td></td></tr></table>', tmp_path)
    assert result.details['tables'][0]['mode'] == 'html_geometry'
    assert '<td></td>' in result.markdown


def test_html_fragment_comments_and_tail(tmp_path):
    result = check_html('Before report.<p>Revenue 100.</p><!-- 999 --><p>After note 2.</p>', tmp_path)
    assert 'Before report' in result.markdown and 'After note' in result.markdown
    assert '999' not in result.markdown


def test_numbered_paragraph_and_literal_markup_are_not_eaten(tmp_path):
    result = check_html('<p>1. Amount is 25.</p><p># literal * text | A &lt; B</p><p>- 123 means negative.</p>', tmp_path)
    assert '1\\.' in result.markdown


def test_split_inline_numbered_financial_note_heading(tmp_path):
    result = check_html('<p><span>1.</span><span> Organization and operation.</span></p><div><b>12.</b> Financial instruments.</div>', tmp_path)
    assert '1\\.' in result.markdown and '12\\.' in result.markdown


def test_ordered_lists_and_table_inside_list(tmp_path):
    result = check_html('<ol start="3"><li>First amount 10.</li><li>Second amount 20.<table><tr><td>Year</td><td>2025</td></tr><tr><td>Value</td><td>30</td></tr></table></li></ol>', tmp_path)
    assert '<ol start="3">' in result.markdown and result.markdown.count('<li>') == 2


def test_preformatted_text_keeps_alignment_and_backticks(tmp_path):
    result = check_html('<pre>Year         2025     2024\nCost          123      (4)\n``` literal</pre>', tmp_path)
    assert 'Year         2025     2024' in result.markdown
    assert '````text' in result.markdown


def test_legacy_ascii_table_and_note_markers():
    result = convert_legacy('Company\n<PAGE>\n<TABLE>\n<CAPTION>Year   2025    2024\n<S>    <C>     <C>\nR&D     123     (4)\n</TABLE>\n<FN><F1>Includes training 12.\n')
    audit = audit_conversion(result)
    assert audit['visible_text_equal_ignoring_whitespace']
    assert audit['missing_numeric_occurrences'] == 0
    assert 'R&D     123     (4)' in result.markdown and '[F1]' in result.markdown


def test_cp1252_and_utf16_are_losslessly_decoded():
    text, encoding, warnings = decode_source('<p>Registrant’s cost: £123.</p>'.encode('cp1252'))
    assert 'Registrant’s' in text and encoding == 'cp1252'
    assert decode_source('Report 2025'.encode('utf-16'))[0] == 'Report 2025'


def test_binary_format_is_rejected():
    with pytest.raises(ValueError, match='Binary'):
        decode_source(b'%PDF-1.4 dummy')


def test_submission_retains_annual_report_and_textual_exhibits():
    text = '<SEC-HEADER>metadata</SEC-HEADER>' + ''.join(
        f'<DOCUMENT>\n<TYPE>{kind}\n<FILENAME>{name}\n<TEXT>{body}</TEXT>\n</DOCUMENT>\n'
        for kind, name, body in [('10-K405', 'main.txt', 'Main report 2025'),
                                 ('EX-13', 'annual.htm', '<p>Financial notes 123</p>'),
                                 ('EX-10', 'contract.txt', 'License expenditure 45'),
                                 ('EX-27', 'schedule.txt', '<ARTICLE>5'),
                                 ('EX-101.INS', 'facts.xml', '<xbrl>9999</xbrl>'),
                                 ('GRAPHIC', 'logo.gif', 'begin 644 logo.gif\nENCODED')])
    docs, warnings = split_submission(text)
    retained, excluded, warnings = select_documents(docs)
    assert [d.kind for d in retained] == ['10-K405', 'EX-13', 'EX-10']
    assert len(excluded) == 3


def test_unclosed_submission_is_reported_without_dropping_text():
    docs, warnings = split_submission('<DOCUMENT>\n<TYPE>10-K\n<TEXT>Cost 123')
    assert docs[0].body == 'Cost 123' and len(warnings) == 2


def test_missing_image_is_explicit_review_flag(tmp_path):
    result = HtmlConverter(tmp_path).convert('<p>Chart below:</p><img src="chart.jpg" alt="R&amp;D 2025">')
    audit = audit_conversion(result)
    assert audit['images'][0]['status'] == 'missing'
    assert audit['quality_status'] == 'REVIEW_REQUIRED'
    assert audit['visible_text_equal_ignoring_whitespace']


def test_multiline_image_alt_does_not_break_following_financial_tables(tmp_path):
    source = tmp_path / '2025.htm'
    source.write_text('<p>Graph:</p><img src="chart.jpg" alt="Chart, line chart\n\nDescription automatically generated">'
                      '<table><tr><td>Year</td><td>2025</td></tr><tr><td>R&amp;D</td><td>(123)</td></tr></table>')
    report = conversion_job(source, tmp_path / 'out/2025.md', {'test': TinyTokenizer()}, {})
    doc = report['documents'][0]
    assert doc['visible_text_equal_ignoring_whitespace']
    assert doc['missing_numeric_occurrences'] == doc['tables_failing_preservation'] == 0


def test_exhibit_download_is_not_mistaken_for_a_complete_10k(tmp_path):
    source = tmp_path / '2000.txt'
    source.write_text('<DOCUMENT>\n<TYPE>EX-3\n<FILENAME>dex31.txt\n<TEXT>Company charter 2000</TEXT></DOCUMENT>')
    report = conversion_job(source, tmp_path / 'out/2000.md', {'test': TinyTokenizer()}, {})
    assert report['quality_status'] == 'SOURCE_INCOMPLETE'
    assert 'EX-3:dex31.txt' in report['warnings'][0]
    assert 'zero intangible investment' in (tmp_path / 'out/2000.md').read_text()


def test_compatible_resume_retains_conversion_provenance_and_rejects_failed_audits(tmp_path):
    source = tmp_path / '2025.htm'
    source.write_text('<p>Expense 123.</p>')
    destination = tmp_path / 'out/2025.md'
    conversion_job(source, destination, {'test': TinyTokenizer()}, {})
    audit_path = destination.with_suffix('.audit.json')
    previous = json.loads(audit_path.read_text())
    previous['converter_sha256'] = 'compatible_old_version'
    previous['converter_version'] = 'old'
    audit_path.write_text(json.dumps(previous))
    with patch('convertmd.COMPATIBLE_CHECKED_HASHES', {'compatible_old_version'}):
        reused = conversion_job(source, destination, {'test': TinyTokenizer()}, {}, resume=True)
        assert reused['compatible_converter_reuse'] and reused['resumed']
        assert reused['converter_sha256'] == 'compatible_old_version'
        assert reused['verified_by_converter_sha256'] != 'compatible_old_version'
        previous['documents'][0]['visible_text_equal_ignoring_whitespace'] = False
        audit_path.write_text(json.dumps(previous))
        repaired = conversion_job(source, destination, {'test': TinyTokenizer()}, {}, resume=True)
        assert not repaired.get('resumed')
        assert repaired['documents'][0]['visible_text_equal_ignoring_whitespace']


def test_output_path_collision_is_rejected(tmp_path):
    (tmp_path / 'same.htm').write_text('<p>A</p>')
    (tmp_path / 'same.txt').write_text('B')
    with pytest.raises(ValueError, match='collide'):
        discover(tmp_path)


class TinyTokenizer:
    def encode_ordinary(self, value):
        return list(value.encode('utf-8'))


def test_atomic_results_and_resume_hash_validation(tmp_path):
    source = tmp_path / 'raw' / '2025.htm'
    source.parent.mkdir()
    source.write_text('<p>Cost 123.</p>', encoding='utf-8')
    destination = tmp_path / 'out' / '2025.md'
    tokenizers = {'test_bytes': TinyTokenizer()}
    first = conversion_job(source, destination, tokenizers, {})
    second = conversion_job(source, destination, tokenizers, {}, resume=True)
    assert second['resumed']
    destination.write_text('Damaged output', encoding='utf-8')
    repaired = conversion_job(source, destination, tokenizers, {}, resume=True)
    assert 'resumed' not in repaired
    assert 'Cost 123' in destination.read_text()
    assert first['source_sha256'] == repaired['source_sha256']


def test_preservation_guard_restores_omitted_financial_text(tmp_path):
    source = tmp_path / 'raw' / '2025.htm'
    source.parent.mkdir()
    source.write_text('<p>Expense 123.</p><table><tr><td>2025</td><td>(45)</td></tr></table>')
    destination = tmp_path / 'out' / '2025.md'
    with patch('convertmd.HtmlConverter.render', return_value='Expense only'):
        report = conversion_job(source, destination, {'test': TinyTokenizer()}, {})
    doc = report['documents'][0]
    assert doc['native_html_fallback']
    assert doc['missing_numeric_occurrences'] == 0
    assert doc['visible_text_equal_ignoring_whitespace']
    assert doc['tables_failing_preservation'] == 0


def test_header_only_download_produces_explicit_placeholder(tmp_path):
    source = tmp_path / '1993.txt'
    source.write_text('<SEC-DOCUMENT>id\n<SEC-HEADER>Metadata 1993</SEC-HEADER>\n</SEC-DOCUMENT>')
    report = conversion_job(source, tmp_path / 'out/1993.md', {'test': TinyTokenizer()}, {})
    assert report['quality_status'] == 'SOURCE_INCOMPLETE'
    summary = write_summary(tmp_path / 'out', [report], ['test'])
    assert summary['tokens']['test']['raw_source'] == 0
    assert summary['converted'] == 0 and summary['markdown_files'] == 1


def test_summary_uses_weighted_saving_not_mean_percent(tmp_path):
    def entry(raw, md):
        return {'source': 'sample', 'markdown': 'sample.md', 'quality_status': 'CHECKED_TEXT', 'documents': [],
                'tokens': {'test': {'raw_source': raw, 'included_documents_raw': raw, 'markdown': md,
                                    'saved_vs_raw': raw-md, 'reduction_vs_raw_pct': 100*(1-md/raw)}}}
    summary = write_summary(tmp_path, [entry(100, 90), entry(900, 90)], ['test'])
    assert summary['tokens']['test']['markdown'] == 180
    assert summary['tokens']['test']['reduction_vs_raw_pct'] == 82
    assert json.loads((tmp_path / 'conversion_summary.json').read_text())['converted'] == 2
