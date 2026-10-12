"""Command line: python -m pipeline <command> [options]."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from pipeline import config, convert_stage, corpus, state
from pipeline.classify import client as clients
from pipeline.classify import stage as classify_stage
from pipeline.segment import stage as segment_stage


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--filing", help="One filing ID, e.g. AAPL_Apple_Inc_CIK0000320193/2025_2025-10-31_0000320193-25-000079")
    parser.add_argument("--company", help="Ticker, company folder or company_id")
    parser.add_argument("--year", type=int, help="Report year")
    parser.add_argument("--limit", type=int, help="Process at most N filings (after other filters)")


def _args(args) -> dict:
    """Command options for the runs table (without the cached filing list or handler)."""
    return {k: v for k, v in vars(args).items() if not k.startswith("_") and k != "func"}


def _selected(cfg, args) -> list:
    if getattr(args, "_filings", None) is None:  # discovery takes seconds; do it once per command
        args._filings = corpus.list_filings(cfg.raw_dir, cfg.markdown_dir)
    chosen = corpus.select(args._filings, args.filing, args.company, args.year, args.limit)
    if not chosen:
        raise SystemExit("No filings match the given filters")
    return chosen


def cmd_convert(cfg, args) -> int:
    filings = _selected(cfg, args)
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "convert", _args(args))
        print(f"convert: {len(filings)} filings (existing Markdown is indexed, never rewritten)", flush=True)
        result = convert_stage.run(cfg, db, filings)
        code = 1 if result["errors"] else 0
        state.finish_run(db, run_id, code)
    _print_convert(result)
    return code


def cmd_segment(cfg, args) -> int:
    filings = _selected(cfg, args)
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "segment", _args(args))
        print(f"segment: {len(filings)} filings (finished filings are skipped unless --force)", flush=True)
        result = segment_stage.run(cfg, db, filings, force=args.force)
        code = 1 if result["failures"] else 0
        state.finish_run(db, run_id, code)
    print(f"segment: {result['counts']}")
    return code


def cmd_classify(cfg, args, client=None) -> int:
    filings = _selected(cfg, args)
    client = client or clients.from_config(cfg, fake=args.fake_model)
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "classify", _args(args))
        print(f"classify: {len(filings)} filings with model '{client.tag}', one filing at a time", flush=True)
        try:
            result = classify_stage.run(cfg, db, filings, client, force=args.force, run_id=run_id)
        except BaseException:
            state.finish_run(db, run_id, 130)
            raise
        code = 1 if result["failures"] else 0
        state.finish_run(db, run_id, code)
    print(f"classify [{client.tag}]: {result['counts']}")
    return code


def cmd_all(cfg, args) -> int:
    client = clients.from_config(cfg, fake=args.fake_model)  # fail early if no model is configured
    codes = [cmd_convert(cfg, args), cmd_segment(cfg, args), cmd_classify(cfg, args, client)]
    return max(codes)


def cmd_rebuild_index(cfg, args) -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
    filings = corpus.list_filings(cfg.raw_dir, cfg.markdown_dir)
    with closing(state.connect(cfg.db_path)) as db:
        result = convert_stage.run(cfg, db, filings, convert_missing=False)
    print(f"Rebuilt {cfg.db_path} from files.")
    _print_convert(result)
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
        print(f"No status database at {cfg.db_path}. Run `python -m pipeline convert` or `rebuild-index` first.")
        return 1
    print(f"Filings: {info['filings']:,}   converted: {info['converted']:,}   "
          f"Markdown tokens (o200k_base text tokens): {info['markdown_tokens']:,}")
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
    convert = sub.add_parser("convert", help="Index existing Markdown; convert filings that have none")
    _filters(convert)
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
        command.set_defaults(func=func)
    sub.add_parser("status", help="Show per-stage counts").set_defaults(func=cmd_status)
    sub.add_parser("rebuild-index", help="Recreate the status database from files").set_defaults(func=cmd_rebuild_index)
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    return args.func(cfg, args)
