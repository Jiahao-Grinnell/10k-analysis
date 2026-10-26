# 10-K cleanup and pre-filtering pipeline

A local tool that prepares SEC annual reports for extracting **intangible investment**:
- research & development;
- advertising and brand;
- organizational capital;
- intellectual property.

It is the step **before** final extraction. It takes filings that have already been downloaded and reduces them, one filing at a time, to the smallest text that still contains everything relevant:

```text
10K_Filings/  ──scan──►  registry  ──convert──►  Markdown  ──segment──►  chunks
      ──filter (local models)──►  kept text  ──compact──►  clean files  ──►  Codex / Claude scheduled tasks
```

- **Out of scope:** the tool does **not** download filings (separate code does that), and it does **not** extract amounts. Codex or Claude scheduled tasks extract from the clean files.
- **Local models** only remove sections that are *clearly* irrelevant. Anything relevant or uncertain is kept. Recall matters more than size.
- **Everything runs on one Windows laptop.** No filing text is sent to an external service, and nothing is deployed.
- **Target scale:** about 5,000 companies, 1993 to the present. Forms: 10-K, 10-K405, 10-KSB(405), 10-KT and amendments. The current 100 companies in `10K_Filings/` are the development sample.

## Status

| Step | State |
|---|---|
| Incremental scan of `10K_Filings/` into a status database | done |
| Conversion to Markdown (`convertmd.py` 1.2.0, including 10-KSB / 10-KT / amendments) | done |
| Segmentation into lossless reading chunks | done; structural Part / Item / note segmentation is next (TASK-06) |
| Classification pipeline with a fake model (for testing) | done |
| Web monitor, with start / stop buttons | done |
| Keep rules, compaction and clean output files | planned (TASK-07, TASK-08) |
| Local models, two-tier filtering, model selection, pilot at scale | planned (TASK-09 to TASK-12) |

Progress and verification notes are in [TASKS.md](TASKS.md).

Development corpus today: 100 companies, 2,896 filings, 588M Markdown tokens (o200k_base), and 382,553 chunks. All chunks join back to their Markdown exactly.

## Requirements

