from pipeline.segment.blocks import check_lossless, parse_blocks
from pipeline.segment.chunks import build_chunks
from pipeline.segment.structure import analyze

BODY = "Body text that runs on for a while to make this section realistic. " * 12


def words(value: str) -> int:
    return len(value.split()) or 1


def items(text: str, form: str = "10-K") -> list[tuple[str, int]]:
    structure = analyze(text, parse_blocks(text), form)
    return [(b.key, b.start) for b in structure.boundaries if b.level == "item"]


def at(text: str, needle: str, occurrence: int = 1) -> int:
    position = -1
    for _ in range(occurrence):
        position = text.index(needle, position + 1)
    return position


def html_filing() -> str:
    toc = ("<table><tr><td>Item 1.</td><td>Business</td><td>3</td></tr>"
           "<tr><td>Item 1A.</td><td>Risk Factors</td><td>9</td></tr>"
           "<tr><td>Item 7.</td><td>MD&amp;A</td><td>30</td></tr>"
           "<tr><td>Item 8.</td><td>Financial Statements</td><td>45</td></tr></table>")
    return (f"Cover page text.\n\n{toc}\n\nPART I\n\nItem 1. Business\n\n{BODY}\n\n"
            f"For details see Item 7, Management's Discussion and Analysis, below. {BODY}\n\n"
            f"Item 1. Business - Continued\n\n{BODY}\n\n"
            f"Item 1A. Risk Factors\n\n{BODY}\n\nPART II\n\n"
            f"Item 7. Management's Discussion and Analysis\n\n{BODY}\n\n"
            f"Item 8. Financial Statements and Supplementary Data\n\n{BODY}\n\n"
            f"NOTE 1\n\nSUMMARY OF SIGNIFICANT ACCOUNTING POLICIES\n\n{BODY}\n\n"
            f"Note 2 – Revenue\n\n{BODY}\n\n3. Intangible Assets\n\n{BODY}\n\n"
            f"Item 9A. Controls and Procedures\n\n{BODY}\n")


def test_toc_table_prose_mentions_and_continued_headers_are_not_boundaries():
    text = html_filing()
    # occurrence 1 is the body heading; occurrence 2 is the "Continued" page header
    assert items(text) == [("1", at(text, "Item 1. Business", 1)), ("1A", at(text, "Item 1A. Risk Factors")),
                           ("7", at(text, "Item 7. Management")), ("8", at(text, "Item 8. Financial")),
                           ("9A", at(text, "Item 9A."))]


def test_notes_parts_and_heading_paths():
    text = html_filing()
    structure = analyze(text, parse_blocks(text))
    notes = [b for b in structure.boundaries if b.level == "note"]
    assert [b.key for b in notes] == ["1", "2", "3"]
    assert notes[0].title == "NOTE 1 SUMMARY OF SIGNIFICANT ACCOUNTING POLICIES"
    assert [b.key for b in structure.boundaries if b.level == "part"] == ["I", "II"]
    note2 = next(s for s in structure.sections if s.note == "2")
    assert note2.heading_path == ["PART II", "Item 8. Financial Statements and Supplementary Data", "Note 2 – Revenue"]
    assert structure.sections[0].heading_path == ["Front matter"]
    assert check_lossless(text, [(s.start, s.end) for s in structure.sections])


def test_paragraph_toc_with_page_numbers_is_skipped():
    toc = "\n\n".join(f"Item {k}. {name} {page}" for k, name, page in
                      [("1", "Business", 3), ("1A", "Risk Factors", 8), ("2", "Properties", 15),
                       ("7", "Management's Discussion", 20), ("8", "Financial Statements", 31)])
    text = (f"{toc}\n\nItem 1. Business\n\n{BODY}\n\nItem 1A. Risk Factors\n\n{BODY}\n\nItem 2. Properties\n\n{BODY}\n\n"
            f"Item 7. Management's Discussion\n\n{BODY}\n\nItem 8. Financial Statements\n\n{BODY}\n")
    assert [start for _, start in items(text)] == [at(text, "Item 1. Business", 2), at(text, "Item 1A. Risk", 2),
                                                   at(text, "Item 2. Prop", 2), at(text, "Item 7. Man", 2),
                                                   at(text, "Item 8. Fin", 2)]


