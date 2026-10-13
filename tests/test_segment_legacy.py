from pipeline.segment.blocks import check_lossless, parse_blocks
from pipeline.segment.chunks import build_chunks, model_text
from pipeline.segment.structure import analyze

PROSE = "".join(f"     The Company sold product line {i} through its retail channel during the year.\n" for i in range(8))


def words(value: str) -> int:
    return len(value.split()) or 1


def legacy() -> str:
    body = ("                          TABLE OF CONTENTS\n\n"
            "Item 1.   Business ........................................   3\n"
            "Item 2.   Properties ......................................   9\n"
            "Item 7.   Management's Discussion and Analysis ............  14\n"
            "Item 8.   Financial Statements ............................  25\n\n"
            "PART I\n\nItem 1. Business\n\n" + PROSE +
            "     additional information may be found in Part II,\n"
            "Item 8 on this Form 10-K in the Notes to the financial statements.\n\n"
            "Item 2. Properties\n\n" + PROSE + "\nPART II\n\n"
            "Item 7. Management's Discussion and Analysis\n\n" + PROSE +
            "\n                                       1998       1997\n"
            "     Research and development          $  604     $  485\n\n"
            "Item 8. Financial Statements and Supplementary Data\n\n" + PROSE)
    return "```text\n" + body + "```\n"


def test_legacy_items_ignore_toc_and_wrapped_prose():
    text = legacy()
    structure = analyze(text, parse_blocks(text))
    found = [(b.key, b.start) for b in structure.boundaries if b.level == "item"]
    assert [k for k, _ in found] == ["1", "2", "7", "8"]
    assert text[found[0][1]:].startswith("Item 1. Business")
    assert all(not text[s:].startswith("Item 8 on this") for _, s in found)
    assert [b.key for b in structure.boundaries if b.level == "part"] == ["I", "II"]


def test_boundaries_inside_a_fence_split_it_and_model_input_stays_fenced():
    text = legacy()
    blocks = parse_blocks(text)
    assert [b.kind for b in blocks] == ["fenced"]
    structure = analyze(text, blocks)
    chunks = build_chunks(text, blocks, 2000, words, structure.sections, structure.quality)
    assert check_lossless(text, [(c.start, c.end) for c in chunks])
    assert len(chunks) >= 4  # front matter + one chunk per Item at least
    for chunk in chunks:
        rendered = model_text(text, chunk.to_dict())
        assert rendered.count("```") % 2 == 0, rendered[:200]
    item7 = next(c for c in chunks if c.item == "7")
    assert "Research and development          $  604" in text[item7.start:item7.end]  # alignment kept verbatim
