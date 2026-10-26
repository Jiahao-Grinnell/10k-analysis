"""Explicit cross-references inside one filing: "see Note 7", "Items 7 and 8", "Exhibit 13".

Only numbered references are recognised, and only to parts the segmentation found in the
same filing (a note, an Item, an attached exhibit). This follows document structure; it
never decides whether text is relevant. Every reference found is reported with what
happened to it, so unresolved ones can be checked.

Not followed (each recorded with its reason):
- references in the front matter (cover page, table of contents);
- list entries: references that start a table cell, a line or a paragraph, in a run that
  names three or more different targets of one kind (a table of contents, an index of
  notes, an exhibit list). A reference inside a sentence or cell text ("Intangible
  assets (Note 7)") is still followed;
- a reference to the note / Item / exhibit the text itself belongs to (headings, self-mentions);
- an Item named in the same sentence as a Note ("Note 7 in Item 8"): the Item only says where the note is.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
import re

from pipeline.segment.blocks import Block

SEP = r"(?:\s|&nbsp;|&#160;|&#xa0;)+"
LIST_SEP = r"\s*(?:,\s*(?:and|or|&)?|and|or|&|through|to|[-–—])\s*"
ID = {
    "note": r"(?:\d{1,2}|[A-Z])(?![\w%]|[.,]\d)",
    "item": r"\d{1,2}[A-Ca-c]?(?![\w%]|[.,]\d)",
    "exhibit": r"\d{1,3}(?:\.\d{1,3})?(?![\d%]|[.,]\d)",
}
WORD = {"note": r"(?i:notes?)", "item": r"(?i:items?)", "exhibit": r"(?i:exhibits?)(?:" + SEP + r"(?i:no\.?))?"}
PATTERNS = {kind: re.compile(rf"\b{WORD[kind]}{SEP}(?P<ids>{ID[kind]}(?:{LIST_SEP}{ID[kind]})*)") for kind in ID}
RANGE = re.compile(r"^\s*(?:through|to|[-–—])\s*$")
LOCATOR_CHARS = 150      # an Item this close to a Note reference, in the same sentence, only locates that note
SENTENCE_END = re.compile(r"[.;:]\s+[A-Z(]")
INDEX_TARGETS = 3        # a run of list entries naming this many different targets of one kind is an index
INDEX_GAP_CHARS = 1000   # list entries of one run sit this close together
ENTRY_PREFIX = re.compile(r"(?:[ \t\xa0*_]|&nbsp;|&#160;)*\Z")  # spacing and emphasis, not line breaks
MAX_RANGE = 40
EXHIBIT_LABEL = re.compile(r"^EX-(\d{1,3})(?:\.(\d{1,3}))?", re.I)
LABEL = {"note": "Note", "item": "Item", "exhibit": "Exhibit"}


@dataclass
class Reference:
    kind: str           # note | item | exhibit
    key: str            # "7", "7A", "G", "13", "13.1"
    start: int          # position of the reference in the Markdown
    text: str           # the matched words, e.g. "Notes 3 and 7"


def _ids(kind: str, value: str) -> list[str]:
    """'3 and 7' -> ['3', '7']; '3 through 5' -> ['3', '4', '5']."""
    found = list(re.finditer(ID[kind], value))
    keys: list[str] = []
    for i, match in enumerate(found):
        key = match.group(0)
        key = key.upper() if kind == "item" else key
        if kind == "note" and i and keys[0].isdigit() != key.isdigit():
            break  # "Note 7 - A summary ...": a list keeps one numbering style
        between = value[found[i - 1].end():match.start()] if i else ""
        if i and RANGE.match(between) and keys[-1].isdigit() and key.isdigit():
            low, high = int(keys[-1]), int(key)
            if low < high <= low + MAX_RANGE:
                keys.extend(str(n) for n in range(low + 1, high + 1))
                continue
        keys.append(key)
    return keys


def find(text: str, offset: int = 0) -> list[Reference]:
    """All numbered references in `text`; positions are shifted by `offset`."""
    refs = []
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            for key in _ids(kind, match.group("ids")):
                if kind == "note" and key.isdigit() and not 1 <= int(key) <= 40:
                    continue
                if kind == "item" and int(re.match(r"\d+", key).group(0)) > 16:
                    continue
                refs.append(Reference(kind, key.lstrip("0") or key, offset + match.start(), match.group(0)))
    return sorted(refs, key=lambda r: (r.start, r.kind))  # stable: list order kept


def _exhibit_matches(label: str | None, key: str) -> bool:
    match = EXHIBIT_LABEL.match(label or "")
    if not match:
        return False
    number, _, sub = key.partition(".")
    if int(match.group(1)) != int(number):
        return False
    return not sub or (match.group(2) is not None and int(match.group(2)) == int(sub))


def _targets(kind: str, key: str, source: dict, sections: list[dict]) -> list[dict]:
    """Sections the reference points to; the source's own attached document is searched first."""
    if kind == "exhibit":
        return [s for s in sections if _exhibit_matches(s["document"], key)]
    field = "note" if kind == "note" else "item"
    matching = [s for s in sections if s[field] == key]
    same_document = [s for s in matching if s["document"] == source["document"]]
    return same_document or matching


