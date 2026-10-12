"""Pack blocks into reading chunks of at most about `max_tokens` tokens.

Chunks are character ranges of the Markdown file; joined in order they equal it.
A block that is too large on its own is split at line breaks (and very long
lines at whitespace or tag ends). When a fenced block is split, the pieces
record which fence they start or end inside, so the model input can re-close
the fence; the stored ranges stay verbatim.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Callable

from pipeline.segment.blocks import Block, check_lossless

UNSTRUCTURED = "UNSTRUCTURED"
BREAK = re.compile(r"[\s>]")


@dataclass
class Piece:
    start: int
    end: int
    tokens: int
    kind: str
    open_fence: str = ""
    close_fence: str = ""


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


def _split_block(text: str, block: Block, max_tokens: int, count: Callable[[str], int]) -> list[Piece]:
    units: list[tuple[int, int, int]] = []
    position = block.start
    for line in text[block.start:block.end].splitlines(keepends=True):
        end = position + len(line)
        tokens = count(line)
        units.extend(_split_long_line(text, position, end, max_tokens, count) if tokens > max_tokens
                     else [(position, end, tokens)])
        position = end
    pieces: list[Piece] = []
    for start, end, tokens in units:
        if pieces and pieces[-1].tokens + tokens <= max_tokens:
            pieces[-1].end, pieces[-1].tokens = end, pieces[-1].tokens + tokens
        else:
            pieces.append(Piece(start, end, tokens, block.kind))
    if block.fence:
        for index, piece in enumerate(pieces):
            piece.open_fence = block.fence if index > 0 else ""
            piece.close_fence = block.fence if index < len(pieces) - 1 else ""
    return pieces


def _kind(kinds: set[str]) -> str:
    table = {"pipe_table", "html"} & kinds
    prose = {"paragraph", "heading", "blank"} & kinds
    if "fenced" in kinds and not table and not prose:
        return "fixed_width"
    if table and not prose and "fenced" not in kinds:
        return "table"
    return "mixed" if table else "prose"


def build_chunks(text: str, blocks: list[Block], max_tokens: int, count: Callable[[str], int]) -> list[Chunk]:
    pieces: list[Piece] = []
    for block in blocks:
        tokens = count(text[block.start:block.end])
        if tokens <= max_tokens:
            pieces.append(Piece(block.start, block.end, tokens, block.kind))
        else:
            pieces.extend(_split_block(text, block, max_tokens, count))
    groups: list[list[Piece]] = []
    for piece in pieces:
        # A piece that starts inside a fence continues the previous chunk's open fence; keep packing simple.
        if groups and sum(p.tokens for p in groups[-1]) + piece.tokens <= max_tokens:
            groups[-1].append(piece)
        else:
            groups.append([piece])
    chunks = []
    for index, group in enumerate(groups, 1):
        start, end = group[0].start, group[-1].end
        kinds = [p.kind for p in group]
        chunks.append(Chunk(
            chunk_id=f"c{index:05d}", section_id="s001", heading_path=[UNSTRUCTURED], item=None,
            start=start, end=end, tokens=count(text[start:end]), kind=_kind(set(kinds)), quality="LOW",
            open_fence=group[0].open_fence, close_fence=group[-1].close_fence,
            block_kinds=sorted(set(kinds))))
    if not check_lossless(text, [(c.start, c.end) for c in chunks]):
        raise ValueError("Chunks do not reproduce the Markdown exactly")
    return chunks


def model_text(markdown: str, chunk: dict) -> str:
    """Chunk text for the model; re-open/close a fence the chunk starts or ends inside."""
    text = markdown[chunk["start"]:chunk["end"]]
    if chunk.get("open_fence"):
        text = chunk["open_fence"] + "text\n" + text
    if chunk.get("close_fence"):
        text = text + ("" if text.endswith("\n") else "\n") + chunk["close_fence"] + "\n"
    return text
