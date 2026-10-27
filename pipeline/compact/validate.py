"""Checks that compacted text says exactly what the uncompacted kept text says.

Written separately from the rules: it re-reads both texts from scratch.
1. Visible tokens: the same words, numbers and signs in the same order. `$ % ( )` count
   as tokens of their own, so "$ 34,550" and "$34,550" are equal but "12 34" and "1234" are not.
2. Numbers with their signs: the same multiset ("$", "(", ")", "%" and "-" attached).
3. Fixed-width (fenced) text: byte-identical, block by block.
4. Tables: the same tables in the same order; each non-empty row has the same cells
   (symbol-only cells such as "$" or "%" counted with the number they belong to), and every
   pair of cells in different rows overlaps the same way, so numbers stay under their headings.
"""
from __future__ import annotations

from collections import Counter
import html
import re

from pipeline.compact import tables as T
from pipeline.segment.blocks import parse_blocks

TOKEN = re.compile(r"[$€£¥%()]|[^\s$€£¥%()]+")
TAG = re.compile(r"<[^>]*>")
NUMBER = re.compile(r"\d")
RIGHT = re.compile(r"^[$€£¥(]+$")
LEFT = re.compile(r"^[)%]+$")
MAX_PAIR_CHECKS = 4_000_000


def visible_tokens(markdown: str) -> list[str]:
    lines = []
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|"):
            if T.DIVIDER.match(stripped):
                continue
            line = " ".join(T.PIPE_SPLIT.split(line)).replace("\\|", "|")
        lines.append(line)
    text = html.unescape(TAG.sub(" ", "\n".join(lines)))
    return TOKEN.findall(text)


def numbers(tokens: list[str]) -> Counter:
    found: Counter = Counter()
    for i, token in enumerate(tokens):
        if NUMBER.search(token):
            before = "".join(t for t in tokens[max(0, i - 2):i] if t in "$€£¥(-")
            after = "".join(t for t in tokens[i + 1:i + 3] if t in ")%")
            found[before + token + after] += 1
    return found


def _tables(markdown: str) -> list[tuple[str, object]]:
    """('table', Table) for each parseable table with some text; ('raw', text) for HTML tables that cannot be
    modelled. Tables without any text are skipped: removing one removes nothing."""
    found = []
    for block in parse_blocks(markdown):
        body = markdown[block.start:block.end].strip()
        if block.kind == "pipe_table":
            candidates = [("pipe", body)]
        elif block.kind == "html":
            candidates = [("html", body[s:e]) for s, e in T.table_spans(body)]
        else:
            continue
        for kind, source in candidates:
            if kind == "pipe":
                table = T.parse_pipe(source)
            else:
                try:
                    table = T.parse_html(source)[0]
                except T.NotATable:
                    found.append(("raw", source))
                    continue
            if any(not c.empty for row in table for c in row):
                found.append(("table", table))
    return found


def _units(table: T.Table) -> list[list[tuple[tuple[str, ...], int, int]]]:
    """Per non-empty row: (tokens, first column, last column + 1) of each unit; symbol-only cells join their number."""
    grid = T.occupancy(table)
    position: dict[tuple[int, int], tuple[int, int]] = {}
    for line in grid:
        for c, ref in enumerate(line):
            if ref is not None:
                start, end = position.get(ref, (c, c + 1))
                position[ref] = (min(start, c), max(end, c + 1))
    rows = []
    for r, row in enumerate(table):
        units: list[list] = []
        pending: list = []  # right-binding symbols waiting for their number
        for i, cell in enumerate(row):
            if cell.empty:
                continue
            start, end = position.get((r, i), (0, 0))
            tokens = TOKEN.findall(cell.text)
            if RIGHT.match(cell.text):
                pending.append((tokens, start, end))
                continue
            if LEFT.match(cell.text) and units:
                last = units[-1]
                units[-1] = [last[0] + tokens, min(last[1], start), max(last[2], end)]
                continue
            unit = [tokens, start, end]
            for p_tokens, p_start, p_end in pending:
                unit = [p_tokens + unit[0], min(unit[1], p_start), max(unit[2], p_end)]
            pending = []
            units.append(unit)
        for p in pending:
            units.append(list(p))
        if units:
            rows.append([(tuple(u[0]), u[1], u[2]) for u in units])
    return rows


def _overlaps(rows) -> set | None:
    flat = [(r, u, s, e) for r, row in enumerate(rows) for u, (_, s, e) in enumerate(row)]
    if len(flat) ** 2 > MAX_PAIR_CHECKS:
        return None
    found = set()
    by_column: dict[int, list] = {}
    for r, u, s, e in flat:
        for c in range(s, e):
            by_column.setdefault(c, []).append((r, u))
    for members in by_column.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if a[0] != b[0]:
                    found.add((a, b) if a < b else (b, a))
    return found


def compare_tables(before: T.Table, after: T.Table) -> str | None:
    """None when equal in content and alignment, else what differs."""
    rows_before, rows_after = _units(before), _units(after)
    if [[u[0] for u in row] for row in rows_before] != [[u[0] for u in row] for row in rows_after]:
        return "cell contents differ"
    overlaps_before, overlaps_after = _overlaps(rows_before), _overlaps(rows_after)
    if overlaps_before is not None and overlaps_after is not None and overlaps_before != overlaps_after:
        return "column alignment differs"
    return None


def check(before: str, after: str) -> list[str]:
    """Problems found; an empty list means the compacted text passes."""
    problems = []
    tokens_before, tokens_after = visible_tokens(before), visible_tokens(after)
    if tokens_before != tokens_after:
        first = next((i for i, (a, b) in enumerate(zip(tokens_before, tokens_after)) if a != b),
                     min(len(tokens_before), len(tokens_after)))
        problems.append(f"visible text differs at token {first}: "
                        f"{' '.join(tokens_before[first:first + 6])!r} vs {' '.join(tokens_after[first:first + 6])!r}")
    if numbers(tokens_before) != numbers(tokens_after):
        problems.append("numbers or signs differ")
    fences = lambda text: [text[b.start:b.end].rstrip("\n") for b in parse_blocks(text) if b.kind == "fenced"]
    if fences(before) != fences(after):
        problems.append("fixed-width text changed")
    tables_before, tables_after = _tables(before), _tables(after)
    if len(tables_before) != len(tables_after):
        problems.append(f"table count differs: {len(tables_before)} vs {len(tables_after)}")
    else:
        for index, ((kind_a, a), (kind_b, b)) in enumerate(zip(tables_before, tables_after)):
            if kind_a == "raw" or kind_b == "raw":
                if (kind_a, a) != (kind_b, b):
                    problems.append(f"table {index + 1}: unparsed table changed")
                continue
            issue = compare_tables(a, b)
            if issue:
                problems.append(f"table {index + 1}: {issue}")
    return problems