def _is_self(kind: str, key: str, source: dict) -> bool:
    if kind == "exhibit":
        return _exhibit_matches(source["document"], key)
    return source["note" if kind == "note" else "item"] == key


def _front_matter(section: dict) -> bool:
    return section["heading_path"] in (["Front matter"],)


def _entry_start(markdown: str, position: int, floor: int) -> bool:
    """True when only spacing or emphasis separates the reference from the start of its line,
    table cell (`|`) or HTML element (`>`)."""
    before = markdown[max(floor, position - 200):position]
    before = before[:ENTRY_PREFIX.search(before).start()]
    return not before or before.endswith(("\n", "|", ">"))


def _index_entries(found: list[tuple[Reference, bool]]) -> set[int]:
    """Positions of list entries: runs of entry-start references of one kind naming
    INDEX_TARGETS or more different targets, each within INDEX_GAP_CHARS of the previous."""
    index: set[int] = set()
    for kind in LABEL:
        entries = [r for r, entry in found if entry and r.kind == kind]
        run: list[Reference] = []
        for ref in entries + [None]:
            if ref is None or (run and ref.start - run[-1].start > INDEX_GAP_CHARS):
                if len({r.key for r in run}) >= INDEX_TARGETS and len({r.start for r in run}) >= INDEX_TARGETS:
                    index.update(r.start for r in run)
                run = []
            if ref is not None:
                run.append(ref)
    return index


def resolve(markdown: str, blocks: list[Block], sections: list[dict], source_ok) -> list[dict]:
    """Find references in the blocks of sections accepted by `source_ok(section)`.

    Returns one row per reference: kind, key, text, position, source section, status
    (follow | front_matter | index | self | locator | unresolved | not_in_filing) and target
    section IDs. `not_in_filing` is an exhibit that is not attached to this filing.
    """
    starts = [s["start"] for s in sections]
    found: list[tuple[Reference, bool]] = []
    for block in blocks:
        first = bisect.bisect_right(starts, block.start) - 1
        last = bisect.bisect_left(starts, block.end) - 1
        if not any(source_ok(sections[i]) for i in range(max(first, 0), last + 1)):
            continue
        for ref in find(markdown[block.start:block.end], block.start):
            found.append((ref, _entry_start(markdown, ref.start, block.start)))
    index = _index_entries(found)
    note_positions = [r.start for r, _ in found if r.kind == "note"]
    rows: list[dict] = []
    for ref, _ in found:
        source = sections[bisect.bisect_right(starts, ref.start) - 1]
        if not source_ok(source):
            continue
        targets: list[dict] = []
        if _front_matter(source):
            status = "front_matter"
        elif _is_self(ref.kind, ref.key, source):
            status = "self"
        elif ref.start in index:
            status = "index"
        elif ref.kind == "item" and any(abs(p - ref.start) <= LOCATOR_CHARS and not SENTENCE_END.search(
                markdown, min(p, ref.start), max(p, ref.start)) for p in note_positions):
            status = "locator"
        else:
            targets = _targets(ref.kind, ref.key, source, sections)
            status = "follow" if targets else "not_in_filing" if ref.kind == "exhibit" else "unresolved"
        rows.append({"kind": ref.kind, "key": ref.key, "label": f"{LABEL[ref.kind]} {ref.key}", "text": ref.text,
                     "position": ref.start, "source_section": source["section_id"], "status": status,
                     "target_sections": [s["section_id"] for s in targets]})
    return rows
