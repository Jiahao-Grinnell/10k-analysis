"""HTML / pipe table model shared by the compaction rules and their validation.

A table is a list of rows; a row is a list of cells. Each cell keeps its tag, spans,
inner HTML and visible text. Only tables made of rows and cells are modelled; anything
else (nested tables, captions, lists inside the table) is left alone by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import html
import re

from lxml import etree, html as lxml_html

SPACE = re.compile(r"\s+")
BLOCK_TAGS = {"p", "div", "ul", "ol", "li", "table", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
ALLOWED_PARENTS = {"table", "thead", "tbody", "tfoot", "tr"}


class NotATable(ValueError):
    pass


@dataclass(frozen=True)
class Cell:
    tag: str            # td | th
    colspan: int
    rowspan: int
    inner: str          # inner HTML, as it will be written
    text: str           # visible text, whitespace-normalised

    @property
    def empty(self) -> bool:
        return not self.text and "<img" not in self.inner.lower()


Table = list[list[Cell]]


def visible(inner: str) -> str:
    """Visible text of an HTML fragment: tags become spaces, entities are decoded."""
    text = re.sub(r"<[^>]*>", " ", inner)
    return SPACE.sub(" ", html.unescape(text)).strip()


def _inner_html(element) -> str:
    parts = [html.escape(element.text, quote=False) if element.text else ""]
    for child in element:
        parts.append(etree.tostring(child, encoding="unicode", method="html", with_tail=True))
    return "".join(parts)


def _span(value: str | None) -> int:
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return 1


def parse_html(source: str) -> tuple[Table, object]:
    """Rows of the single <table> in `source`; also returns the lxml cell elements per row.
    Raises NotATable for anything that is not one plain table."""
    stripped = source.strip()
    if not stripped.lower().startswith("<table") or not stripped.lower().endswith("</table>"):
        raise NotATable("not a single table")
    if stripped.lower().count("<table") != 1:
        raise NotATable("nested or several tables")
    try:
        root = lxml_html.fragment_fromstring(stripped)
    except (etree.ParserError, ValueError) as exc:
        raise NotATable(f"unparseable: {exc}") from exc
    if root.tag != "table":
        raise NotATable("root is not a table")
    elements = []
    for element in root.iter():
        if element is root or not isinstance(element.tag, str):
            continue
        parent = element.getparent()
        if element.tag in {"td", "th"}:
            if parent.tag != "tr":
                raise NotATable("cell outside a row")
        elif element.tag in {"tr", "thead", "tbody", "tfoot"}:
            if parent.tag not in ALLOWED_PARENTS:
                raise NotATable(f"{element.tag} in {parent.tag}")
        elif parent.tag in ALLOWED_PARENTS:
            raise NotATable(f"<{element.tag}> directly inside the table")
    if (root.text or "").strip() or any((e.tail or "").strip() for e in root.iter() if e.tag in ALLOWED_PARENTS | {"td", "th"} and e is not root):
        raise NotATable("text outside cells")
    rows, cells = [], []
    for tr in root.iter("tr"):
        row, row_cells = [], []
        for cell in tr:
            if not isinstance(cell.tag, str):
                continue
            row_cells.append(cell)
            row.append(Cell(cell.tag, _span(cell.get("colspan")), _span(cell.get("rowspan")),
                            _inner_html(cell), visible(_inner_html(cell))))
        rows.append(row)
        cells.append(row_cells)
    return rows, cells


PIPE_SPLIT = re.compile(r"(?<!\\)\|")
DIVIDER = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def parse_pipe(source: str) -> Table:
    rows = []
    for line in source.strip().splitlines():
        line = line.strip()
        if not line or DIVIDER.match(line):
            continue
        body = line[1:] if line.startswith("|") else line
        body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
        rows.append([Cell("td", 1, 1, part, visible(part.replace("\\|", "|"))) for part in PIPE_SPLIT.split(body)])
    return rows


def occupancy(table: Table) -> list[list[tuple[int, int] | None]]:
    """grid[r][c] = (row, index) of the cell covering that position, honouring row and column spans."""
    grid: list[list[tuple[int, int] | None]] = []
    for r, row in enumerate(table):
        while len(grid) <= r:
            grid.append([])
        c = 0
        for i, cell in enumerate(row):
            while c < len(grid[r]) and grid[r][c] is not None:
                c += 1
            for dr in range(cell.rowspan):
                while len(grid) <= r + dr:
                    grid.append([])
                line = grid[r + dr]
                while len(line) < c + cell.colspan:
                    line.append(None)
                for dc in range(cell.colspan):
                    line[c + dc] = (r, i)
            c += cell.colspan
    width = max((len(line) for line in grid), default=0)
    for line in grid:
        line.extend([None] * (width - len(line)))
    return grid


def render_html(table: Table) -> str:
    out = ["<table>"]
    for row in table:
        out.append("<tr>")
        for cell in row:
            spans = (f' colspan="{cell.colspan}"' if cell.colspan > 1 else "") + \
                    (f' rowspan="{cell.rowspan}"' if cell.rowspan > 1 else "")
            out.append(f"<{cell.tag}{spans}>{cell.inner}</{cell.tag}>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def pipe_ready(table: Table) -> str | None:
    """None when the table can be written as a pipe table without losing structure, else the reason."""
    if not table or not table[0]:
        return "empty table"
    width = len(table[0])
    for row in table:
        if len(row) != width:
            return "rows of different length"
        for cell in row:
            if cell.colspan > 1 or cell.rowspan > 1:
                return "merged cells"
            tags = {m.lower() for m in re.findall(r"<\s*([a-zA-Z0-9]+)", cell.inner)}
            if tags & BLOCK_TAGS:
                return "cell with several paragraphs or block content"
    return None


def render_pipe(table: Table) -> str:
    width = len(table[0])
    lines = []
    for index, row in enumerate(table):
        cells = [SPACE.sub(" ", cell.inner).strip().replace("|", "\\|") for cell in row]
        lines.append("|" + "|".join(cells) + "|")
        if index == 0:
            lines.append("|" + "|".join(["---"] * width) + "|")
    return "\n".join(lines)


def with_inner(cell: Cell, inner: str) -> Cell:
    return replace(cell, inner=inner, text=visible(inner))


TABLE_TAG = re.compile(r"<(/?)table\b[^>]*>", re.I)


def table_spans(source: str) -> list[tuple[int, int]]:
    """(start, end) of each outermost <table>...</table> inside an HTML fragment."""
    spans, depth, start = [], 0, 0
    for match in TABLE_TAG.finditer(source):
        if not match.group(1):
            if depth == 0:
                start = match.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                spans.append((start, match.end()))
    return spans
