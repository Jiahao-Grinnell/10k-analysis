from contextlib import closing
import json
import re

from keep_fixture import FILLER, build
from pipeline import config, state
from pipeline.classify.client import FakeClient
from pipeline.classify.rules import apply_keep_rules
from pipeline.segment import stage as segment_stage
from test_classify import FILING, answer, classify, excerpt, outputs, prepare, read_jsonl


def test_one_keep_chunk_keeps_its_whole_section():
    markdown, sections, rows = build([
        ({"section_id": "s1", "item": "7"}, FILLER),
        ({"section_id": "s1", "item": "7", "label": "KEEP"}, FILLER),
        ({"section_id": "s1", "item": "7"}, FILLER),
        ({"section_id": "s2", "item": "7"}, FILLER),
    ])
    apply_keep_rules(rows, sections, markdown)
    assert [(r["kept"], r["kept_reason"]) for r in rows] == [
        (True, "section"), (True, "label"), (True, "section"), (False, None)]
    assert rows[0]["kept_detail"] == "section s1" and rows[0]["final_label"] == "DROP"


def test_failures_and_uncertain_keep_their_section_too():
    markdown, sections, rows = build([
        ({"section_id": "s1", "item": "7", "label": "UNCERTAIN", "failed": True}, FILLER),
        ({"section_id": "s1", "item": "7"}, FILLER),
        ({"section_id": "s2", "item": "8", "label": "UNCERTAIN"}, FILLER),
        ({"section_id": "s2", "item": "8"}, FILLER),
        ({"section_id": "s3", "item": "9"}, FILLER),
    ])
    apply_keep_rules(rows, sections, markdown)
    assert [r["kept"] for r in rows] == [True, True, True, True, False]


def note_filing():
    return build([
        ({"section_id": "s1", "item": "7", "label": "KEEP"}, "Research spending rose; see Note 7 for details."),
        ({"section_id": "s2", "item": "8", "note": "6"}, "Note 6. Debt " + FILLER),
        ({"section_id": "s3", "item": "8", "note": "7"}, "Note 7. Intangible assets " + FILLER),
        ({"section_id": "s3", "item": "8", "note": "7"}, "Amortization continued; see also Note 9. " + FILLER),
        ({"section_id": "s4", "item": "8", "note": "9"}, "Note 9. Leases " + FILLER),
    ])


def test_see_note_7_keeps_note_7_with_the_reason_recorded():
    markdown, sections, rows = note_filing()
    refs = apply_keep_rules(rows, sections, markdown)
    assert [r["chunk_id"] for r in rows if r["kept"]] == ["c00001", "c00003", "c00004"]
    assert rows[2]["kept_reason"] == "reference" and rows[2]["kept_detail"] == "Note 7 referenced in c00001"
    followed = [r for r in refs if r["status"] == "follow"]
    assert [(r["label"], r["source_chunk"], r["newly_kept_chunks"]) for r in followed] == [
        ("Note 7", "c00001", ["c00003", "c00004"])]
    # one step only: Note 7 refers to Note 9, which stays dropped
    assert not rows[4]["kept"]


def test_a_reference_kind_can_be_switched_off():
    markdown, sections, rows = note_filing()
    refs = apply_keep_rules(rows, sections, markdown, reference_kinds=("item", "exhibit"))
    assert [r["chunk_id"] for r in rows if r["kept"]] == ["c00001"]
    assert [r["status"] for r in refs if r["label"] == "Note 7"] == ["kind_off"]


def test_model_input_has_heading_path_and_previous_text(small_corpus):
    filing = prepare(small_corpus)[FILING]
    client = FakeClient(lambda m: answer("DROP"))
    classify(small_corpus, [filing], client)
    chunks = segment_stage.load_chunks(segment_stage.paths(small_corpus, filing)[0])
    by_text = {}
    markdown = segment_stage.read_markdown(filing.markdown_path)
    for messages in client.calls:
        by_text[excerpt(messages)] = messages[1]["content"]
    second = next(v for k, v in by_text.items() if k == markdown[chunks[1]["start"]:chunks[1]["end"]])
    assert "Location: " + " > ".join(chunks[1]["heading_path"]) in second
    preceding = re.search(r"Preceding text \(context only, do not label it\):\n<<<\n(.*?)\n>>>", second, re.S).group(1)
    assert markdown[chunks[0]["start"]:chunks[0]["end"]].endswith(preceding)


def test_rules_change_reruns_classification_only_without_model_requests(small_corpus):
    filing = prepare(small_corpus)[FILING]
    classify(small_corpus, [filing], FakeClient(lambda m: answer("KEEP")))
    requests_before = outputs(small_corpus, filing)["requests"].read_bytes()

    text = small_corpus.path.read_text(encoding="utf-8") + '[keep]\nreference_kinds = ["note"]\n'
    small_corpus.path.write_text(text, encoding="utf-8")
    edited = config.load(small_corpus.path)
    with closing(state.connect(edited.db_path)) as db:
        assert segment_stage.run(edited, db, [filing], log=lambda *_: None)["counts"] == {"already_done": 1}
    again = FakeClient(lambda m: answer("DROP"))  # would change every label if it were asked
    assert classify(edited, [filing], again)["counts"] == {"classified": 1}
    assert again.calls == []
    summary = json.loads(outputs(edited, filing)["summary"].read_text(encoding="utf-8"))
    assert summary["model_answers_reused"] is True and summary["final_labels"] == {"KEEP": summary["chunks"]}
    assert outputs(edited, filing)["requests"].read_bytes() == requests_before
    assert classify(edited, [filing], again)["counts"] == {"already_done": 1}


def test_labels_record_every_chunk_once_with_its_keep_decision(small_corpus):
    filing = prepare(small_corpus)[FILING]
    classify(small_corpus, [filing], FakeClient(lambda m: answer("DROP")))
    rows = read_jsonl(outputs(small_corpus, filing)["labels"])
    chunks = segment_stage.load_chunks(segment_stage.paths(small_corpus, filing)[0])
    assert [r["chunk_id"] for r in rows] == [c["chunk_id"] for c in chunks]
    assert all(r["kept"] is False and r["kept_reason"] is None for r in rows)
    assert outputs(small_corpus, filing)["references"].is_file()
