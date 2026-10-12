import json

import pytest

import convertmd
from conftest import TinyTokenizer, build_corpus, registered
from convertmd import COMPATIBLE_CHECKED_HASHES, conversion_job
from pipeline import cli, state
from verify_outputs import verify

PREVIOUS_HASH = "c02774c21ee991597e7394b31650354b33b9f872266bf4cacd16ec1deb9b3823"  # convertmd 1.1.0


def submission(form: str, body: str = "ITEM 6. MANAGEMENT'S DISCUSSION\n\n   Research costs   (12)   34\n") -> str:
    return ("<SEC-DOCUMENT>x\n<SEC-HEADER>Metadata</SEC-HEADER>\n"
            f"<DOCUMENT>\n<TYPE>{form}\n<FILENAME>main.txt\n<TEXT>\n{body}</TEXT>\n</DOCUMENT>\n"
            "<DOCUMENT>\n<TYPE>EX-27\n<FILENAME>fds.txt\n<TEXT>\n<ARTICLE>5\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n")


@pytest.mark.parametrize("form", ["10-KSB", "10-KSB405", "10-KT", "10-KT405", "10-K/A", "10-KSB/A", "10-KT/A"])
def test_new_forms_convert(tmp_path, form):
    source = tmp_path / "filing.txt"
    source.write_text(submission(form), encoding="utf-8")
    report = conversion_job(source, tmp_path / "out" / "filing.md", {"t": TinyTokenizer()}, {})
    assert report["quality_status"] == "CHECKED_TEXT", report
    assert [d["type"] for d in report["documents"]] == [form]
    markdown = (tmp_path / "out" / "filing.md").read_text(encoding="utf-8")
    assert "Research costs   (12)   34" in markdown and "<ARTICLE>" not in markdown


def test_other_forms_are_still_not_annual_reports(tmp_path):
    source = tmp_path / "filing.txt"
    source.write_text(submission("20-F"), encoding="utf-8")
    report = conversion_job(source, tmp_path / "out" / "filing.md", {"t": TinyTokenizer()}, {})
    assert report["quality_status"] == "SOURCE_INCOMPLETE"


def test_output_from_previous_version_is_reused(tmp_path):
    source = tmp_path / "filing.txt"
    source.write_text(submission("10-K"), encoding="utf-8")
    destination = tmp_path / "out" / "filing.md"
    conversion_job(source, destination, {"t": TinyTokenizer()}, {})
    audit_path = destination.with_suffix(".audit.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit.update(converter_version="1.1.0", converter_sha256=PREVIOUS_HASH)
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    markdown_before = destination.read_bytes()

    reused = conversion_job(source, destination, {"t": TinyTokenizer()}, {}, resume=True)
    assert PREVIOUS_HASH in COMPATIBLE_CHECKED_HASHES
    assert reused["resumed"] and reused["compatible_converter_reuse"]
    assert destination.read_bytes() == markdown_before


def test_verify_accepts_compatible_versions_and_rejects_unknown(tmp_path):
    raw = tmp_path / "raw"
    (raw / "AAA").mkdir(parents=True)
    (raw / "AAA" / "2001_a.txt").write_text(submission("10-KSB"), encoding="utf-8")
    out = tmp_path / "md"
    assert convertmd.main(["--input", str(raw), "--output", str(out), "--tokenizer", "o200k_base"]) == 0
    assert verify(raw, out)["passed"]

    audit_path = out / "AAA" / "2001_a.audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    for converter, verified_by, expected in [
        (PREVIOUS_HASH, None, True),                         # converted by 1.1, never re-verified
        ("6f5ee1eec9b012f1215dfd777260fcaa7cb4176943fcdb223fd0b67451ee3c12", PREVIOUS_HASH, True),  # 1.0, verified by 1.1
        ("0" * 64, None, False),                             # unknown converter
    ]:
        audit.update(converter_sha256=converter)
        audit.pop("compatible_converter_reuse", None)
        audit.pop("verified_by_converter_sha256", None)
        if verified_by:
            audit.update(compatible_converter_reuse=True, verified_by_converter_sha256=verified_by)
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        assert verify(raw, out)["passed"] is expected, converter


def test_pipeline_converts_a_10ksb_filing(tmp_path):
    cfg = build_corpus(tmp_path, {"SMALL_Co": {"2003_ksb.txt": submission("10-KSB")}})
    text = (cfg.raw_dir / "SMALL_Co" / "filings.csv").read_text(encoding="utf-8").replace(",10-K,", ",10-KSB,")
    (cfg.raw_dir / "SMALL_Co" / "filings.csv").write_text(text, encoding="utf-8")
    assert cli.main(["--config", str(cfg.path), "all", "--fake-model"]) == 0
    filing = registered(cfg)["SMALL_Co/2003_ksb"]
    assert filing.form == "10-KSB" and filing.conversion_status == "CHECKED_TEXT"
    summary = state.summary(cfg.db_path)
    assert summary["forms"] == {"10-KSB": 1} and summary["stages"]["classify [fake]"] == {"done": 1}
