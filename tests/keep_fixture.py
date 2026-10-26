"""A hand-built filing for keep-rule tests: Markdown, sections and one chunk row per piece."""
from __future__ import annotations

FILLER = "The company describes its operations in this paragraph without any figures at all. " * 3


def build(pieces: list[tuple[dict, str]]):
    """pieces: (section fields, text). Consecutive pieces with the same section_id share a section;
    every piece is one chunk. Returns (markdown, sections, rows)."""
    markdown, sections, rows = "", [], []
    for number, (meta, text) in enumerate(pieces, 1):
        text = text.rstrip("\n") + "\n\n"
        start, end = len(markdown), len(markdown) + len(text)
        markdown += text
        if sections and sections[-1]["section_id"] == meta["section_id"]:
            sections[-1]["end"] = end
        else:
            sections.append({"section_id": meta["section_id"], "start": start, "end": end,
                             "heading_path": meta.get("path", ["PART II", f"Item {meta.get('item')}"]),
                             "part": meta.get("part", "II"), "item": meta.get("item"), "note": meta.get("note"),
                             "document": meta.get("document"), "group": "", "subsection": None})
        rows.append({"chunk_id": f"c{number:05d}", "section_id": meta["section_id"], "section_ids": [meta["section_id"]],
                     "start": start, "end": end, "tokens": len(text) // 4, "item": meta.get("item"),
                     "document": meta.get("document"), "heading_path": sections[-1]["heading_path"],
                     "final_label": meta.get("label", "DROP"), "model_label": meta.get("label", "DROP"),
                     "kept_by": "failure" if meta.get("failed") else "model"})
    return markdown, sections, rows
