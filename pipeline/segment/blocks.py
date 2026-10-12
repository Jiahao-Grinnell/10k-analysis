"""Split converter Markdown into ordered blocks with exact character offsets.

Every character belongs to exactly one block; trailing blank lines belong to the
block before them. Kinds: heading, paragraph, pipe_table, html, fenced.
These are format rules only; they never decide relevance.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

FENCE_OPEN = re.compile(r"(`{3,})text[ \t]*$")
HTML_START = re.compile(r"<(table|ul|ol|div|pre|dl|blockquote|img)\b", re.I)
TAG = re.compile(r"<(/?)(table|ul|ol|div|pre|dl|blockquote)\b[^>]*>", re.I)


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    kind: str
    fence: str = ""   # backtick fence string for fenced blocks


def _lines(text: str) -> list[tuple[int, int]]:
    """(start, end) of each line, end including its newline."""
    spans, position = [], 0
    for line in text.splitlines(keepends=True):
        spans.append((position, position + len(line)))
        position += len(line)
    return spans


def parse_blocks(text: str) -> list[Block]:
    lines = _lines(text)
    blocks: list[Block] = []
    i = 0
    content = lambda k: text[lines[k][0]:lines[k][1]].rstrip("\r\n")
    while i < len(lines):
        line = content(i)
        start = lines[i][0]
        if not line.strip():
            # Leading blank lines (only possible at the document start) form their own block.
            j = i
            while j < len(lines) and not content(j).strip():
                j += 1
            if blocks:
                last = blocks[-1]
                blocks[-1] = Block(last.start, lines[j - 1][1], last.kind, last.fence)
            else:
                blocks.append(Block(start, lines[j - 1][1], "blank"))
            i = j
            continue
        kind, fence, j = "paragraph", "", i + 1
        opened = FENCE_OPEN.match(line)
        if opened:
            kind, fence = "fenced", opened.group(1)
            while j < len(lines) and content(j).strip() != fence:
                j += 1
            j = min(j + 1, len(lines))  # include the closing fence line
        elif line.startswith("#"):
            kind = "heading"
        elif line.startswith("|"):
            kind = "pipe_table"
            while j < len(lines) and content(j).startswith("|"):
                j += 1
        elif HTML_START.match(line):
            kind = "html"
            depth, k = 0, i
            # Follow nesting of block-level tags so a table or list spanning lines stays whole.
            while k < len(lines):
                for tag in TAG.finditer(content(k)):
                    depth += -1 if tag.group(1) else 1
                k += 1
                if depth <= 0:
                    break
            j = k
        else:
            while j < len(lines) and content(j).strip() and not (
                    FENCE_OPEN.match(content(j)) or content(j).startswith(("#", "|")) or HTML_START.match(content(j))):
                j += 1
        blocks.append(Block(start, lines[j - 1][1], kind, fence))
        i = j
    return blocks


def check_lossless(text: str, spans: list[tuple[int, int]]) -> bool:
    """Spans are contiguous, start at 0, end at len(text)."""
    position = 0
    for start, end in spans:
        if start != position or end < start:
            return False
        position = end
    return position == len(text)
