from keep_fixture import FILLER, build
from pipeline.classify import crossref
from pipeline.classify.rules import apply_keep_rules
from pipeline.segment.blocks import parse_blocks


def keys(text):
    return [(r.kind, r.key) for r in crossref.find(text)]


def test_numbered_references_are_found():
    assert keys("See Note 7 to the consolidated financial statements.") == [("note", "7")]
    assert keys("as described in Notes 3 and 12.") == [("note", "3"), ("note", "12")]
    assert keys("see Notes 3 through 5") == [("note", "3"), ("note", "4"), ("note", "5")]
    assert keys("refer to NOTE G - Intangibles") == [("note", "G")]
    assert keys("In Part II, Items 7 and 7A of this report") == [("item", "7"), ("item", "7A")]
    assert keys("filed as Exhibit 13.1 hereto") == [("exhibit", "13.1")]
    assert keys("incorporated by reference to Exhibit 13.") == [("exhibit", "13")]


def test_debt_notes_percentages_and_years_are_not_references():
    assert keys("The 6.5% Notes due 2030 were issued at par.") == []
    assert keys("Senior Notes 2028 and Notes 7.25% were repaid.") == []
    assert keys("See note 7.5 million shares.") == []
    assert keys("Item 99 is not an Item.") == []
    assert keys("See Note 7 - A summary of policies") == [("note", "7")]


def resolve(pieces, kept_sections=None):
    markdown, sections, _ = build(pieces)
    chosen = set(kept_sections or [s["section_id"] for s in sections])
    return crossref.resolve(markdown, parse_blocks(markdown), sections, lambda s: s["section_id"] in chosen)


def test_each_reference_gets_a_recorded_status():
    rows = resolve([
        ({"section_id": "s1", "path": ["Front matter"], "part": None}, "Item 7 page 20. Item 8 page 30. See Note 3."),
        ({"section_id": "s2", "item": "7"}, "Item 7. Management's Discussion\n\n" + FILLER),
        ({"section_id": "s2", "item": "7"}, "We refer to Item 7 above and to Item 1A. See Note 3, in Item 8, and Note 12."),
        ({"section_id": "s3", "item": "7"},
         "| Item 1 | Item 2 | Item 3 |\n|---|---|---|\n| Business | Properties | Legal |"),
        ({"section_id": "s4", "item": "8", "note": "3", "path": ["PART II", "Item 8", "Note 3"]}, "Note 3. Intangibles " + FILLER),
        ({"section_id": "s5", "item": "1A", "path": ["PART I", "Item 1A"]}, FILLER),
    ])
    status = {(r["label"], r["source_section"]): r["status"] for r in rows}
    assert status[("Item 7", "s1")] == "front_matter" and status[("Note 3", "s1")] == "front_matter"
    assert status[("Item 7", "s2")] == "self"
    assert status[("Item 1A", "s2")] == "follow"
    assert status[("Note 3", "s2")] == "follow"
    assert status[("Item 8", "s2")] == "locator"  # names where Note 3 is, not a reference to all of Item 8
    assert status[("Note 12", "s2")] == "unresolved"
    assert {status[(f"Item {n}", "s3")] for n in (1, 2, 3)} == {"index"}
    assert next(r for r in rows if r["label"] == "Note 3" and r["source_section"] == "s2")["target_sections"] == ["s4"]


def test_references_in_sections_that_are_not_kept_are_ignored():
    rows = resolve([({"section_id": "s1", "item": "7"}, "See Note 3."),
                    ({"section_id": "s2", "item": "8", "note": "3"}, "Note 3. " + FILLER)], kept_sections=["s2"])
    assert rows == [{"kind": "note", "key": "3", "label": "Note 3", "text": "Note 3", "position": rows[0]["position"],
                     "source_section": "s2", "status": "self", "target_sections": []}]


def test_exhibit_reference_keeps_the_attached_exhibit():
    markdown, sections, rows = build([
        ({"section_id": "s1", "item": "8", "label": "KEEP"}, "Financial statements are in Exhibit 13."),
        ({"section_id": "s2", "item": "9"}, FILLER),
        ({"section_id": "s3", "document": "EX-13 — Annual report", "item": None,
          "path": ["Attached document: EX-13 — Annual report"]}, "Annual report text " + FILLER),
        ({"section_id": "s4", "document": "EX-21 — Subsidiaries", "item": None,
          "path": ["Attached document: EX-21 — Subsidiaries"]}, FILLER),
    ])
    refs = apply_keep_rules(rows, sections, markdown)
    assert [(r["label"], r["status"], r["target_sections"]) for r in refs] == [("Exhibit 13", "follow", ["s3"])]
    assert [r["chunk_id"] for r in rows if r["kept"]] == ["c00001", "c00003"]
    assert rows[2]["kept_reason"] == "reference" and rows[2]["kept_detail"] == "Exhibit 13 referenced in c00001"


def test_list_entries_are_an_index_but_references_inside_text_are_followed():
    notes = [({"section_id": f"n{n}", "item": "8", "note": str(n), "path": ["PART II", "Item 8", f"Note {n}"]},
              f"Note {n}. Title\n\n" + FILLER) for n in (3, 4, 7)]
    rows = resolve([
        ({"section_id": "s1", "item": "8"},  # statement rows: the note reference sits inside the row label
         "| | 2020 |\n|---|---|\n| Inventories (Note 3) | 10 |\n| Leases (see Note 4) | 20 |\n"
         "| Intangible assets (Note 7) | 30 |"),
        ({"section_id": "s2", "item": "8"},  # an index of notes: each entry starts its cell
         "| Note 3 | Inventories | 50 |\n|---|---|---|\n| Note 4 | Leases | 52 |\n| Note 7 | Intangibles | 55 |"),
        ({"section_id": "s3", "item": "15"},  # a fixed-width exhibit list
         "```text\n   Exhibit 3.1   Articles\n   Exhibit 10.1  Plan\n   Exhibit 13    Annual report\n```"),
        ({"section_id": "s4", "item": "15"}, "The plan in Exhibit 10.2 is incorporated by reference to the 1993 report."),
    ] + notes)
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source_section"], []).append((r["label"], r["status"]))
    assert by_source["s1"] == [("Note 3", "follow"), ("Note 4", "follow"), ("Note 7", "follow")]
    assert {status for _, status in by_source["s2"]} == {"index"}
    assert {status for _, status in by_source["s3"]} == {"index"}
    assert by_source["s4"] == [("Exhibit 10.2", "not_in_filing")]


def test_a_list_inside_one_sentence_is_not_an_index():
    rows = resolve([
        ({"section_id": "s1", "item": "7"}, "Notes 3, 4 and 7 describe the assets."),
    ] + [({"section_id": f"n{n}", "item": "8", "note": str(n)}, f"Note {n}. " + FILLER) for n in (3, 4, 7)],
        kept_sections=["s1"])
    assert [(r["label"], r["status"]) for r in rows] == [("Note 3", "follow"), ("Note 4", "follow"), ("Note 7", "follow")]
