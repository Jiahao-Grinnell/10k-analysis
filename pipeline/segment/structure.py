"""Document structure: attached documents → Part → Item → Note, from the converter's Markdown.

Only structural signals are used: where a heading-like unit stands (its own short
paragraph, a tiny heading table, or a line that starts a paragraph in fixed-width
text), its numbering, and its position in the sequence. Nothing here decides
relevance. Tables of contents, cross-references in prose and repeated page headers
("Item 1. Business - Continued") never become boundaries.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
import html
import re

from pipeline.segment.blocks import Block

MAX_HEADING_CHARS = 220          # a heading unit is short
TOC_GAP_CHARS = 400              # TOC entries sit this close together
ATTACHED = re.compile(r"^#\s+Attached document:\s*(.+?)\s*$")
TAG = re.compile(r"<[^>]+>")
ESCAPE = re.compile(r"\\(.)")
SPACE = re.compile(r"\s+")
ITEM = re.compile(r"^items?\s*(\d{1,2})\s*([a-c])?(?![\d])\s*(?:(?:,|and|&|through|-|–|—)\s*(?:items?\s*)?\d{1,2}[a-c]?\s*)*"
                  r"(?P<sep>[.:\-–—)]|\s{2,}|$|\s+(?=(?-i:[A-Z(\[\"“'])))", re.I)  # a title starts with a real capital:
                                                                                  # "Item 8 of this Form 10-K" is prose
PART = re.compile(r"^part\s+(iv|i{1,3})\b\s*(?:[.:\-–—]\s*.*|\s+[A-Z].*)?$", re.I)
NOTE_WORD = re.compile(r"^note\s+(\d{1,2})\b\s*(?:[.:\-–—)]|\s|$)", re.I)
SUBSECTION_MAX_CHARS = 100
SUBSECTION_MAX_WORDS = 14
MIN_SECTION_CHARS = 200          # headings closer than this to the previous boundary belong to it
MIN_NOTE_GAP_CHARS = 800         # median distance between real notes; shorter means a numbered list
NOTE_NUMBER = re.compile(r"^(\d{1,2})\s*[.)]\s+([A-Z][^.]{2,110})$")
PAGE_NUMBER_END = re.compile(r"\s(?:\d{1,3}|[ivx]{1,5})$", re.I)
CONTINUED = re.compile(r"\bcontinued\b|\(cont(?:'d|inued)?\.?\)", re.I)


def plain(value: str) -> str:
    """Visible text of a Markdown unit: no tags, escapes, emphasis, pipes or table rules."""
    value = re.sub(r"(?m)^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*$", " ", value)  # pipe-table divider rows
    value = TAG.sub(" ", value)
    value = ESCAPE.sub(r"\1", value)
    value = value.replace("**", " ").replace("|", " ")
    value = re.sub(r"(?m)^\s*#+\s*", "", value)
    return SPACE.sub(" ", html.unescape(value).replace(" ", " ")).strip()


@dataclass
class Unit:
    """A heading candidate position: a whole block, or one line inside a fenced block."""
    start: int
    text: str           # plain text
    standalone: bool    # its own short paragraph / heading table / paragraph-starting line
    in_table: bool      # inside a multi-row table (cannot be a heading)
    table_id: int | None = None
    next_line: str | None = None  # fixed-width text only: the following line ("" if blank)
    raw_line: str | None = None   # fixed-width text only: the line itself, spacing intact


@dataclass
class Boundary:
    start: int
    level: str          # document | part | item | note | subsection
    key: str            # e.g. "II", "7A", "4", or the document label
    title: str
    ordinal: int = 0


@dataclass
class Section:
    section_id: str
    start: int
    end: int
    heading_path: list[str]
    part: str | None = None
    item: str | None = None
    note: str | None = None
    document: str | None = None
    group: str = ""                 # enclosing hard unit (document|part|item|note); chunks never cross it
    subsection: str | None = None


@dataclass
class Structure:
    sections: list[Section]
    boundaries: list[Boundary]
    quality: str
    info: dict = field(default_factory=dict)


def item_ordinal(number: str, letter: str | None) -> int:
    return int(number) * 10 + (" abc".index(letter.lower()) if letter else 0)


def _units(text: str, blocks: list[Block]) -> list[Unit]:
    units: list[Unit] = []
    for index, block in enumerate(blocks):
        body = text[block.start:block.end]
        if block.kind == "fenced":
            offset, previous_blank = block.start, True
            lines = body.splitlines(keepends=True)
            for position, line in enumerate(lines):
                stripped = line.strip()
                if stripped and not stripped.startswith("```"):
                    shown = plain(stripped)
                    # A heading line starts a paragraph (blank line before) or is written in capitals.
                    standalone = len(shown) <= MAX_HEADING_CHARS and (previous_blank or shown.isupper())
                    following = lines[position + 1].rstrip("\r\n") if position + 1 < len(lines) else ""
                    units.append(Unit(offset + (len(line) - len(line.lstrip())), shown, standalone, False,
                                      next_line=following, raw_line=line.rstrip("\r\n")))
                previous_blank = not stripped
                offset += len(line)
            continue
        shown = plain(body)
        if block.kind in {"paragraph", "heading"}:
            units.append(Unit(block.start, shown, len(shown) <= MAX_HEADING_CHARS, False))
        elif block.kind == "pipe_table":
            rows = [r for r in body.splitlines() if r.strip() and not re.fullmatch(r"\|?[\s|:-]*\|?", r.strip())]
            small = len(rows) <= 2 and len(shown) <= MAX_HEADING_CHARS
            units.append(Unit(block.start, shown, small, not small, table_id=index))
            if not small:  # rows of a larger table: candidates only for TOC detection
                units.extend(Unit(block.start, plain(r), False, True, table_id=index) for r in rows)
        elif block.kind == "html":
            units.extend(_html_units(body, block.start, index))
    return units


HTML_TAG = re.compile(r"<(/?)(table|tr|p|div|h[1-6]|li|br)\b[^>]*>", re.I)


def _html_units(body: str, offset: int, block_index: int) -> list[Unit]:
    """Heading candidates inside an HTML block.

    Tables: a table whose rows with text number at most one (spacer rows are common) is a
    heading table; rows of bigger tables are kept only for table-of-contents detection.
    Outside tables, the text of each paragraph-level element is its own unit. This also
    covers filings whose whole body is one minimal-HTML block (converter fallback).
    """
    units: list[Unit] = []
    tables: list[tuple[int, list[tuple[int, str]]]] = []  # (start, [(row start, row text)]) for open tables
    depth = 0
    piece_start: int | None = None

    def flush_piece(end: int) -> None:
        nonlocal piece_start
        if piece_start is not None and depth == 0:
            shown = plain(body[piece_start:end])
            if shown:
                units.append(Unit(offset + piece_start, shown, len(shown) <= MAX_HEADING_CHARS, False))
        piece_start = None

    for tag in HTML_TAG.finditer(body):
        closing, name = tag.group(1) == "/", tag.group(2).lower()
        if name == "table":
            if not closing:
                flush_piece(tag.start())
                depth += 1
                tables.append((tag.start(), []))
            elif tables:
                start, rows = tables.pop()
                depth = max(0, depth - 1)
                text_rows = [(s, t) for s, t in rows if t]
                table_id = block_index * 100000 + start
                whole = plain(body[start:tag.end()])
                if len(text_rows) <= 1 and whole and len(whole) <= MAX_HEADING_CHARS and depth == 0:
                    units.append(Unit(offset + start, whole, True, False, table_id=table_id))
                else:
                    units.extend(Unit(offset + s, t, False, True, table_id=table_id) for s, t in text_rows)
            continue
        if name == "tr" and tables and not closing:
            # row text runs to the next row or the end of the table; filled in when the row closes
            tables[-1][1].append((tag.start(), None))
            continue
        if name == "tr" and tables and closing and tables[-1][1] and tables[-1][1][-1][1] is None:
            row_start = tables[-1][1][-1][0]
            tables[-1][1][-1] = (row_start, plain(body[row_start:tag.end()]))
            continue
        if depth == 0:
            flush_piece(tag.start())
            if not closing:  # an opening block tag or a <br> line break starts the next piece of text
                piece_start = tag.end()
    flush_piece(len(body))
    return units


def _documents(text: str, blocks: list[Block]) -> list[tuple[int, int, str | None]]:
    """(start, end, label) for the main report (label None) and each attached document."""
    starts = [(0, None)]
    for block in blocks:
        if block.kind == "heading":
            match = ATTACHED.match(text[block.start:block.end].strip())
            if match:
                starts.append((block.start, plain(match.group(1))))
    return [(s, starts[i + 1][0] if i + 1 < len(starts) else len(text), label) for i, (s, label) in enumerate(starts)]


def _item_candidates(units: list[Unit]) -> list[tuple[Unit, str, int]]:
    found = []
    for unit in units:
        match = ITEM.match(unit.text)
        if not match:
            continue
        key = match.group(1).lstrip("0") + (match.group(2) or "").upper()
        if not key or int(match.group(1)) > 16:
            continue
        found.append((unit, key, item_ordinal(match.group(1), match.group(2))))
    return found


def _toc_units(items: list[tuple[Unit, str, int]]) -> set[int]:
    """Unit starts that belong to a table of contents.

    1. Any table holding Item references to three or more different Items.
    2. A run of Item lines that sit very close together and cover both an early Item (< 5)
       and a late one (>= 7): real Item sections 1 to 7 are never that short.
    3. A short Item line that ends in a page number.
    """
    toc: set[int] = set()
    by_table: dict[int, set[str]] = {}
    for unit, key, _ in items:
        if unit.table_id is not None and unit.in_table:
            by_table.setdefault(unit.table_id, set()).add(key)
    toc_tables = {t for t, keys in by_table.items() if len(keys) >= 3}
    for unit, key, _ in items:
        if unit.table_id in toc_tables:
            toc.add(unit.start)
        if PAGE_NUMBER_END.search(unit.text) and len(unit.text) < 140 and unit.standalone:
            toc.add(unit.start)
    run: list[tuple[Unit, str, int]] = []

    def close(run):
        ordinals = [o for _, _, o in run]
        if len(run) >= 4 and min(ordinals) < 50 and max(ordinals) >= 70:
            toc.update(u.start for u, _, _ in run)

    for item in items:
        # A TOC lists Items in increasing order; a gap or a step back (body starts again at Item 1) ends it.
        if run and (item[0].start - run[-1][0].start > TOC_GAP_CHARS or item[2] <= run[-1][2]):
            close(run)
            run = []
        run.append(item)
    close(run)
    return toc


def _increasing_chain(candidates: list[tuple[Unit, str, int]]) -> list[tuple[Unit, str, int]]:
    """Longest chain with strictly increasing ordinals (earliest positions win ties)."""
    if not candidates:
        return []
    best = [1] * len(candidates)
    previous = [-1] * len(candidates)
    for i in range(len(candidates)):
        for j in range(i):
            if candidates[j][2] < candidates[i][2] and best[j] + 1 > best[i]:
                best[i], previous[i] = best[j] + 1, j
    end = max(range(len(candidates)), key=lambda i: (best[i], -i))
    chain = []
    while end != -1:
        chain.append(candidates[end])
        end = previous[end]
    return chain[::-1]


def _items(units: list[Unit]) -> tuple[list[Boundary], dict]:
    candidates = [c for c in _item_candidates(units) if not c[0].in_table or c[0].standalone]
    all_items = _item_candidates(units)
    toc = _toc_units(all_items)
    usable = [c for c in candidates if c[0].standalone and c[0].start not in toc and not CONTINUED.search(c[0].text)]
    first_seen: dict[str, tuple[Unit, str, int]] = {}
    for candidate in usable:  # repeated page headers: the first occurrence starts the section
        first_seen.setdefault(candidate[1], candidate)
    ordered = sorted(first_seen.values(), key=lambda c: c[0].start)
    chain = _increasing_chain(ordered)
    boundaries = [Boundary(u.start, "item", key, u.text[:120], ordinal) for u, key, ordinal in chain]
    return boundaries, {"item_candidates": len(all_items), "toc_entries": len(toc), "items": [b.key for b in boundaries]}


def _parts(units: list[Unit], items: list[Boundary]) -> list[Boundary]:
    """PART headings that sit in order and before the Items they contain."""
    found = []
    for unit in units:
        if unit.standalone and not unit.in_table and len(unit.text) <= 120 and not CONTINUED.search(unit.text):
            match = PART.match(unit.text)
            if match:
                found.append((unit, match.group(1).upper(), ["I", "II", "III", "IV"].index(match.group(1).upper()) + 1))
    first_item = items[0].start if items else None
    # Drop TOC parts: those before the first chosen Item that are followed by another PART within the TOC span.
    if first_item is not None:
        before = [f for f in found if f[0].start < first_item]
        found = ([before[-1]] if before else []) + [f for f in found if f[0].start >= first_item]
    first: dict[str, tuple] = {}
    for f in found:
        first.setdefault(f[1], f)
    chain = _increasing_chain(sorted(first.values(), key=lambda f: f[0].start))
    return [Boundary(u.start, "part", key, f"PART {key}", ordinal) for u, key, ordinal in chain]


def _notes(units: list[Unit], start: int, end: int) -> list[Boundary]:
    """Note headings inside [start, end): 'Note 4 ...' or '4. Title', numbered in order from 1.
    A bare 'NOTE 4' takes the following short unit as its title."""
    found = []
    for position, unit in enumerate(units):
        if not (start <= unit.start < end) or not unit.standalone or unit.in_table or len(unit.text) > 140:
            continue
        if re.fullmatch(r"note\s+\d{1,2}\s*[.:]?", unit.text, re.I) and position + 1 < len(units):
            following = units[position + 1]
            if following.standalone and len(following.text) <= SUBSECTION_MAX_CHARS:
                unit = Unit(unit.start, f"{unit.text} {following.text}", True, False)
        if CONTINUED.search(unit.text) or PAGE_NUMBER_END.search(unit.text):
            continue
        match = NOTE_WORD.match(unit.text)
        number = int(match.group(1)) if match else None
        if number is None:
            numbered = NOTE_NUMBER.match(unit.text)
            if numbered and not numbered.group(2).rstrip().endswith((".", ",", ";")):
                number = int(numbered.group(1))
        if number is not None and 1 <= number <= 40:
            found.append((unit, str(number), number))
    first: dict[str, tuple] = {}
    for f in found:
        first.setdefault(f[1], f)
    chain = _increasing_chain(sorted(first.values(), key=lambda f: f[0].start))
    if len(chain) < 3 or chain[0][2] > 2:  # a real notes sequence starts at 1 (or 2) and has several notes
        return []
    gaps = sorted(b[0].start - a[0].start for a, b in zip(chain, chain[1:]))
    if gaps[len(gaps) // 2] < MIN_NOTE_GAP_CHARS:  # a short numbered list (e.g. "1. Financial Statements
        return []                                  # 2. Schedules 3. Exhibits"), not notes with bodies
    return [Boundary(u.start, "note", key, u.text[:120], ordinal) for u, key, ordinal in chain]


def _furniture(units: list[Unit]) -> set[str]:
    """Short texts repeated three or more times (page headers/footers, unit lines): never headings."""
    seen: dict[str, int] = {}
    for unit in units:
        if unit.standalone and len(unit.text) <= SUBSECTION_MAX_CHARS:
            key = re.sub(r"\d+", "#", unit.text.lower())
            seen[key] = seen.get(key, 0) + 1
    return {k for k, n in seen.items() if n >= 3}


def _looks_like_title(text: str, fixed_width: bool) -> bool:
    words = text.split()
    if not (3 <= len(text) <= SUBSECTION_MAX_CHARS and 1 <= len(words) <= SUBSECTION_MAX_WORDS):
        return False
    if not text[0].isupper() or text.rstrip()[-1] in ".,;:" or text.startswith(("(", "*", "See ", "Amounts in")):
        return False
    if fixed_width and (re.search(r"\d", text) or "  " in text):  # aligned table rows in legacy text
        return False
    if re.search(r"\$|%|\d{3,}", text):  # amounts: a table row or a caption, not a heading
        return False
    long_words = [w for w in words if len(w) > 3 and w[0].isalpha()]
    return text.isupper() or not long_words or sum(w[0].isupper() for w in long_words) >= 0.6 * len(long_words)


TABULAR = re.compile(r"\d\s{2,}\S|\S\s{2,}[$(]?\d")
COLUMN_GAP = re.compile(r"\S(?: {3,}|\t)\S")


def _fixed_width_heading(unit: Unit) -> bool:
    """In fixed-width text a heading is a short line of its own: not the first line of a wrapped
    paragraph (those run close to the full line width and continue on the next line), and not part
    of a table (a column header or row: wide gaps between columns, or figures on the next line)."""
    following = (unit.next_line or "").rstrip()
    own = (unit.raw_line or unit.text).strip()
    if len(unit.text) > 60 or TABULAR.search(following) or TABULAR.search(own):
        return False
    if COLUMN_GAP.search(own) or COLUMN_GAP.search(following.strip()):
        return False
    return not following.strip() or len(unit.text) <= 0.6 * len(following.strip())


def _subsections(units: list[Unit], hard: list[Boundary], start: int, end: int, fixed_width: set[int]) -> list[Boundary]:
    """Title-like standalone units inside [start, end), at least MIN_SECTION_CHARS after the previous boundary."""
    furniture = _furniture(units)
    hard_starts = sorted(b.start for b in hard)
    found: list[Boundary] = []
    last = start
    for unit in units:
        if not (start <= unit.start < end) or not unit.standalone or unit.in_table:
            continue
        i = bisect.bisect_right(hard_starts, unit.start) - 1
        near_hard = i >= 0 and unit.start < hard_starts[i] + MIN_SECTION_CHARS
        if near_hard or unit.start - last < MIN_SECTION_CHARS:
            continue
        if re.sub(r"\d+", "#", unit.text.lower()) in furniture or ITEM.match(unit.text) or PART.match(unit.text):
            continue
        if unit.start in fixed_width and not _fixed_width_heading(unit):
            continue
        if _looks_like_title(unit.text, unit.start in fixed_width):
            found.append(Boundary(unit.start, "subsection", "", unit.text[:120]))
            last = unit.start
    return found


def analyze(text: str, blocks: list[Block], form: str = "") -> Structure:
    """`form` decides which Item holds the financial statements (10-KSB: Item 7, others: Item 8)."""
    statements_item = "7" if form.upper().startswith("10-KSB") else "8"
    units = _units(text, blocks)
    fences = [(b.start, b.end) for b in blocks if b.kind == "fenced"]
    fence_starts = [s for s, _ in fences]
    fixed_width = set()
    for unit in units:
        i = bisect.bisect_right(fence_starts, unit.start) - 1
        if i >= 0 and unit.start < fences[i][1]:
            fixed_width.add(unit.start)
    boundaries: list[Boundary] = []
    info: dict = {"documents": []}
    item_keys: list[str] = []
    for doc_start, doc_end, label in _documents(text, blocks):
        doc_units = [u for u in units if doc_start <= u.start < doc_end]
        if label is not None:
            boundaries.append(Boundary(doc_start, "document", label, f"Attached document: {label}"))
        items, item_info = _items(doc_units)
        parts = _parts(doc_units, items)
        notes: list[Boundary] = []
        statements = next((i for i, b in enumerate(items) if b.key == statements_item), None)
        if statements is not None:
            stop = items[statements + 1].start if statements + 1 < len(items) else doc_end
            notes = _notes(doc_units, items[statements].start, stop)
            if not notes:  # statements may follow the last Item ("F-pages") with Item 8 only pointing to them
                notes = _notes(doc_units, items[statements].start, doc_end)
        elif label is not None and label.upper().startswith("EX-13"):
            notes = _notes(doc_units, doc_start, doc_end)
        elif not items and label is None:
            notes = _notes(doc_units, doc_start, doc_end)
        hard = parts + items + notes
        main_or_annual = label is None or label.upper().startswith("EX-13")
        # The cover page and table of contents before the first Part/Item stay one "Front matter" section.
        body_start = min((b.start for b in parts + items), default=doc_start)
        subsections = _subsections(doc_units, hard, body_start, doc_end, fixed_width) if main_or_annual else []
        boundaries += hard + subsections
        if label is None:
            item_keys = [b.key for b in items]
        info["documents"].append({"label": label or "main", **item_info, "parts": [b.key for b in parts],
                                  "notes": len(notes), "subsections": len(subsections)})
    boundaries.sort(key=lambda b: (b.start, LEVELS.index(b.level)))
    sections = _sections(text, boundaries)
    distinct = len(set(item_keys))
    quality = "HIGH" if distinct >= 8 else "MEDIUM" if distinct >= 3 else "LOW"
    info["quality"] = quality
    return Structure(sections, boundaries, quality, info)


LEVELS = ["document", "part", "item", "note", "subsection"]


def _sections(text: str, boundaries: list[Boundary]) -> list[Section]:
    """Contiguous sections covering the whole text; a boundary at the same start as another merges into it.

    Each boundary resets the levels below it (a new Item clears the note and subsection).
    `group` identifies the enclosing hard unit (document/part/item/note); chunks never cross it.
    """
    sections: list[Section] = []
    keys = dict.fromkeys(LEVELS)
    titles = dict.fromkeys(LEVELS)
    starts: list[tuple[int, dict, dict]] = [(0, dict(keys), dict(titles))]
    for boundary in boundaries:
        depth = LEVELS.index(boundary.level)
        for level in LEVELS[depth:]:
            keys[level] = titles[level] = None
        if boundary.level == "document":
            keys = dict.fromkeys(LEVELS)
            titles = dict.fromkeys(LEVELS)
        keys[boundary.level] = boundary.key or boundary.title
        titles[boundary.level] = boundary.title
        if starts and starts[-1][0] == boundary.start:
            starts[-1] = (boundary.start, dict(keys), dict(titles))
        else:
            starts.append((boundary.start, dict(keys), dict(titles)))
    for index, (start, k, names) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        if end <= start:
            continue
        path = [names[level] for level in LEVELS if names[level]]
        if not path:
            path = ["Front matter"] if len(starts) > 1 else ["UNSTRUCTURED"]
        section = Section(f"s{len(sections) + 1:04d}", start, end, path, k["part"], k["item"], k["note"], k["document"])
        section.group = "|".join(str(k[level]) for level in LEVELS[:4])
        section.subsection = names["subsection"]
        sections.append(section)
    return sections
