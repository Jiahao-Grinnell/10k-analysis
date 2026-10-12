from contextlib import closing
import os

from conftest import TinyTokenizer
from convertmd import conversion_job
from pipeline import cli, convert_stage, corpus, state


def filings_of(cfg):
    return {f.filing_id: f for f in corpus.list_filings(cfg.raw_dir, cfg.markdown_dir)}


def run_convert(cfg):
    with closing(state.connect(cfg.db_path)) as db:
        return convert_stage.run(cfg, db, list(filings_of(cfg).values()),
                                 tokenizers={"o200k_base": TinyTokenizer()}, log=lambda *_: None)


def test_existing_output_is_indexed_and_never_rewritten(small_corpus):
    existing = filings_of(small_corpus)["AAA_Alpha_Inc/2020_a"]
    conversion_job(existing.raw_path, existing.markdown_path, {"o200k_base": TinyTokenizer()}, existing.metadata)
    before = existing.markdown_path.read_bytes(), existing.audit_path.read_bytes()
    os.utime(existing.markdown_path, (1_000_000, 1_000_000))

    result = run_convert(small_corpus)

    assert (existing.markdown_path.read_bytes(), existing.audit_path.read_bytes()) == before
    assert existing.markdown_path.stat().st_mtime == 1_000_000
    assert result["newly_converted"] == 2  # legacy filing and header-only placeholder
    assert result["conversion_status"] == {"CHECKED_TEXT": 2, "SOURCE_INCOMPLETE": 1}
    assert result["converted"] == 2 and not result["errors"]
    assert (small_corpus.outputs_dir / "convert" / "conversion_index.csv").is_file()


def test_raw_files_are_not_modified(small_corpus):
    raw = {p: p.read_bytes() for p in small_corpus.raw_dir.rglob("*") if p.is_file()}
    run_convert(small_corpus)
    assert {p: p.read_bytes() for p in small_corpus.raw_dir.rglob("*") if p.is_file()} == raw


def test_damaged_or_partial_output_is_reported_not_rewritten(small_corpus):
    run_convert(small_corpus)
    filings = filings_of(small_corpus)
    damaged = filings["AAA_Alpha_Inc/2020_a"]
    damaged.markdown_path.write_text("edited by hand\n", encoding="utf-8")
    partial = filings["AAA_Alpha_Inc/1998_b"]
    partial.audit_path.unlink()

    result = run_convert(small_corpus)

    assert damaged.markdown_path.read_text(encoding="utf-8") == "edited by hand\n"
    assert not partial.audit_path.exists()
    errors = {e["filing_id"]: e["error"] for e in result["errors"]}
    assert "hash differs" in errors["AAA_Alpha_Inc/2020_a"]
    assert "Partial conversion output" in errors["AAA_Alpha_Inc/1998_b"]


def test_status_database_and_rebuild_index(small_corpus, capsys):
    run_convert(small_corpus)
    first = state.summary(small_corpus.db_path)
    assert first["filings"] == 3 and first["converted"] == 2
    assert first["conversion_status"] == {"CHECKED_TEXT": 2, "SOURCE_INCOMPLETE": 1}
    assert first["stages"]["convert"] == {"done": 2, "source_incomplete": 1}

    config_arg = ["--config", str(small_corpus.path)]
    assert cli.main(config_arg + ["rebuild-index"]) == 0
    rebuilt = state.summary(small_corpus.db_path)
    assert {k: rebuilt[k] for k in ("filings", "converted", "markdown_tokens", "conversion_status", "stages")} == \
           {k: first[k] for k in ("filings", "converted", "markdown_tokens", "conversion_status", "stages")}

    assert cli.main(config_arg + ["status"]) == 0
    out = capsys.readouterr().out
    assert "SOURCE_INCOMPLETE: 1" in out and "convert: done 2, source_incomplete 1" in out


def test_status_without_database(small_corpus, capsys):
    assert cli.main(["--config", str(small_corpus.path), "status"]) == 1
    assert "No status database" in capsys.readouterr().out


def test_cli_convert_respects_filters(small_corpus):
    run_convert(small_corpus)  # convert with the test tokenizer, then index through the CLI
    small_corpus.db_path.unlink()
    assert cli.main(["--config", str(small_corpus.path), "convert", "--company", "BBB"]) == 0
    info = state.summary(small_corpus.db_path)
    assert info["filings"] == 1 and info["conversion_status"] == {"SOURCE_INCOMPLETE": 1}
