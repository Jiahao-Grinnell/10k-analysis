"""Command line: python -m pipeline <command> [options]."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from pipeline import config, control, corpus, state

# Stage modules (converter, HTTP client, tokenizer) load only in the commands that need them,
# so `status` and `scan` start quickly.


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--filing", help="One filing ID, e.g. AAPL_Apple_Inc_CIK0000320193/2025_2025-10-31_0000320193-25-000079")
    parser.add_argument("--company", help="Ticker, company folder, company_id or CIK")
    parser.add_argument("--year", type=int, help="Report year")
    parser.add_argument("--form", help="Form type, e.g. 10-K, 10-K405, 10-KSB, 10-K/A")
    parser.add_argument("--limit", type=int, help="Process at most N filings (after other filters)")


def _args(args) -> dict:
    """Command options for the runs table (without the handler)."""
    return {k: v for k, v in vars(args).items() if not k.startswith("_") and k != "func"}


def _selected(cfg, args) -> list:
    """Filings from the DB registry; scans 10K_Filings/ only if nothing is registered yet."""
    with closing(state.connect(cfg.db_path)) as db:
        if not corpus.registered_count(db):
            print("No filings registered yet; scanning 10K_Filings/ first.", flush=True)
            _print_scan(corpus.scan(cfg, db))
        chosen = corpus.select(cfg, db, args.filing, args.company, args.year, args.form, args.limit)
    if not chosen:
        raise SystemExit("No filings match the given filters")
    return chosen


def _print_scan(result: dict) -> None:
    print(f"scan: {result['manifests']:,} manifests, {result['listed']:,} filings listed; {result['counts']}; "
          f"amendments linked: {result['amendments_linked']}; missing source files: {result['missing_source']}; "
          f"registered but no longer listed: {result['registered_but_no_longer_listed']}")


def cmd_scan(cfg, args) -> int:
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "scan", _args(args), job_id=getattr(args, "_job_id", None))
        result = corpus.scan(cfg, db)
        state.finish_run(db, run_id, 0)
    _print_scan(result)
    return 0


def cmd_convert(cfg, args) -> int:
    from pipeline import convert_stage
    filings = _selected(cfg, args)
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "convert", _args(args), planned=len(filings), job_id=getattr(args, "_job_id", None))
        print(f"convert: {len(filings)} filings (existing Markdown is indexed, never rewritten)", flush=True)
        result = convert_stage.run(cfg, db, filings, reconvert_changed=getattr(args, "reconvert_changed", False))
        code = control.EXIT_STOPPED if result["stopped"] else 1 if result["errors"] else 0
        state.finish_run(db, run_id, code)
    _print_convert(result)
    return code


def cmd_segment(cfg, args) -> int:
    from pipeline.segment import stage as segment_stage
    filings = _selected(cfg, args)
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "segment", _args(args), planned=len(filings), job_id=getattr(args, "_job_id", None))
        print(f"segment: {len(filings)} filings (finished filings are skipped unless --force)", flush=True)
        result = segment_stage.run(cfg, db, filings, force=args.force)
        code = control.EXIT_STOPPED if result["stopped"] else 1 if result["failures"] else 0
        state.finish_run(db, run_id, code)
    print(f"segment: {result['counts']}")
    return code


def cmd_classify(cfg, args, client=None) -> int:
    from pipeline.classify import client as clients, stage as classify_stage
    filings = _selected(cfg, args)
    client = client or clients.from_config(cfg, fake=args.fake_model, fake_delay=args.fake_delay)
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "classify", _args(args), planned=len(filings), model_tag=client.tag,
                                 job_id=getattr(args, "_job_id", None))
        print(f"classify: {len(filings)} filings with model '{client.tag}', one filing at a time", flush=True)
        try:
            result = classify_stage.run(cfg, db, filings, client, force=args.force, run_id=run_id)
        except BaseException:
            state.finish_run(db, run_id, 130)
            raise
        code = control.EXIT_STOPPED if result["stopped"] else 1 if result["failures"] else 0
        state.finish_run(db, run_id, code)
    print(f"classify [{client.tag}]: {result['counts']}")
    return code


def cmd_all(cfg, args) -> int:
    from pipeline.classify import client as clients
    client = clients.from_config(cfg, fake=args.fake_model, fake_delay=args.fake_delay)  # fail early if no model is configured
    with closing(state.connect(cfg.db_path)) as db:  # one row for the whole job; its steps point to it
        args._job_id = state.start_run(db, "all", _args(args), model_tag=client.tag)
    code = 130
    try:
        cmd_scan(cfg, args)  # pick up filings added to 10K_Filings/ since the last scan
        codes = []
        for step in (cmd_convert, cmd_segment, lambda c, a: cmd_classify(c, a, client)):
            codes.append(step(cfg, args))
            if codes[-1] == control.EXIT_STOPPED:  # stop requested: do not start the next stage
                print("Stopped on request; run the same command again to continue.")
                break
        code = control.EXIT_STOPPED if codes[-1] == control.EXIT_STOPPED else max(codes)
        return code
    finally:
        with closing(state.connect(cfg.db_path)) as db:
            state.finish_run(db, args._job_id, code)


def cmd_rebuild_index(cfg, args) -> int:
    from pipeline import convert_stage
    from pipeline.reindex import reindex_records
    for suffix in ("", "-wal", "-shm"):
        Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
    with closing(state.connect(cfg.db_path)) as db:
        scanned = corpus.scan(cfg, db)
        result = convert_stage.run(cfg, db, corpus.select(cfg, db), convert_missing=False)
        records = reindex_records(cfg, db)
    print(f"Rebuilt {cfg.db_path} from files.")
    _print_scan(scanned)
    _print_convert(result)
    print(f"stage records re-indexed: {records}")
    return 0


def _print_convert(result: dict) -> None:
    print(f"filings: {result['filings']:,}  converted: {result['converted']:,}  "
          f"newly converted: {result['newly_converted']:,}  Markdown tokens: {result['markdown_tokens']:,}")
    for status, n in sorted(result["conversion_status"].items()):
        print(f"  {status}: {n:,}")
    for problem in result["errors"][:20]:
        print(f"  ! {problem['filing_id']}: {problem['error']}")


def cmd_status(cfg, args) -> int:
    info = state.summary(cfg.db_path)
    if info is None:
        print(f"No status database at {cfg.db_path}. Run `python -m pipeline scan` or `rebuild-index` first.")
        return 1
    print(f"Filings: {info['filings']:,}   converted: {info['converted']:,}   "
          f"Markdown tokens (o200k_base text tokens): {info['markdown_tokens']:,}")
    print("Forms: " + ", ".join(f"{k} {v:,}" for k, v in info["forms"].items()))
    print("Scan status: " + ", ".join(f"{k} {v:,}" for k, v in info["scan_status"].items()))
    print("Conversion status:")
    for status, n in info["conversion_status"].items():
        print(f"  {status}: {n:,}")
    print("Stages:")
    for stage, counts in info["stages"].items():
        print(f"  {stage}: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
    if info["last_run"]:
        run = info["last_run"]
        print(f"Last run: #{run['run_id']} {run['command']} started {run['started']} "
              f"finished {run['finished'] or '(running or interrupted)'} exit {run['exit_code']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__)
    parser.add_argument("--config", type=Path, default=config.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="Register new filings from 10K_Filings/*/filings.csv").set_defaults(func=cmd_scan)
    convert = sub.add_parser("convert", help="Index existing Markdown; convert filings that have none")
    _filters(convert)
    convert.add_argument("--reconvert-changed", action="store_true",
                         help="Rebuild Markdown for filings whose raw file changed since conversion")
    convert.set_defaults(func=cmd_convert)
    segment = sub.add_parser("segment", help="Split Markdown into reading chunks")
    _filters(segment)
    segment.add_argument("--force", action="store_true", help="Redo filings that are already done")
    segment.set_defaults(func=cmd_segment)
    for name, func, text in [("classify", cmd_classify, "Label every chunk with the local model"),
                             ("all", cmd_all, "convert + segment + classify for the selected filings")]:
        command = sub.add_parser(name, help=text)
        _filters(command)
        command.add_argument("--force", action="store_true", help="Redo filings that are already done")
        command.add_argument("--fake-model", action="store_true",
                             help="Use the test stand-in instead of a local model (labels everything UNCERTAIN)")
        command.add_argument("--fake-delay", type=float, default=0.0,
                             help="Seconds per fake request, to watch progress in the monitor")
        command.set_defaults(func=func)
    sub.add_parser("status", help="Show per-stage counts").set_defaults(func=cmd_status)
    sub.add_parser("rebuild-index", help="Recreate the status database from files").set_defaults(func=cmd_rebuild_index)
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    if args.command in {"convert", "segment", "classify", "all"}:
        control.clear_stop(cfg)  # a stop request applies only to the run that was active when it was made
    return args.func(cfg, args)
