"""Parse model answers and apply the keep rules. Failures are never dropped.

Keep rules, applied per filing after every chunk has a label:
1. a chunk labelled KEEP or UNCERTAIN (failures included) is kept (`kept_reason` "label");
2. every section such a chunk overlaps is kept whole: all chunks overlapping it are kept ("section");
3. numbered cross-references in the text kept by 1-2 ("see Note 7") keep their target note,
   Item or attached exhibit of the same filing ("reference"). One step only: text kept by a
   reference does not pull in further references.
"""
from __future__ import annotations

import bisect
import json

from pipeline.classify import crossref
from pipeline.segment.blocks import parse_blocks

RULES_VERSION = "rules-2"  # 2: section expansion and cross-references
LABELS = {"KEEP", "DROP", "UNCERTAIN"}
CATEGORIES = {"rd", "advertising_marketing", "organizational_capital", "intellectual_property"}
KEPT = {"KEEP", "UNCERTAIN"}
REFERENCE_KINDS = ("note", "item", "exhibit")


class InvalidResponse(ValueError):
    code = "invalid_response"


def parse(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidResponse(f"not JSON: {str(raw)[:120]!r}") from exc
    if not isinstance(value, dict):
        raise InvalidResponse("not a JSON object")
    label, categories, reason = value.get("label"), value.get("categories", []), value.get("reason", "")
    if label not in LABELS:
        raise InvalidResponse(f"label {label!r} not in {sorted(LABELS)}")
    if not isinstance(categories, list) or not set(categories) <= CATEGORIES:
        raise InvalidResponse(f"bad categories {categories!r}")
    if not isinstance(reason, str):
        raise InvalidResponse("reason is not text")
    return {"label": label, "categories": sorted(set(categories)), "reason": reason.strip()}


def decide(parsed: dict | None) -> tuple[str, str]:
    """(final_label, kept_by). A failed or unreadable answer becomes UNCERTAIN, so it is kept."""
    if parsed is None:
        return "UNCERTAIN", "failure"
    return parsed["label"], "model"


def _rows_by_section(rows: list[dict], sections: list[dict]) -> dict[str, list[int]]:
    """Indexes of the chunk rows that overlap each section (rows are in document order)."""
    starts = [r["start"] for r in rows]
    found = {}
    for s in sections:
        low = max(bisect.bisect_right(starts, s["start"]) - 1, 0)
        high = bisect.bisect_left(starts, s["end"])
        found[s["section_id"]] = [i for i in range(low, high) if rows[i]["start"] < s["end"] and s["start"] < rows[i]["end"]]
    return found


def apply_keep_rules(rows: list[dict], sections: list[dict], markdown: str,
                     reference_kinds=REFERENCE_KINDS) -> list[dict]:
    """Set `kept`, `kept_reason` and `kept_detail` on every label row (in place).

    Returns the cross-references found in the kept text, each with its status and, when
    followed, the chunks it newly kept.
    """
    members = _rows_by_section(rows, sections)
    for row in rows:
        labelled = row["final_label"] in KEPT
        row.update(kept=labelled, kept_reason="label" if labelled else None, kept_detail=None)
    kept_sections = [s["section_id"] for s in sections if any(rows[i]["kept"] for i in members[s["section_id"]])]
    for section_id in kept_sections:
        for i in members[section_id]:
            if not rows[i]["kept"]:
                rows[i].update(kept=True, kept_reason="section", kept_detail=f"section {section_id}")
    # Sources for references: every section with text kept by rule 1 or 2 (all its chunks are kept now).
    sources = set(kept_sections)
    references = crossref.resolve(markdown, parse_blocks(markdown), sections, lambda s: s["section_id"] in sources)
    starts = [r["start"] for r in rows]
    for ref in references:
        index = bisect.bisect_right(starts, ref["position"]) - 1
        ref["source_chunk"] = rows[index]["chunk_id"] if index >= 0 else None
        ref["newly_kept_chunks"] = []
        if ref["status"] != "follow":
            continue
        if ref["kind"] not in reference_kinds:
            ref["status"] = "kind_off"
            continue
        for section_id in ref["target_sections"]:
            for i in members[section_id]:
                if not rows[i]["kept"]:
                    rows[i].update(kept=True, kept_reason="reference",
                                   kept_detail=f"{ref['label']} referenced in {ref['source_chunk']}")
                    ref["newly_kept_chunks"].append(rows[i]["chunk_id"])
    return references
