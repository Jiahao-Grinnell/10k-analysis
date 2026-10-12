from pathlib import Path

import pytest

from pipeline import config, tokens
from pipeline.segment.blocks import check_lossless, parse_blocks
from pipeline.segment.chunks import build_chunks, model_text

SAMPLE = (
    "\n\n# Heading\n\nFirst paragraph line one.\nline two.\n\n"
    "|A|B|\n|---|---|\n|1|(2)|\n\n"
    "<table><tr><td>outer<table><tr><td>inner</td></tr>\n</table></td></tr>\n</table>\n\n"
    "````text\nfixed ``` width\n   12   (34)\n````\n\n"
    "<img src=\"x.gif\" alt=\"logo\">\n"
    "Last paragraph without newline"
)


def words(value: str) -> int:
    return len(value.split()) or 1


def test_blocks_cover_every_character_and_detect_kinds():
    blocks = parse_blocks(SAMPLE)
    assert check_lossless(SAMPLE, [(b.start, b.end) for b in blocks])
    assert [b.kind for b in blocks] == ["blank", "heading", "paragraph", "pipe_table", "html", "fenced", "html", "paragraph"]
    fenced = next(b for b in blocks if b.kind == "fenced")
    assert fenced.fence == "````" and SAMPLE[fenced.start:fenced.end].rstrip().endswith("````")
    nested = next(b for b in blocks if b.kind == "html")
    assert SAMPLE[nested.start:nested.end].count("</table>") == 2


def test_crlf_and_empty_text_are_lossless():
    for text in ["", "a\r\n\r\nb\r\n", "|x|\r\n|---|\r\n"]:
        assert check_lossless(text, [(b.start, b.end) for b in parse_blocks(text)])


def test_oversized_fence_is_split_with_fence_markers():
    body = "".join(f"   row {i} value {i * 3}\n" for i in range(40))
    text = "Intro.\n\n```text\n" + body + "```\n\nOutro.\n"
    chunks = build_chunks(text, parse_blocks(text), 30, words)
    assert check_lossless(text, [(c.start, c.end) for c in chunks])
    inside = [c for c in chunks if c.open_fence or c.close_fence]
    assert len(inside) >= 2
    for chunk in inside:
        rendered = model_text(text, chunk.to_dict())
        assert rendered.count("```") % 2 == 0, rendered  # every fence the model sees is closed


def test_chunks_respect_budget_except_single_long_units():
    text = "\n\n".join(f"Paragraph {i} " + "word " * (i % 7 + 3) for i in range(60)) + "\n"
    chunks = build_chunks(text, parse_blocks(text), 25, words)
    assert check_lossless(text, [(c.start, c.end) for c in chunks])
    assert all(c.tokens <= 25 for c in chunks)
    assert [c.chunk_id for c in chunks] == [f"c{i:05d}" for i in range(1, len(chunks) + 1)]


def test_very_long_single_line_is_split():
    text = "<table>" + "<tr><td>cell value 123</td></tr>" * 400 + "</table>\n"
    chunks = build_chunks(text, parse_blocks(text), 100, words)
    assert len(chunks) > 1 and check_lossless(text, [(c.start, c.end) for c in chunks])


REAL = config.ROOT / "Markdown_10K"


@pytest.mark.skipif(not REAL.is_dir(), reason="converted corpus not available")
def test_real_markdown_sample_is_lossless():
    cfg = config.load()
    encoding = tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))
    count = lambda value: len(encoding.encode_ordinary(value))
    files = sorted(p for p in REAL.rglob("*.md") if p.parent != REAL)
    sample = files[:: max(1, len(files) // 25)]  # spread across companies and years
    for path in sample:
        text = path.read_bytes().decode("utf-8")
        chunks = build_chunks(text, parse_blocks(text), cfg.chunk_max_tokens, count)
        assert "".join(text[c.start:c.end] for c in chunks) == text, path
