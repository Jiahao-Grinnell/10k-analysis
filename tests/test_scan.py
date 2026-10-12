from contextlib import closing
import hashlib

from conftest import HTML_10K, TinyTokenizer, registered, write_company
from pipeline import cli, convert_stage, corpus, state
from pipeline.segment import stage as segment_stage


def scan(cfg):
    with closing(state.connect(cfg.db_path)) as db:
        return corpus.scan(cfg, db, log=lambda *_: None)


def convert(cfg, **kwargs):
    with closing(state.connect(cfg.db_path)) as db:
        filings = corpus.select(cfg, db)
        return convert_stage.run(cfg, db, filings, tokenizers={"o200k_base": TinyTokenizer()}, log=lambda *_: None, **kwargs)


def test_rescan_registers_only_new_filings(small_corpus):
    assert scan(small_corpus)["counts"] == {"new": 3}
    assert scan(small_corpus)["counts"] == {"unchanged": 3}
    write_company(small_corpus.raw_dir, "CCC_Gamma_Co", {"2021_x.htm": HTML_10K, "2022_y.htm": HTML_10K})
    result = scan(small_corpus)
    assert result["counts"] == {"unchanged": 3, "new": 2}
    assert set(registered(small_corpus)) >= {"CCC_Gamma_Co/2021_x", "CCC_Gamma_Co/2022_y"}


def test_missing_source_fails_only_that_filing(small_corpus):
    write_company(small_corpus.raw_dir, "DDD_Delta", {"2020_ok.htm": HTML_10K, "2021_gone.htm": None})
    result = scan(small_corpus)
    assert result["missing_source"] == 1 and result["counts"]["new"] == 5
    summary = convert(small_corpus)
    errors = {e["filing_id"]: e["error"] for e in summary["errors"]}
    assert list(errors) == ["DDD_Delta/2021_gone"] and "missing" in errors["DDD_Delta/2021_gone"]
    assert summary["conversion_status"]["CHECKED_TEXT"] == 3  # the other filings still converted

    # the downloader fetches the file later: the next scan picks it up
    (small_corpus.raw_dir / "DDD_Delta" / "2021_gone.htm").write_text(HTML_10K, encoding="utf-8")
    assert scan(small_corpus)["counts"] == {"unchanged": 4, "updated": 1}
    assert registered(small_corpus)["DDD_Delta/2021_gone"].scan_status == "registered"


def test_changed_source_needs_explicit_reconvert_and_reruns_only_that_filing(small_corpus):
    scan(small_corpus)
    convert(small_corpus)
    filings = registered(small_corpus)
    with closing(state.connect(small_corpus.db_path)) as db:
        segment_stage.run(small_corpus, db, list(filings.values()), log=lambda *_: None)
    target = filings["AAA_Alpha_Inc/2020_a"]
    old_markdown = target.markdown_path.read_bytes()

    new_html = HTML_10K.replace("$12 million", "$15 million")
    new_sha = hashlib.sha256(new_html.encode("utf-8")).hexdigest()
    write_company(small_corpus.raw_dir, "AAA_Alpha_Inc",
                  {"2020_a.htm": new_html, "1998_b.txt": (small_corpus.raw_dir / "AAA_Alpha_Inc" / "1998_b.txt").read_text(encoding="utf-8")},
                  rows={"2020_a.htm": {"sha256": new_sha}})
    assert scan(small_corpus)["counts"]["source_changed"] == 1

    refused = convert(small_corpus)
    assert "reconvert-changed" in {e["filing_id"]: e["error"] for e in refused["errors"]}["AAA_Alpha_Inc/2020_a"]
    assert target.markdown_path.read_bytes() == old_markdown  # not rewritten without the flag

    rebuilt = convert(small_corpus, reconvert_changed=True)
    assert rebuilt["newly_converted"] == 1 and not rebuilt["errors"]
    assert "$15 million" in target.markdown_path.read_text(encoding="utf-8")
    filings = registered(small_corpus)
    assert filings["AAA_Alpha_Inc/2020_a"].scan_status == "registered"
    with closing(state.connect(small_corpus.db_path)) as db:
        counts = segment_stage.run(small_corpus, db, list(filings.values()), log=lambda *_: None)["counts"]
    assert counts["segmented"] == 1 and counts["already_done"] == 1


def test_amendment_is_marked_and_linked(small_corpus):
    write_company(small_corpus.raw_dir, "EEE_Eps", {"2020_orig.htm": HTML_10K, "2020_amend.htm": HTML_10K},
                  rows={"2020_amend.htm": {"form": "10-K/A", "filing_date": "2021-06-01"}})
    assert scan(small_corpus)["amendments_linked"] == 1
    filings = registered(small_corpus)
    amendment = filings["EEE_Eps/2020_amend"]
    assert amendment.is_amendment and amendment.amends_filing_id == "EEE_Eps/2020_orig"
    assert not filings["EEE_Eps/2020_orig"].is_amendment


def test_paths_outside_raw_folder_are_rejected(small_corpus):
    write_company(small_corpus.raw_dir, "FFF_Bad", {"2020_ok.htm": HTML_10K, "..\\..\\evil.htm": None})
    result = scan(small_corpus)
    assert result["counts"]["rejected"] == 1
    assert "FFF_Bad/2020_ok" in registered(small_corpus)


def test_old_database_is_migrated(small_corpus):
    import sqlite3
    small_corpus.db_path.parent.mkdir(parents=True)
    old = sqlite3.connect(small_corpus.db_path)
    old.execute("CREATE TABLE filings (filing_id TEXT PRIMARY KEY, company_dir TEXT, ticker TEXT, company_id TEXT, "
                "year INTEGER, form TEXT, raw_path TEXT, markdown_path TEXT, conversion_status TEXT, "
                "raw_format TEXT, markdown_tokens INTEGER)")
    old.execute("INSERT INTO filings (filing_id, raw_path) VALUES ('AAA_Alpha_Inc/2020_a', 'C:/old/absolute.htm')")
    old.commit()
    old.close()
    assert scan(small_corpus)["counts"] == {"updated": 1, "new": 2}
    assert registered(small_corpus)["AAA_Alpha_Inc/2020_a"].raw_path == small_corpus.raw_dir / "AAA_Alpha_Inc" / "2020_a.htm"


def test_scan_command(small_corpus, capsys):
    assert cli.main(["--config", str(small_corpus.path), "scan"]) == 0
    assert "3 filings listed" in capsys.readouterr().out
