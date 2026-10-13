from contextlib import closing

import pytest

from conftest import HTML_10K, SMALL_FILES, build_corpus, write_company
from monitor import queries
from monitor.app import create_app
from pipeline import cli, state

FILING = "AAA_Alpha_Inc/2020_a"


@pytest.fixture(scope="module")
def processed(tmp_path_factory):
    """One corpus run through the pipeline with the fake model, shared by the tests in this module."""
    cfg = build_corpus(tmp_path_factory.mktemp("monitor"), SMALL_FILES)
    assert cli.main(["--config", str(cfg.path), "all", "--fake-model"]) == 0
    return cfg


@pytest.fixture
def client(processed):
    app = create_app(processed.path)
    app.testing = True
    return app.test_client()


def page(client, url, status=200):
    response = client.get(url)
    assert response.status_code == status, (url, response.status_code)
    return response.get_data(as_text=True)


def test_every_page_renders(client):
    overview = page(client, "/")
    assert "Filings registered" in overview and "segment" in overview and "Tokens by step" in overview
    assert "kept by filter [fake]" in overview
    runs = page(client, "/runs")
    assert "History" in runs and "all registered filings · fake model" in runs and "Finished" in runs
    assert runs.count('class="job-row"') == 1  # scan + convert + segment + classify shown as one action
    structure = page(client, f"/structure?id={FILING}")
    assert "Document map" in structure and "Outline" in structure and "PART I" in structure
    assert "c00001" in structure and "lbl-uncertain" in structure  # chunk links and fake-model labels
    page(client, "/structure?id=nope", 404)
    assert "not been segmented" in page(client, "/structure?id=BBB_Beta_Corp/1993_c")
    assert 'href="/structure?id=' in page(client, "/filings")
    assert "AAA_Alpha_Inc" in page(client, "/filings")
    assert "Failed stages" in page(client, "/problems")
    detail = page(client, f"/filing?id={FILING}")
    assert "c00001" in detail and "UNCERTAIN" in detail and "Markdown" in detail
    assert "fake client: no model judgement" in page(client, f"/chunk?id={FILING}&chunk=c00001")
    status = client.get("/api/status").get_json()
    assert status["active"] is None and "segment" in status["stages"]


def test_filing_filters(client):
    rows = lambda url: page(client, url).count('href="/filing?id=')
    assert rows("/filings?q=aaa") == 2
    assert rows("/filings?year=1998") == 1
    assert rows("/filings?form=10-K") == 3
    assert rows("/filings?stage=segment&status=done") == 2
    assert rows("/filings?stage=segment&status=source_incomplete") == 1
    assert rows("/filings?stage=classify&status=not+started") == 0
    assert rows("/filings?page=abc") == 3  # a bad page number falls back to page 1


def test_unknown_or_path_like_ids_are_404(client):
    for url in ["/filing?id=nope", "/filing?id=../../config.toml", "/filing?id=..%2F..%2Fconfig.toml",
                f"/chunk?id={FILING}&chunk=c99999", f"/chunk?id={FILING}&chunk=..%2F..%2Fx",
                "/chunk?id=../../config.toml&chunk=c00001"]:
        page(client, url, 404)


def test_filing_text_is_escaped(client):
    # The fixture has a table with a rowspan, which the converter keeps as raw HTML in the Markdown.
    detail = page(client, f"/filing?id={FILING}")
    chunk_ids = sorted(set(part.split('"')[0] for part in detail.split("&chunk=")[1:]))
    texts = [page(client, f"/chunk?id={FILING}&chunk={c}") for c in chunk_ids]
    html_chunk = next(t for t in texts if "rowspan" in t)
    assert "&lt;td rowspan=&#34;2&#34;&gt;" in html_chunk or "&lt;td rowspan=&quot;2&quot;&gt;" in html_chunk
    assert '<td rowspan="2">' not in html_chunk


def test_low_disk_warning(client, monkeypatch):
    monkeypatch.setattr(queries.disk, "free_bytes", lambda path: 1024 ** 3)
    assert "Low disk space" in page(client, "/")
    assert client.get("/api/status").get_json()["disk"]["low"] is True


def test_monitor_does_not_write(client, processed):
    with closing(state.connect(processed.db_path, readonly=True)) as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM stage_status ORDER BY 1, 2, 3")]
    for url in ["/", "/runs", "/filings", "/problems", f"/filing?id={FILING}", "/api/status"]:
        page(client, url)
    with closing(state.connect(processed.db_path, readonly=True)) as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM stage_status ORDER BY 1, 2, 3")] == before


# The tests below change the shared corpus, so they run last.

def test_not_started_filter_after_new_company(client, processed):
    write_company(processed.raw_dir, "CCC_Gamma_Co", {"2021_x.htm": HTML_10K, "2022_y.htm": HTML_10K})
    assert cli.main(["--config", str(processed.path), "scan"]) == 0
    assert page(client, "/filings?stage=convert&status=not+started").count('href="/filing?id=') == 2


def test_active_run_progress_and_problems(client, processed):
    with closing(state.connect(processed.db_path)) as db:
        run_id = state.start_run(db, "classify", {}, planned=3, model_tag="fake")
        state.set_progress(db, run_id, FILING, 2, 9)
        state.set_stage(db, "AAA_Alpha_Inc/1998_b", "segment", "failed", error="boom: simulated")
        state.set_stage(db, FILING, "classify", "done", model_tag="fake", tokens_in=100, tokens_kept=0)
        db.commit()
    overview = page(client, "/")
    assert "Running now" in overview and "Current filing" in overview and 'data-field="chunks_done">2<' in overview
    assert "Running now" in page(client, "/runs")
    active = client.get("/api/status").get_json()["active"]
    assert active["run_id"] == run_id and active["chunk"]["chunks_done"] == 2 and active["planned"] == 3
    problems = page(client, "/problems")
    assert "boom: simulated" in problems and "Filings with nothing kept <span class=\"muted\">(1)" in problems


def test_missing_database_explains(small_corpus):
    app = create_app(small_corpus.path)
    response = app.test_client().get("/")
    assert response.status_code == 503 and "pipeline scan" in response.get_data(as_text=True)
