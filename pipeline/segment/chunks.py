"""Pack blocks into reading chunks of at most about `max_tokens` tokens, following document structure.

Chunks are character ranges of the Markdown file; joined in order they equal it.

- A chunk never crosses a hard boundary (attached document, Part, Item, Note).
- Small subsections inside one hard unit may share a chunk; a subsection is kept whole
  when it fits, so a chunk boundary falls between subsections whenever possible.
- A table travels with up to two short lines before it (caption, unit line) and the
  footnotes right after it.
- A block that is too large on its own is split at line breaks (very long lines at
  whitespace or tag ends). Split fenced text records the fence to re-open/close, and a
  split pipe table records its header rows; both are added only in the model input.
  The stored ranges stay verbatim.
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field
import re
from typing import Callable

from pipeline.segment.blocks import Block, check_lossless

UNSTRUCTURED = "UNSTRUCTURED"
BREAK = re.compile(r"[\s>]")
FOOTNOTE = re.compile(r"^\s*(?:\\?\(\s*(?:\d{1,2}|[a-z]|[ivx]{1,4})\s*\\?\)|\\?\*{1,3}|\[\d+\]|\d\)\s)", re.I)
MAX_CAPTION_TOKENS = 80


@dataclass
class Piece:
    start: int
    end: int
    tokens: int
    kind: str
    open_fence: str = ""
    close_fence: str = ""
    table_header: list[int] | None = None  # [start, end) of the pipe-table header to repeat
    section: int = 0


@dataclass
class Chunk:
    chunk_id: str
    section_id: str
    heading_path: list[str]
    item: str | None
    start: int
    end: int
    tokens: int
    kind: str
    quality: str
    open_fence: str = ""
    close_fence: str = ""
    block_kinds: list[str] = field(default_factory=list)
    section_ids: list[str] = field(default_factory=list)
    part: str | None = None
    note: str | None = None
    document: str | None = None
    table_header: list[int] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _split_long_line(text: str, start: int, end: int, max_tokens: int, count: Callable[[str], int]) -> list[tuple[int, int, int]]:
    spans, position = [], start
    window = max(200, max_tokens * 2)
    while position < end:
        stop = min(end, position + window)
        if stop < end:
            cut = max((m.end() for m in BREAK.finditer(text, position, stop)), default=stop)
            stop = cut if cut > position else stop
        tokens = count(text[position:stop])
        if tokens > max_tokens and stop - position > 200:
            window = max(200, window // 2)
            continue
        spans.append((position, stop, tokens))
        position = stop
    return spans


def _header_span(text: str, block: Block) -> list[int] | None:
    """The first two lines (header row + divider) of a pipe table."""
    lines = text[block.start:block.end].splitlines(keepends=True)
    if block.kind != "pipe_table" or len(lines) < 3:
        return None
    return [block.start, block.start + len(lines[0]) + len(lines[1])]


def _pieces(text: str, block: Block, start: int, end: int, max_tokens: int, count) -> list[Piece]:
    """Pieces of block[start:end] no larger than max_tokens (unless a single unsplittable span)."""
    tokens = count(text[start:end])
    if tokens <= max_tokens:
        spans = [(start, end, tokens)]
    else:
        units: list[tuple[int, int, int]] = []
        position = start
        for line in text[start:end].splitlines(keepends=True):
            stop = position + len(line)
            line_tokens = count(line)
            units.extend(_split_long_line(text, position, stop, max_tokens, count) if line_tokens > max_tokens
                         else [(position, stop, line_tokens)])
            position = stop
        spans = []
        for s, e, t in units:
            if spans and spans[-1][2] + t <= max_tokens:
                spans[-1] = (spans[-1][0], e, spans[-1][2] + t)
            else:
                spans.append((s, e, t))
    header = _header_span(text, block)
    pieces = []
    for s, e, t in spans:
        piece = Piece(s, e, t, block.kind)
        if block.fence:
            piece.open_fence = block.fence if s > block.start else ""
            piece.close_fence = block.fence if e < block.end else ""
        if header and s >= header[1]:
            piece.table_header = header
        pieces.append(piece)
    return pieces


def _is_table(text: str, piece: Piece) -> bool:
    return piece.kind == "pipe_table" or (piece.kind == "html" and "<table" in text[piece.start:piece.end].lower())


def _units(text: str, pieces: list[Piece]) -> list[list[Piece]]:
    """Group a table with its caption/unit lines before it and footnotes after it (same section only)."""
    units: list[list[Piece]] = [[p] for p in pieces]
    merged: list[list[Piece]] = []
    i = 0
    while i < len(units):
        piece = units[i][0]
        if _is_table(text, piece):
            group = list(units[i])
            # captions: up to two short paragraphs right before the table
            taken = 0
            while merged and taken < 2:
                previous = merged[-1]
                if (len(previous) == 1 and previous[0].kind == "paragraph" and previous[0].tokens <= MAX_CAPTION_TOKENS
                        and previous[0].section == piece.section):
                    group = merged.pop() + group
                    taken += 1
                else:
                    break
            # footnotes right after the table
            j = i + 1
            while j < len(units) and j - i <= 4:
                following = units[j][0]
                if (following.kind == "paragraph" and following.section == piece.section
                        and FOOTNOTE.match(text[following.start:following.end])):
                    group += units[j]
                    j += 1
                else:
                    break
            merged.append(group)
            i = j
            continue
        merged.append(units[i])
        i += 1
    return merged


def build_chunks(text: str, blocks: list[Block], max_tokens: int, count: Callable[[str], int],
                 sections: list | None = None, quality: str = "LOW") -> list[Chunk]:
    """`sections` (from structure.analyze) must cover the text contiguously; None means one section."""
    if not sections:
        from pipeline.segment.structure import Section
        sections = [Section("s0001", 0, len(text), [UNSTRUCTURED])]
    starts = [s.start for s in sections]
    pieces: list[Piece] = []
    for block in blocks:
        cuts = [block.start] + [c for c in starts if block.start < c < block.end] + [block.end]
        for a, b in zip(cuts, cuts[1:]):
            for piece in _pieces(text, block, a, b, max_tokens, count):
                piece.section = bisect.bisect_right(starts, piece.start) - 1
                pieces.append(piece)

    section_tokens: dict[int, int] = {}
    for piece in pieces:
        section_tokens[piece.section] = section_tokens.get(piece.section, 0) + piece.tokens

    groups: list[list[Piece]] = []
    current: list[Piece] = []
    current_tokens = 0
    previous_section = None

    def flush():
        nonlocal current, current_tokens
        if current:
            groups.append(current)
        current, current_tokens = [], 0

    for unit in _units(text, pieces):
        section = unit[0].section
        if previous_section is not None and section != previous_section:
            if getattr(sections[section], "group", "") != getattr(sections[previous_section], "group", ""):
                flush()  # hard boundary: never share a chunk
            elif current_tokens + section_tokens[section] > max_tokens:
                flush()  # keep the next subsection whole if it fits in a fresh chunk
        previous_section = section
        unit_tokens = sum(p.tokens for p in unit)
        if unit_tokens > max_tokens:  # an oversized table group: place its pieces one by one
            for piece in unit:
                if current_tokens + piece.tokens > max_tokens:
                    flush()
                current.append(piece)
                current_tokens += piece.tokens
            continue
        if current_tokens + unit_tokens > max_tokens:
            flush()
        current.extend(unit)
        current_tokens += unit_tokens
    flush()

    chunks = []
    for index, group in enumerate(groups, 1):
        start, end = group[0].start, group[-1].end
        kinds = sorted({p.kind for p in group})
        first = sections[group[0].section]
        ids = []
        for piece in group:
            if sections[piece.section].section_id not in ids:
                ids.append(sections[piece.section].section_id)
        chunks.append(Chunk(
            chunk_id=f"c{index:05d}", section_id=first.section_id, heading_path=list(first.heading_path),
            item=first.item, start=start, end=end, tokens=count(text[start:end]), kind=_kind(set(kinds)),
            quality=quality, open_fence=group[0].open_fence, close_fence=group[-1].close_fence,
            block_kinds=kinds, section_ids=ids, part=first.part, note=first.note, document=first.document,
            table_header=group[0].table_header))
    if not check_lossless(text, [(c.start, c.end) for c in chunks]):
        raise ValueError("Chunks do not reproduce the Markdown exactly")
    return chunks


def _kind(kinds: set[str]) -> str:
    table = {"pipe_table", "html"} & kinds
    prose = {"paragraph", "heading", "blank"} & kinds
    if "fenced" in kinds and not table and not prose:
        return "fixed_width"
    if table and not prose and "fenced" not in kinds:
        return "table"
    return "mixed" if table else "prose"


def model_text(markdown: str, chunk: dict) -> str:
    """Chunk text for the model: re-open/close a fence the chunk starts or ends inside, and
    repeat the header of a pipe table the chunk continues. The stored range is unchanged."""
    text = markdown[chunk["start"]:chunk["end"]]
    header = chunk.get("table_header")
    if header:
        text = markdown[header[0]:header[1]] + text
    if chunk.get("open_fence"):
        text = chunk["open_fence"] + "text\n" + text
    if chunk.get("close_fence"):
        text = text + ("" if text.endswith("\n") else "\n") + chunk["close_fence"] + "\n"
    return text
