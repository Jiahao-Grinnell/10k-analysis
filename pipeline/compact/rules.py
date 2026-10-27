"""Deterministic compaction rules. They remove presentational markup, empty wrappers,
wasted whitespace and table columns/rows that are empty everywhere; they never remove
or reorder visible text, numbers or signs. Fixed-width (fenced) and <pre> text is never touched.

Each rule can be switched on or off in config.toml ([compact] rules) and is measured
first with `compact --profile`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import re

from lxml import etree, html as lxml_html

from pipeline.compact import tables as T
from pipeline.compact.validate import compare_tables
from pipeline.segment.blocks import Block

TABLE_RULES = ["html_minify", "cell_whitespace", "remove_empty_wrappers", "unwrap_cell_wrappers",
               "merge_symbol_cells", "drop_empty_rows", "drop_empty_columns", "table_to_pipe"]
RULES = TABLE_RULES + ["html_whitespace", "prose_whitespace"]
RULES_VERSION = "compact-rules-1"

WRAPPERS = {"p", "div", "span"}
EMPTY_WRAPPERS = {"p", "div", "span", "b", "i", "u", "font", "strong", "em", "sup", "sub"}
RIGHT_SYMBOLS = re.compile(r"^[$€£¥(]+$")       # "$" "(" "($": belong to the number on their right
LEFT_SYMBOLS = re.compile(r"^[)%]+$")            # ")" "%" ")%": belong to the number on their left
NUMBER_START = re.compile(r"^[\s(\-–—]*[\d.]")
NUMBER_END = re.compile(r"[\d.)]\s*$")
DASH_ONLY = re.compile(r"^[\-–—]+$")             # "—" stands for zero / nil in SEC tables


def _cell_clean(inner: str, rules: set[str], counts: Counter) -> str:
    """Rules that work inside one cell's HTML."""
    if "<pre" in inner.lower():
        return inner
    if {"remove_empty_wrappers", "unwrap_cell_wrappers"} & rules and "<" in inner:
        try:
            holder = lxml_html.fragment_fromstring(f"<td>{inner}</td>")
        except (etree.ParserError, ValueError):
            holder = None
        if holder is not None and holder.tag == "td":
            changed = False
            if "remove_empty_wrappers" in rules:
                for element in list(holder.iter()):
                    if (element is not holder and element.tag in EMPTY_WRAPPERS and len(element) == 0
                            and not (element.text or "").strip()):
                        element.drop_tag()  # keeps its tail text
                        counts["remove_empty_wrappers"] += 1
                        changed = True
            if "unwrap_cell_wrappers" in rules:
                children = [c for c in holder if isinstance(c.tag, str)]
                if (len(children) == 1 and children[0].tag in WRAPPERS and not (holder.text or "").strip()
                        and not (children[0].tail or "").strip()
                        and not any(isinstance(d.tag, str) and d.tag in T.BLOCK_TAGS for d in children[0].iterdescendants())):
                    children[0].drop_tag()
                    counts["unwrap_cell_wrappers"] += 1
                    changed = True
            if changed:
                inner = T._inner_html(holder)
    if "cell_whitespace" in rules:
        collapsed = T.SPACE.sub(" ", inner).strip()
        if collapsed != inner:
            counts["cell_whitespace"] += 1
            inner = collapsed
    return inner


def _merge_symbols(row: list[T.Cell], counts: Counter) -> list[T.Cell]:
    """'$' + '34,550' -> '$34,550'; '(9,834' + ')' -> '(9,834)'. Only plain single-row cells."""
    out: list[T.Cell] = []
    i = 0
    while i < len(row):
        cell = row[i]
        nxt = row[i + 1] if i + 1 < len(row) else None
        if (nxt is not None and cell.rowspan == 1 and nxt.rowspan == 1 and RIGHT_SYMBOLS.match(cell.text or "-")
                and "<" not in cell.inner and (NUMBER_START.match(nxt.text) or DASH_ONLY.match(nxt.text))):
            out.append(T.with_inner(replace(nxt, colspan=cell.colspan + nxt.colspan), cell.inner.strip() + nxt.inner.strip()))
            counts["merge_symbol_cells"] += 1
            i += 2
            continue
        if (out and cell.rowspan == 1 and out[-1].rowspan == 1 and LEFT_SYMBOLS.match(cell.text or "-")
                and "<" not in cell.inner and NUMBER_END.search(out[-1].text)):
            prev = out.pop()
            out.append(T.with_inner(replace(prev, colspan=prev.colspan + cell.colspan), prev.inner.strip() + cell.inner.strip()))
            counts["merge_symbol_cells"] += 1
            i += 1
            continue
        out.append(cell)
        i += 1
    return out


