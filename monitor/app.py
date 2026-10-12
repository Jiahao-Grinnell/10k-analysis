"""Read-only local web monitor.

Run:  python -m monitor            (http://127.0.0.1:5000)
 or:  python -m flask --app monitor.app run --host 127.0.0.1 --port 5000

The status DB is opened read-only. Pages never write anything and never build a
file path from URL text; filing IDs are looked up in the DB first.
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request

from monitor import queries
from pipeline import config, state


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
        rows = [queries.run_progress(db(), r) for r in queries.runs(db(), limit=50)]
        return render_template("runs.html", runs=rows, active=queries.active_run(db()))

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

    @app.route("/problems")
    def problems():
        return render_template("problems.html", p=queries.problems(db()))

    @app.route("/api/status")
    def api_status():
        return jsonify(queries.api_status(cfg, db()))

    @app.errorhandler(404)
    @app.errorhandler(503)
    def _error(error):
        return render_template("error.html", error=error), error.code

    return app
