"""Local web monitor with run control.

Run:  python -m monitor            (http://127.0.0.1:5000)
 or:  python -m flask --app monitor.app run --host 127.0.0.1 --port 5000

The status DB is opened read-only and pages never build a file path from URL text;
filing IDs are looked up in the DB first. The only actions are starting and stopping
a `pipeline all` run (POST, token-protected, local requests only).
"""
from __future__ import annotations

from pathlib import Path
import secrets

from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for

from monitor import queries, runner
from pipeline import config, state

LOCAL = {"127.0.0.1", "::1"}


def _duration(seconds) -> str:
    if seconds is None:
        return "–"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def create_app(config_path: Path | str | None = None) -> Flask:
    app = Flask(__name__)
    cfg = config.load(config_path)
    app.config["PIPELINE"] = cfg
    app.config["RUN_TOKEN"] = secrets.token_urlsafe(24)  # required on every start/stop request
    app.jinja_env.globals["run_token"] = app.config["RUN_TOKEN"]
    app.jinja_env.filters["n"] = lambda v: "–" if v is None else f"{v:,}"
    app.jinja_env.filters["duration"] = _duration

    def db():
        if "db" not in g:
            if not cfg.db_path.is_file():
                abort(503, description=f"No status database at {cfg.db_path}. Run `python -m pipeline scan` first.")
            g.db = state.connect(cfg.db_path, readonly=True)
        return g.db

    @app.teardown_appcontext
    def _close(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.route("/")
    def overview():
        return render_template("overview.html", o=queries.overview(cfg, db()), cfg=cfg)

    @app.route("/runs")
    def runs():
        history = queries.jobs(db(), limit=50)
        active = next((j for j in history if j["result"] == "running"), None)
        return render_template("runs.html", jobs=history, active=active)

    @app.route("/structure")
    def structure():
        result = queries.structure(cfg, db(), request.args.get("id", ""), request.args.get("model"))
        if result is None:
            abort(404, description="Unknown filing ID")
        return render_template("structure.html", s=result)

    @app.route("/filings")
    def filings():
        args = {k: request.args.get(k, "").strip() for k in ("q", "year", "form", "stage", "status")}
        page = request.args.get("page", "1")
        result = queries.filing_list(db(), page=int(page) if page.isdigit() else 1, **args)
        return render_template("filings.html", r=result, args=args)

    @app.route("/filing")
    def filing():
        detail = queries.filing_detail(cfg, db(), request.args.get("id", ""), request.args.get("model"))
        if detail is None:
            abort(404, description="Unknown filing ID")
        return render_template("filing.html", d=detail)

    @app.route("/chunk")
    def chunk():
        result = queries.chunk_text(cfg, db(), request.args.get("id", ""), request.args.get("chunk", ""),
                                    request.args.get("model"))
        if result is None:
            abort(404, description="Unknown filing or chunk")
        return render_template("chunk.html", c=result)

    @app.route("/clean")
    def clean():
        result = queries.clean_file(cfg, db(), request.args.get("id", ""), request.args.get("model"))
        if result is None:
            abort(404, description="Unknown filing ID")
        return render_template("clean.html", c=result)

    @app.route("/problems")
    def problems():
        return render_template("problems.html", p=queries.problems(db()))

    @app.route("/api/status")
    def api_status():
        result = queries.api_status(cfg, db())
        web = runner.current(cfg)
        result["web_run"] = {"alive": web["alive"], "pid": web["pid"]} if web else None
        return jsonify(result)

    # ---- run control: the only actions this app performs -------------------------------

    def _check_action():
        if request.remote_addr not in LOCAL:
            abort(403, description="Run control is only available from this computer.")
        if not secrets.compare_digest(request.form.get("token", ""), app.config["RUN_TOKEN"]):
            abort(400, description="Missing or wrong form token. Reload the page and try again.")

    def _active_db_run():
        return queries.active_run(db()) if cfg.db_path.is_file() else None

    @app.route("/control")
    def control_page():
        web = runner.current(cfg)
        job = queries.active_job(db()) if cfg.db_path.is_file() else None
        return render_template("control.html", web=web, active=_active_db_run(), job=job, message=request.args.get("msg", ""),
                               model_configured=bool(cfg.classify.get("model_tag")) and cfg.classify["backend"] != "fake")

    @app.post("/control/start")
    def control_start():
        _check_action()
        form = request.form
        try:
            args = runner.command(cfg, form.get("company", ""), form.get("year", ""), form.get("form", ""),
                                  form.get("limit", ""), fake_model=form.get("fake_model") == "on",
                                  fake_delay=form.get("fake_delay", ""))
            info = runner.start(cfg, args, _active_db_run())
        except runner.RunRefused as refused:
            return redirect(url_for("control_page", msg=f"Not started: {refused}"))
        return redirect(url_for("control_page", msg=f"Started (PID {info['pid']})."))

    @app.post("/control/stop")
    def control_stop():
        _check_action()
        runner.request_stop(cfg)
        return redirect(url_for("control_page", msg="Stop requested: the run stops after the filing in progress."))

    @app.post("/control/force-stop")
    def control_force_stop():
        _check_action()
        stopped = runner.force_stop(cfg)
        return redirect(url_for("control_page", msg="Process ended. The unfinished filing will be redone next time."
                                if stopped else "No run from this page is active."))

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(503)
    def _error(error):
        return render_template("error.html", error=error), error.code

    return app
