from pipeline.segment.blocks import check_lossless, parse_blocks
from pipeline.segment.chunks import build_chunks, model_text
from pipeline.segment.structure import analyze

FILLER = "Narrative paragraph about operations and results for the year. " * 6


def words(value: str) -> int:
    return len(value.split()) or 1


def test_table_stays_with_caption_unit_line_and_footnotes():
    table = "|Expense|2020|2019|\n|---|---|---|\n|R&D|12|(9)|\n|Advertising|5|4|\n"
    text = (f"Item 7. Management's Discussion\n\n{FILLER}\n\nResearch and development expense\n\n"
            f"(in millions)\n\n{table}\n(1) Includes acquired licenses.\n\n(2) Restated.\n\n{FILLER}\n")
    blocks = parse_blocks(text)
    structure = analyze(text, blocks)
    for budget in (90, 120, 200):  # whatever the budget, the table unit is never cut
        chunks = build_chunks(text, blocks, budget, words, structure.sections)
        assert check_lossless(text, [(c.start, c.end) for c in chunks])
        holder = next(c for c in chunks if c.start <= text.index("|Expense") < c.end)
        span = text[holder.start:holder.end]
        assert "(in millions)" in span and "(1) Includes acquired licenses." in span and "(2) Restated." in span


def test_split_pipe_table_repeats_header_in_model_input_only():
    rows = "".join(f"|Line item {i}|{i * 10}|({i})|\n" for i in range(80))
    text = f"Item 8. Financial Statements\n\n|Account|2020|2019|\n|---|---|---|\n{rows}\nAfter the table.\n"
    blocks = parse_blocks(text)
    structure = analyze(text, blocks)
    chunks = build_chunks(text, blocks, 60, words, structure.sections)
    assert check_lossless(text, [(c.start, c.end) for c in chunks])
    continued = [c for c in chunks if c.table_header]
    assert continued
    for chunk in continued:
        rendered = model_text(text, chunk.to_dict())
        assert rendered.startswith("|Account|2020|2019|\n|---|---|---|\n")
        assert "|Account|" not in text[chunk.start:chunk.end]  # the stored range itself is unchanged
