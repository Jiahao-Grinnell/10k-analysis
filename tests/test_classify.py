from contextlib import closing
import json
import re
import shutil

import pytest

from conftest import TinyTokenizer
from pipeline import config, convert_stage, corpus, state
from pipeline.classify import stage as classify_stage
from pipeline.classify.client import ClientError, FakeClient
from pipeline.segment import stage as segment_stage
from pipeline.segment.chunks import model_text

FILING = "AAA_Alpha_Inc/2020_a"


def prepare(cfg):
    filings = corpus.list_filings(cfg.raw_dir, cfg.markdown_dir)
    with closing(state.connect(cfg.db_path)) as db:
        convert_stage.run(cfg, db, filings, tokenizers={"o200k_base": TinyTokenizer()}, log=lambda *_: None)
        segment_stage.run(cfg, db, filings, log=lambda *_: None)
    return {f.filing_id: f for f in filings}


def classify(cfg, filings, client, **kwargs):
    with closing(state.connect(cfg.db_path)) as db:
        return classify_stage.run(cfg, db, filings, client, log=lambda *_: None, **kwargs)


def outputs(cfg, filing, tag="fake"):
    return classify_stage.output_paths(cfg, tag, filing)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def excerpt(messages):
    return re.search(r"Excerpt to label:\n<<<\n(.*)\n>>>$", messages[1]["content"], re.S).group(1)


def answer(label):
    return json.dumps({"reason": "test", "label": label, "categories": []})


def test_failures_become_uncertain_and_are_kept(small_corpus):
    filings = prepare(small_corpus)
    filing = filings[FILING]
    chunks = segment_stage.load_chunks(segment_stage.paths(small_corpus, filing)[0])
    assert len(chunks) >= 3
    markdown = segment_stage.read_markdown(filing.markdown_path)
    texts = [model_text(markdown, c) for c in chunks]

    def respond(messages):
        index = texts.index(excerpt(messages))
        if index == 0:
            raise ClientError("timeout", "simulated")
        if index == 1:
            return "not json"
        return answer("DROP")

    classify(small_corpus, [filing], FakeClient(respond))
    rows = read_jsonl(outputs(small_corpus, filing)["labels"])
    assert len(rows) == len(chunks) and [r["chunk_id"] for r in rows] == [c["chunk_id"] for c in chunks]
    assert rows[0]["final_label"] == "UNCERTAIN" and rows[0]["kept_by"] == "failure" and "timeout" in rows[0]["error"]
    assert rows[1]["final_label"] == "UNCERTAIN" and "invalid_response" in rows[1]["error"]
    assert all(r["final_label"] == "DROP" and r["kept_by"] == "model" for r in rows[2:])
    requests = read_jsonl(outputs(small_corpus, filing)["requests"])
    assert sum(r["chunk_ids"] == [chunks[0]["chunk_id"]] for r in requests) == 2  # one retry


def test_each_request_contains_one_filing_only(small_corpus):
    filings = prepare(small_corpus)
    chosen = [filings[FILING], filings["AAA_Alpha_Inc/1998_b"]]
    client = FakeClient(lambda m: answer("KEEP"))
    classify(small_corpus, chosen, client)
    texts = {}
    for filing in chosen:
        markdown = segment_stage.read_markdown(filing.markdown_path)
        chunks = segment_stage.load_chunks(segment_stage.paths(small_corpus, filing)[0])
        texts[filing.filing_id] = {model_text(markdown, c) for c in chunks}
    for messages in client.calls:
        named = re.search(r"^Filing: (\S+)", messages[1]["content"]).group(1)
        assert excerpt(messages) in texts[named]
        other = next(f for f in texts if f != named)
        assert other not in messages[1]["content"]
    for filing in chosen:
        assert {r["filing_id"] for r in read_jsonl(outputs(small_corpus, filing)["requests"])} == {filing.filing_id}


def test_kept_text_is_verbatim_and_in_order(small_corpus):
    filings = prepare(small_corpus)
    filing = filings[FILING]
    labels = iter(["KEEP", "DROP", "UNCERTAIN"] * 50)
    order = {}

    def respond(messages):
        key = excerpt(messages)
        order.setdefault(key, next(labels))
        return answer(order[key])

    classify(small_corpus, [filing], FakeClient(respond))
    markdown = segment_stage.read_markdown(filing.markdown_path)
    rows = read_jsonl(outputs(small_corpus, filing)["labels"])
    kept = outputs(small_corpus, filing)["kept"].read_bytes().decode("utf-8")
    position, found = kept.index("\n\n") + 2, []
    marker = re.compile(r"<!-- chunk (c\d+) \| .*? \| chars (\d+)-(\d+) \| length (\d+) \| (KEEP|UNCERTAIN) -->\n")
    while position < len(kept):
        match = marker.match(kept, position)
        assert match, kept[position:position + 80]
        length = int(match.group(4))
        text = kept[match.end():match.end() + length]
        assert text == markdown[int(match.group(2)):int(match.group(3))]
        found.append(match.group(1))
        position = match.end() + length + 2
    assert found == [r["chunk_id"] for r in rows if r["final_label"] in {"KEEP", "UNCERTAIN"}]
    assert any(r["final_label"] == "DROP" for r in rows) and found


class Crash(BaseException):
    pass


def test_crash_mid_filing_leaves_no_record_and_rerun_has_no_duplicates(small_corpus):
    filings = prepare(small_corpus)
    filing = filings[FILING]
    calls = {"n": 0}

    def crashing(messages):
        calls["n"] += 1
        if calls["n"] > 2:
            raise Crash()
        return answer("KEEP")

    with pytest.raises(Crash):
        classify(small_corpus, [filing], FakeClient(crashing))
    assert not outputs(small_corpus, filing)["record"].exists()
    result = classify(small_corpus, [filing], FakeClient(lambda m: answer("KEEP")))
    assert result["counts"] == {"classified": 1}
    rows = read_jsonl(outputs(small_corpus, filing)["labels"])
    chunks = segment_stage.load_chunks(segment_stage.paths(small_corpus, filing)[0])
    assert [r["chunk_id"] for r in rows] == [c["chunk_id"] for c in chunks]


def test_resume_skips_and_prompt_change_reruns_only_classification(small_corpus, tmp_path):
    filings = prepare(small_corpus)
    filing = filings[FILING]
    classify(small_corpus, [filing], FakeClient())
    again = FakeClient()
    assert classify(small_corpus, [filing], again)["counts"] == {"already_done": 1}
    assert again.calls == []

    prompts = tmp_path / "prompts"
    shutil.copytree(small_corpus.classify["prompts_dir"], prompts)
    (prompts / "classify_v1.md").write_text((prompts / "classify_v1.md").read_text(encoding="utf-8") + "\nEdited.\n",
                                            encoding="utf-8")
    text = small_corpus.path.read_text(encoding="utf-8")
    small_corpus.path.write_text(re.sub(r'prompts_dir = ".*"', f'prompts_dir = "{prompts.as_posix()}"', text),
                                 encoding="utf-8")
    edited = config.load(small_corpus.path)
    with closing(state.connect(edited.db_path)) as db:
        assert segment_stage.run(edited, db, [filing], log=lambda *_: None)["counts"] == {"already_done": 1}
    assert classify(edited, [filing], FakeClient())["counts"] == {"classified": 1}


def test_source_incomplete_filing_is_not_classified(small_corpus):
    filings = prepare(small_corpus)
    result = classify(small_corpus, [filings["BBB_Beta_Corp/1993_c"]], FakeClient())
    assert result["counts"] == {"source_incomplete": 1}
