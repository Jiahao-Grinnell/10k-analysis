from pipeline import records


def make(tmp_path, **overrides):
    output = tmp_path / "out" / "a.labels.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("row\n", encoding="utf-8")
    path = tmp_path / "out" / "a.record.json"
    args = dict(stage="segment", stage_version="segment-1", filing_id="A/1", inputs={"markdown": "h1"},
                settings={"chunk_max_tokens": 2000}, outputs=[output])
    args.update(overrides)
    records.write_record(path, **args)
    return path, output


def test_atomic_write_leaves_no_temp_file(tmp_path):
    records.write_json(tmp_path / "x.json", {"a": 1})
    assert records.read_json(tmp_path / "x.json") == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_current_record_is_skipped(tmp_path):
    path, _ = make(tmp_path)
    assert records.is_current(path, stage_version="segment-1", inputs={"markdown": "h1"},
                              settings={"chunk_max_tokens": 2000})


def test_changes_force_a_rerun(tmp_path):
    path, output = make(tmp_path)
    same = dict(stage_version="segment-1", inputs={"markdown": "h1"}, settings={"chunk_max_tokens": 2000})
    assert not records.is_current(path, **{**same, "inputs": {"markdown": "h2"}})
    assert not records.is_current(path, **{**same, "stage_version": "segment-2"})
    assert not records.is_current(path, **{**same, "settings": {"chunk_max_tokens": 1500}})
    output.write_text("tampered\n", encoding="utf-8")
    assert not records.is_current(path, **same)


def test_missing_or_failed_record_is_not_current(tmp_path):
    same = dict(stage_version="segment-1", inputs={"markdown": "h1"}, settings={"chunk_max_tokens": 2000})
    assert not records.is_current(tmp_path / "none.record.json", **same)
    path, _ = make(tmp_path, status="failed")
    assert not records.is_current(path, **same)


def test_settings_hash_ignores_key_order():
    assert records.settings_sha256({"a": 1, "b": 2}) == records.settings_sha256({"b": 2, "a": 1})