def test_heading_table_with_spacer_rows_and_minimal_html_body():
    spacer = "<table><tr><td></td><td></td></tr><tr><td colspan=\"1\">ITEM {k}.</td><td>{name}</td></tr></table>"
    text = "\n\n".join(spacer.format(k=k, name=n) + f"\n\n{BODY}" for k, n in
                       [("1", "BUSINESS"), ("7", "MANAGEMENT'S DISCUSSION"), ("8", "FINANCIAL STATEMENTS")]) + "\n"
    assert [k for k, _ in items(text)] == ["1", "7", "8"]
    fallback = ("<div>\n<p>Cover</p> <p><a> </a> <br>Item 1. Business <br> </p> <p>" + BODY + "</p>"
                " <p><a> </a> <br>Item 7. Management's Discussion and Analysis <br> </p> <p>" + BODY + "</p>"
                " <p>As discussed in Item 7 of this report, sales grew.</p>"
                " <p><a> </a> <br>Item 8. Financial Statements <br> </p> <p>" + BODY + "</p>\n</div>\n")
    found = items(fallback)
    assert [k for k, _ in found] == ["1", "7", "8"]
    assert fallback[found[1][1]:].startswith("Item 7. Management")


def test_10ksb_statements_item_and_ex13_notes():
    ksb = (f"Item 6. Management's Discussion or Plan of Operation\n\n{BODY}\n\nItem 7. Financial Statements\n\n{BODY}\n\n"
           f"Note 1 - Organization\n\n{BODY}\n\nNote 2 - Property\n\n{BODY}\n\nNote 3 - Patents\n\n{BODY}\n\n"
           f"Item 8. Changes in and Disagreements with Accountants\n\n{BODY}\n")
    structure = analyze(ksb, parse_blocks(ksb), "10-KSB")
    assert [s.note for s in structure.sections if s.note] == ["1", "2", "3"]
    assert all(s.item == "7" for s in structure.sections if s.note)

    ex13 = (f"Item 7. Management's Discussion\n\nSee the Annual Report, Exhibit 13.\n\n"
            f"Item 8. Financial Statements\n\nIncorporated by reference to Exhibit 13.\n\n"
            f"# Attached document: EX-13 — annual.htm\n\nNotes to Consolidated Financial Statements\n\n"
            f"1. Summary of Accounting Policies\n\n{BODY}\n\n2. Research and Development\n\n{BODY}\n\n"
            f"3. Goodwill and Intangible Assets\n\n{BODY}\n")
    structure = analyze(ex13, parse_blocks(ex13))
    ex13_notes = [s for s in structure.sections if s.document and s.note]
    assert [s.note for s in ex13_notes] == ["1", "2", "3"]
    assert ex13_notes[1].heading_path[0] == "Attached document: EX-13 — annual.htm"


def test_no_structure_falls_back_to_one_unstructured_section():
    text = f"{BODY}\n\n{BODY}\n"
    structure = analyze(text, parse_blocks(text))
    assert structure.quality == "LOW" and [s.heading_path for s in structure.sections] == [["UNSTRUCTURED"]]


def test_chunks_never_cross_item_or_note_but_share_small_subsections():
    text = html_filing()
    blocks = parse_blocks(text)
    structure = analyze(text, blocks)
    chunks = build_chunks(text, blocks, 400, words, structure.sections, structure.quality)
    assert check_lossless(text, [(c.start, c.end) for c in chunks])
    groups = {s.section_id: s.group for s in structure.sections}
    for chunk in chunks:
        assert len({groups[i] for i in chunk.section_ids}) == 1, chunk
        assert chunk.quality == structure.quality
    sections_by_id = {s.section_id: s for s in structure.sections}
    for chunk in chunks:
        first = sections_by_id[chunk.section_id]
        assert first.start <= chunk.start < first.end
