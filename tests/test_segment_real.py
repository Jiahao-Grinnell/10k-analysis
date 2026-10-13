"""Structure detection on real filings against reviewed reference lists in tests/expected/.

Skipped when the converted corpus is not present.
"""
import json
from pathlib import Path

import pytest

from pipeline import config, tokens
from pipeline.segment.blocks import check_lossless, parse_blocks
from pipeline.segment.chunks import build_chunks
from pipeline.segment.structure import analyze

EXPECTED = sorted((Path(__file__).parent / "expected").glob("*.json"))
MARKDOWN = config.ROOT / "Markdown_10K"


@pytest.mark.skipif(not MARKDOWN.is_dir(), reason="converted corpus not available")
@pytest.mark.parametrize("path", EXPECTED, ids=lambda p: p.stem[:40])
def test_reference_filing_structure(path):
    expected = json.loads(path.read_text(encoding="utf-8"))
    text = (MARKDOWN / (expected["filing_id"] + ".md")).read_bytes().decode("utf-8")
    blocks = parse_blocks(text)
    structure = analyze(text, blocks)
    found = [[b.level, b.key, b.start, b.title[:80]] for b in structure.boundaries if b.level in {"part", "item"}]
    assert found == expected["headings"]
    assert [b.key for b in structure.boundaries if b.level == "note"] == expected["notes"]
    assert structure.quality == expected["quality"]
    cfg = config.load()
    encoding = tokens.encoding(cfg.encoding, str(cfg.tiktoken_cache))
    chunks = build_chunks(text, blocks, cfg.chunk_max_tokens, lambda v: len(encoding.encode_ordinary(v)),
                          structure.sections, structure.quality)
    assert check_lossless(text, [(c.start, c.end) for c in chunks])
    groups = {s.section_id: s.group for s in structure.sections}
    assert all(len({groups[i] for i in c.section_ids}) == 1 for c in chunks)  # no chunk crosses an Item or Note