def drop_empty_columns(table: T.Table, counts: Counter | None = None) -> T.Table:
    """Remove a column when every cell covering it is empty or also covers another column."""
    table = [list(row) for row in table]
    while True:
        grid = T.occupancy(table)
        width = len(grid[0]) if grid else 0
        target = None
        for c in range(width - 1, -1, -1):
            ok = True
            for line in grid:
                ref = line[c]
                if ref is None:
                    continue
                cell = table[ref[0]][ref[1]]
                if not (cell.empty or cell.colspan > 1):
                    ok = False
                    break
            if ok:
                target = c
                break
        if target is None:
            return table
        refs = {line[target] for line in grid if line[target] is not None}
        removed = []
        for r, i in refs:
            cell = table[r][i]
            if cell.colspan > 1:
                table[r][i] = replace(cell, colspan=cell.colspan - 1)
            else:
                removed.append((r, i))
        for r, i in sorted(removed, reverse=True):
            del table[r][i]
        if counts is not None:
            counts["drop_empty_columns"] += 1


def compact_table(source: str, rules: set[str], counts: Counter) -> str | None:
    """Compacted text for one HTML table block body, or None to leave it as it is."""
    if not rules & set(TABLE_RULES):
        return None
    try:
        table, _ = T.parse_html(source)
    except T.NotATable as exc:
        counts[f"table_skipped: {str(exc).split(':')[0]}"] += 1
        return None
    original = table
    layout = {"drop_empty_columns", "merge_symbol_cells", "drop_empty_rows"}
    if any(c.rowspan > 1 for row in table for c in row) and layout & rules:
        rules = rules - layout  # keep row-spanning layouts as they are
        counts["table_rowspan_kept"] += 1
    table = [[T.with_inner(c, _cell_clean(c.inner, rules, counts)) for c in row] for row in table]
    if "merge_symbol_cells" in rules:
        table = [_merge_symbols(row, counts) for row in table]
    if "drop_empty_rows" in rules:
        kept = [row for row in table if any(not c.empty for c in row) or any(c.rowspan > 1 for c in row)]
        counts["drop_empty_rows"] += len(table) - len(kept)
        table = kept
    if not any(not c.empty for row in table for c in row):
        if "remove_empty_wrappers" in rules:  # a table without any text is an empty wrapper
            counts["empty_table_removed"] += 1
            return ""
        return None
    if "drop_empty_columns" in rules:
        table = drop_empty_columns(table, counts)
    # Each table is checked on its own; one that fails stays as it was (the filing check runs again later).
    if compare_tables(original, table) is not None:
        counts["table_kept_failed_check"] += 1
        return None
    if "table_to_pipe" in rules and T.pipe_ready(table) is None:
        counts["table_to_pipe"] += 1
        return T.render_pipe(table)
    counts["html_minify"] += 1
    return T.render_html(table)


TRAILING = re.compile(r"[ \t]+$", re.M)
INNER_RUN = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")


def compact_prose(source: str, counts: Counter) -> str:
    """Paragraph text: runs of spaces inside a line become one; trailing spaces go. Line breaks stay."""
    out = INNER_RUN.sub(" ", TRAILING.sub("", source))
    if out != source:
        counts["prose_whitespace"] += 1
    return out


HTML_RUN = re.compile(r"\s{2,}")


def compact_html(source: str, rules: set[str], counts: Counter) -> str:
    """An HTML block that is not a single table (minimal-HTML filings put the whole report in one):
    tables inside it are compacted but stay HTML; whitespace runs become one space."""
    if "<pre" in source.lower():
        return source
    parts, position = [], 0
    for start, end in T.table_spans(source):
        parts.append(_html_space(source[position:start], rules, counts))
        result = compact_table(source[start:end], rules - {"table_to_pipe"}, counts)
        parts.append(source[start:end] if result is None else result)
        position = end
    parts.append(_html_space(source[position:], rules, counts))
    return "".join(parts)


def _html_space(fragment: str, rules: set[str], counts: Counter) -> str:
    if "html_whitespace" not in rules:
        return fragment
    out = HTML_RUN.sub(" ", fragment)
    if out != fragment:
        counts["html_whitespace"] += 1
    return out


def compact_block(text: str, block: Block, rules: set[str], counts: Counter) -> str:
    """Compacted body of one block (its trailing blank lines are kept by the caller)."""
    if block.kind == "html" and text.lstrip().lower().startswith("<table") and len(T.table_spans(text)) == 1:
        result = compact_table(text, rules, counts)
        return text if result is None else result
    if block.kind == "html":
        return compact_html(text, rules, counts)
    if block.kind == "paragraph" and "prose_whitespace" in rules and "`" not in text:
        return compact_prose(text, counts)
    return text
