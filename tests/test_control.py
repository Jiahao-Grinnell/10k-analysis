from contextlib import closing
import json
import time

import pytest

from conftest import TinyTokenizer, registered
from monitor import runner
from monitor.app import create_app
from pipeline import cli, control, convert_stage, state
from pipeline.classify import stage as classify_stage
from pipeline.classify.client import FakeClient
from pipeline.segment import stage as segment_stage


def answer():
    return json.dumps({"reason": "test", "label": "KEEP", "categories": []})


def test_each_stage_stops_before_the_next_filing(small_corpus):
    filings = list(registered(small_corpus).values())
    with closing(state.connect(small_corpus.db_path)) as db:
        control.request_stop(small_corpus)
        assert convert_stage.run(small_corpus, db, filings, tokenizers={"t": TinyTokenizer()},
                                 log=lambda *_: None)["stopped"]
        control.clear_stop(small_corpus)
        convert_stage.run(small_corpus, db, filings, tokenizers={"t": TinyTokenizer()}, log=lambda *_: None)
        filings = list(registered(small_corpus).values())
        control.request_stop(small_corpus)
        result = segment_stage.run(small_corpus, db, filings, log=lambda *_: None)
        assert result["stopped"] and result["counts"] == {}
        control.clear_stop(small_corpus)
        segment_stage.run(small_corpus, db, filings, log=lambda *_: None)

        def respond(messages):  # the user presses "stop" while the first filing is being classified
            control.request_stop(small_corpus)
            return answer()

        result = classify_stage.run(small_corpus, db, filings, FakeClient(respond), log=lambda *_: None)
    assert result["stopped"] and result["counts"] == {"classified": 1}  # the filing in progress was finished


def test_cli_clears_old_stop_request_and_reports_stop(small_corpus):
    control.request_stop(small_corpus)  # left over from an earlier run
    assert cli.main(["--config", str(small_corpus.path), "all", "--fake-model"]) == 0
    assert not control.stop_requested(small_corpus)


def test_command_validation(small_corpus):
    args = runner.command(small_corpus, company="AAPL", year="2020", form="10-K", limit="5", fake_model=True, fake_delay="0.5")
    assert args[2:] == ["pipeline", "--config", str(small_corpus.path), "all", "--company", "AAPL", "--form", "10-K",
                        "--year", "2020", "--limit", "5", "--fake-model", "--fake-delay", "0.5"]
    for bad in [dict(year="20x0"), dict(limit="-3"), dict(company="--force"), dict(fake_model=True, fake_delay="99")]:
        with pytest.raises(runner.RunRefused):
            runner.command(small_corpus, **bad)


@pytest.fixture
def web(small_corpus):
    app = create_app(small_corpus.path)
    app.testing = True
    client = app.test_client()
    token = app.config["RUN_TOKEN"]
    post = lambda url, **form: client.post(url, data={"token": token, **form})
    return small_corpus, client, post


def wait_until(predicate, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def last_run(cfg):
    """The latest run row, or {} while the run has not created the database yet."""
    if not cfg.db_path.is_file():
        return {}
    with closing(state.connect(cfg.db_path, readonly=True)) as db:
        row = db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return dict(row) if row else {}


def test_actions_need_the_token(web):
    _, client, _ = web
    assert client.post("/control/start", data={"fake_model": "on"}).status_code == 400
    assert client.post("/control/stop", data={"token": "wrong"}).status_code == 400


def test_start_runs_the_pipeline_and_refuses_a_second_start(web):
    cfg, client, post = web
    assert "Start processing" in client.get("/control").get_data(as_text=True)
    response = post("/control/start", fake_model="on", fake_delay="0.3")
    assert response.status_code == 302 and "Started" in response.headers["Location"]
    second = post("/control/start", fake_model="on")
    assert "Not+started" in second.headers["Location"] or "Not%20started" in second.headers["Location"]
    assert "Stop after current filing" in client.get("/control").get_data(as_text=True)

    assert wait_until(lambda: not runner.current(cfg)["alive"])
    log = runner.current(cfg)["log_tail"]
    assert "classify [fake]" in log, log
    assert state.summary(cfg.db_path)["stages"]["classify [fake]"] == {"done": 2, "source_incomplete": 1}


def test_stop_after_current_filing_then_resume(web):
    cfg, client, post = web
    post("/control/start", fake_model="on", fake_delay="0.4", company="AAA")
    assert wait_until(lambda: last_run(cfg).get("command") == "classify")
    post("/control/stop")
    assert wait_until(lambda: not runner.current(cfg)["alive"])
    assert last_run(cfg)["exit_code"] == control.EXIT_STOPPED
    done = state.summary(cfg.db_path)["stages"].get("classify [fake]", {}).get("done", 0)
    assert done < 2  # the second filing was not started

    post("/control/start", fake_model="on", company="AAA")  # start again: continues with what is left
    assert wait_until(lambda: not runner.current(cfg)["alive"])
    assert state.summary(cfg.db_path)["stages"]["classify [fake]"]["done"] == 2


def test_force_stop_ends_the_process_and_next_run_redoes_the_filing(web):
    cfg, client, post = web
    post("/control/start", fake_model="on", fake_delay="1.0", company="AAA")
    assert wait_until(lambda: last_run(cfg).get("command") == "classify")
    response = post("/control/force-stop")
    assert "Process+ended" in response.headers["Location"] or "Process%20ended" in response.headers["Location"]
    assert wait_until(lambda: not runner.current(cfg)["alive"], timeout=30)
    assert last_run(cfg)["finished"] is None  # killed: the run row was never finished

    restarted = post("/control/start", fake_model="on", company="AAA")
    assert "Started" in restarted.headers["Location"], restarted.headers["Location"]
    assert wait_until(lambda: not runner.current(cfg)["alive"])
    stages = state.summary(cfg.db_path)["stages"]["classify [fake]"]
    assert stages.get("done") == 2, (stages, runner.current(cfg)["log_tail"])
