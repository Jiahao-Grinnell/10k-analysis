import json

from pipeline import cli, state
from pipeline.records import sha256_file


def hashes(folder):
    return {p: sha256_file(p) for p in folder.rglob("*") if p.is_file()}


def test_one_command_runs_a_filing_end_to_end(small_corpus, capsys):
    raw_before = hashes(small_corpus.raw_dir)
    args = ["--config", str(small_corpus.path), "all", "--fake-model", "--filing", "AAA_Alpha_Inc/1998_b"]
    assert cli.main(args) == 0

    out = small_corpus.outputs_dir / "classify" / "fake" / "AAA_Alpha_Inc"
    names = {p.name for p in out.iterdir()}
    assert {f"1998_b{s}" for s in (".labels.jsonl", ".requests.jsonl", ".kept.md", ".summary.json", ".record.json")} <= names
    summary = json.loads((out / "1998_b.summary.json").read_text(encoding="utf-8"))
    assert summary["chunks"] >= 2 and summary["final_labels"] == {"UNCERTAIN": summary["chunks"]}
    assert "ITEM 7" in (out / "1998_b.kept.md").read_text(encoding="utf-8")

    markdown_before = hashes(small_corpus.markdown_dir)
    capsys.readouterr()
    assert cli.main(args) == 0  # second run: everything skipped
    printed = capsys.readouterr().out
    assert "'already_done': 1" in printed
    assert hashes(small_corpus.raw_dir) == raw_before
    assert hashes(small_corpus.markdown_dir) == markdown_before

    info = state.summary(small_corpus.db_path)
    assert info["stages"]["segment"] == {"done": 1}
    assert info["stages"]["classify [fake]"] == {"done": 1}


def test_classify_without_model_explains_what_to_do(small_corpus, capsys):
    text = small_corpus.path.read_text(encoding="utf-8").replace('backend = "fake"', 'backend = "openai"')
    small_corpus.path.write_text(text, encoding="utf-8")
    try:
        cli.main(["--config", str(small_corpus.path), "classify"])
    except SystemExit as exc:
        assert "No local model configured" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
