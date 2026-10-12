"""Command line: python -m pipeline <command> [options]."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from pipeline import config, convert_stage, corpus, state


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--filing", help="One filing ID, e.g. AAPL_Apple_Inc_CIK0000320193/2025_2025-10-31_0000320193-25-000079")
    parser.add_argument("--company", help="Ticker, company folder or company_id")
    parser.add_argument("--year", type=int, help="Report year")
    parser.add_argument("--limit", type=int, help="Process at most N filings (after other filters)")


def _selected(cfg, args) -> list:
    filings = corpus.list_filings(cfg.raw_dir, cfg.markdown_dir)
    chosen = corpus.select(filings, args.filing, args.company, args.year, args.limit)
    if not chosen:
        raise SystemExit("No filings match the given filters")
    return chosen


def cmd_convert(cfg, args) -> int:
    filings = _selected(cfg, args)
    with closing(state.connect(cfg.db_path)) as db:
        run_id = state.start_run(db, "convert", vars(args))
        print(f"convert: {len(filings)} filings (existing Markdown is indexed, never rewritten)", flush=True)
        result = convert_stage.run(cfg, db, filings)
        code = 1 if result["errors"] else 0
        state.finish_run(db, run_id, code)
    _print_convert(result)
    return code


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
    sub.add_parser("status", help="Show per-stage counts").set_defaults(func=cmd_status)
    sub.add_parser("rebuild-index", help="Recreate the status database from files").set_defaults(func=cmd_rebuild_index)
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    return args.func(cfg, args)