- Windows, PowerShell, Python 3.12.
- [uv](https://docs.astral.sh/uv/) to manage the virtual environment (`.venv` has no pip).
- Later, for the local models: an NVIDIA GPU (developed on an RTX 5000 Ada, 16 GB), llama.cpp and GGUF models from Hugging Face. No Docker or WSL is needed.

```powershell
uv venv --python 3.12 .venv                       # only in a new checkout
uv pip install --python .venv -r requirements.txt
```

The first time `tiktoken` loads an encoding it needs network access. After that it uses the cache in `.cache/tiktoken`.

## Quick start

```powershell
$py = '.\.venv\Scripts\python.exe'

& $py -m pipeline scan                                   # register filings in 10K_Filings/ (only new ones on later runs)
& $py -m pipeline all --fake-model --company AAPL        # scan + convert + segment + classify, test model
& $py -m pipeline status                                 # counts per stage
& $py -m monitor                                         # web monitor at http://127.0.0.1:5000
```

`--fake-model` stands in for the local model until one is set up. It labels every chunk UNCERTAIN, so it keeps everything. It proves the pipeline runs end to end, but its labels mean nothing.

## Commands

All commands: `& '.\.venv\Scripts\python.exe' -m pipeline <command> [options]`.

| Command | What it does |
|---|---|
| `scan` | Reads `10K_Filings/*/filings.csv` and registers new filings.<ul><li>A missing raw file is recorded as `missing_source`.</li><li>A raw file whose SHA-256 changed is flagged `source_changed`.</li><li>Amendments are linked to the filing they amend.</li></ul> |
| `convert` | Indexes existing Markdown and converts filings that have none. Existing Markdown is never rewritten. `--reconvert-changed` rebuilds Markdown only for filings whose raw file changed. |
| `segment` | Splits each filing's Markdown into reading chunks of at most about 2,000 tokens. Every filing is checked: the chunks must join back into the Markdown byte for byte. |
| `classify` | Labels every chunk KEEP, DROP or UNCERTAIN, one filing at a time. Failures count as UNCERTAIN and are kept. Then applies the keep rules: a section with any KEEP or UNCERTAIN chunk is kept whole, and numbered cross-references in kept text ("see Note 7", "Item 8", "Exhibit 13") keep their target in the same filing. Writes a filtering report (`filter_report.json` / `.csv`, `filter_alerts.csv`, `unresolved_references.csv` in `outputs/classify/<model>/`). When only the keep rules change, the stored model answers are reused and no request is sent. |
| `all` | `scan`, then `convert` + `segment` + `classify` for the selected filings. |
| `status` | Counts by stage, form and conversion status; total tokens; the last run. |
| `rebuild-index` | Recreates the status database from the files on disk. |

Options:
- Filters: `--filing ID`, `--company` (ticker, company folder, company_id or CIK), `--year`, `--form`, `--limit N`.
- `--force` redoes filings that are already done.
- `--fake-model` (with `--fake-delay SECONDS` to slow it down for the monitor).

**Resume.** Finished filings are skipped automatically. A filing counts as done only when its inputs, code version and settings (prompt, model and so on) still match its record. Changing a later step reruns only that step and the steps after it. A filing interrupted halfway is redone from the start.

## Monitor

`python -m monitor` serves a page on `http://127.0.0.1:5000`, reachable from this computer only. It reads the status database read-only.

**Run control.** Processing can be started and stopped from the **Control** page instead of the command line:
- **Start processing** runs the same `pipeline all` command in the background, with optional filters (company, year, form, max filings). Leave every field empty to process everything still unfinished; filings that are already done are skipped.
- **Stop after current filing** finishes the filing in progress and stops cleanly. Press Start again to continue where it left off.
- **Force stop now** ends the process at once. The filing in progress is redone next time.
- **One run at a time:** only one run can be active, including runs started from the command line. The page shows the log of the last run it started.

| Page | Shows |
|---|---|
| Overview | Per-stage counts, tokens at each step, free disk space (warning below 50 GB), the current run with progress and time left |
| Runs | Recent runs, with speed and filings completed |
| Filings | Search and filters (company, CIK, year, form, stage, status) |
| Filing detail | Metadata, amendment link, stage status, tokens per step, every chunk with its label and reason; click a chunk to read its original text |
| Problems | Missing or changed sources, failed stages, chunks kept after a model error, filings with nothing kept, interrupted filings |
| Control | Start / stop processing, and the log of the last run started from the page |

## Folders

| Path | Content | In git |
|---|---|---|
| `10K_Filings/<company>/filings.csv` + files | Raw filings from the downloader (read-only for this tool) | no |
| `Markdown_10K/<company>/<file>.md` + `.audit.json` | Converted filings | no |
| `outputs/convert/` | Conversion index (CSV / JSON) | no |
| `outputs/segments/<company>/<file>.segments.jsonl` + `.record.json` | Chunks with exact character ranges | no |
| `outputs/classify/<model>/<company>/<file>.*` | `labels.jsonl`, `requests.jsonl`, `references.jsonl`, `kept.md`, `summary.json`, `record.json` | no |
| `outputs/classify/<model>/filter_report.*`, `filter_alerts.csv`, `unresolved_references.csv` | Kept tokens per Item, reasons, reference statistics; filings whose MD&A or financial statements Item kept nothing or was not found | no |
| `runs/pipeline.sqlite` | Status database (rebuildable from files) | no |
| `config.toml` | Paths, chunk size, model settings, keep rules (`[keep] reference_kinds`), disk threshold, GPU budget | yes |
| `prompts/` | Category definitions (from the research document), classification prompt, answer schema | yes |
| `pipeline/`, `monitor/`, `tests/` | Code | yes |

## Research rules

These rules apply to every stage:
- **One filing at a time:** each filing is processed independently. No model request ever contains text from two filings, and no batch API is used.
- **No keyword matching:** keywords never decide relevance. Structural rules (document boundaries, headings, tables, markup) are allowed.
- **Every chunk is read:** every chunk is read by a local model. Sections are never picked by keyword or by a fixed list of Items.
- **Recall first:** errors, timeouts and uncertain answers are kept, and a whole section is kept if any part of it is kept.
- **Original text only:** selected text stays original; it is never summarized.
- **Missing is not zero:** a filing with nothing kept is marked as such, never reported as zero investment.

## The converter (`convertmd.py`)

`convertmd.py` converts each filing into its own Markdown file and records token counts before and after. It is a conservative converter written with `lxml` (not a wrapper around `sec2md`). It handles four formats:
- SGML complete submissions;
- early fixed-width text;
- HTML;
- inline XBRL.

It preserves complex financial tables. The conversion, audit and token-report workflow is adapted from [Jiahao-Grinnell/10-k-analysis](https://github.com/Jiahao-Grinnell/10-k-analysis).

The pipeline's `convert` command calls the converter for new filings. It can also be run on its own:

```powershell
& '.\.venv\Scripts\python.exe' convertmd.py --input '10K_Filings' --output 'Markdown_10K' --workers 8 --resume
& '.\.venv\Scripts\python.exe' convertmd.py --input 'some-10-K.htm' --output 'test_outputs/single'
```

Options:
- `--limit N` converts only the first N filings.
- `--workers N` parallelizes local conversion only (no LLM calls).
- `--tokenizer` can be repeated to count with several encodings; the default is `o200k_base`.
- `--resume` reuses an output when the raw content, converter, tokenizer and output hashes match. Outputs from earlier compatible versions (1.0, 1.1) whose text, number and table checks all passed are reused too.

**Accepted forms (1.2.0).** 10-K, 10-K405, 10-KSB, 10-KSB405, 10-KT, 10-KT405 and their `/A` amendments. A file with only an SEC header, or without one of these main documents, produces an explicit placeholder marked `SOURCE_INCOMPLETE`. Placeholders are not counted as successes.

**What it keeps**
- The full readable body: table of contents, risk factors, financial statements, notes and so on. Nothing is removed by keyword.
- SGML `<DOCUMENT>` records are split first. The main report and readable text exhibits are kept, including EX-13, which may hold the full annual report.
- Technical attachments (EX-27, EX-101 / XML, binary images) are excluded and recorded in the audit. Images keep their reference and alt text; they are not OCR'd.
- HTML loses styles, scripts, technical headers and hidden XBRL. Visible inline XBRL numbers are kept exactly as written.
- Tables:
  - simple rectangular tables become Markdown tables;
  - tables with merged cells, nesting or irregular shapes stay as minimal HTML with `rowspan` / `colspan`.
- Legacy TXT filings become fenced text with their spacing, negative-number parentheses and footnote markers intact.
- Encodings (UTF-8, UTF-16, declared charsets, CP1252 / Latin-1) are decoded explicitly; characters are never dropped.

**Checks per document**
- The visible text matches the re-rendered Markdown (ignoring whitespace).
- No numeric occurrence is missing.
- Table cell order and merged-cell structure match.

If plain Markdown fails these checks, the converter falls back to minimal HTML and checks again. `CHECKED_TEXT` means all checks passed. Images, parsing or encoding diagnostics, absolute positioning or failed checks give `REVIEW_REQUIRED`. The checks do not prove identical browser rendering, and they do not replace review of the financial meaning.

**Outputs**
- per filing: `.md` and `.audit.json`;
- corpus files: `conversion_report.csv`, `conversion_summary.json`, `conversion_report.md`, and `conversion_progress.jsonl` (an append-only log).

`verify_outputs.py` re-checks every raw, output and audit hash, plus the check results and token totals, and writes `corpus_validation.json`.

**Token savings** are reported against two baselines: the whole raw file, and the raw text of the retained documents only. Totals are weighted: `1 − total Markdown tokens / total baseline tokens`. These are local `tiktoken` text-token counts, not billing figures.

## Tests

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
```

85 tests. They need no GPU, model or network: the classifier tests use a fake model client. Tests that read the real corpus are skipped when `10K_Filings/` or `Markdown_10K/` is missing. A full run takes about 1–3 minutes. Most of that is pytest cleaning its temporary folder.

## Project documents

| File | Purpose |
|---|---|
| [SPEC.md](SPEC.md) | What the tool must do, and its acceptance checklist |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | How it is built: design, storage, models, testing |
| [TASKS.md](TASKS.md) | Task list, status and verification notes |
| [AGENTS.md](AGENTS.md) | Standing rules for coding agents working on this repository |

## Limitations (current)

- **No real model yet:** only `--fake-model` works, because no local model is configured. It keeps everything, so no tokens are saved yet beyond the Markdown conversion.
- **No structural segmentation yet:** chunks are not yet grouped into Part / Item / note sections. Every chunk sits under one `UNSTRUCTURED` section until TASK-06.
- **No clean output yet:** compaction and the clean files for scheduled tasks are not built yet (TASK-08).
- **Not yet tested outside the sample:** the tool has been run only on the 100-company development sample.
