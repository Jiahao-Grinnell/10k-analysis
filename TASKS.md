# TASKS.md

Source of truth for progress. Rules: AGENTS.md. Behavior: SPEC.md. Design: IMPLEMENTATION_PLAN.md (§ numbers below refer to it).

Status flow: todo → in progress → ready for review → done (or blocked)

## Decisions log

- **2026-10-12 — scheduled tasks run in the cloud.**
  - The final extraction (Codex or Claude scheduled tasks) cannot read this laptop's files, so clean files must be handed over to a location the cloud task can read, and results brought back.
  - Proposed: batches pushed to a separate private GitHub repo, one branch per batch, results committed back as JSON, then the batch branch is deleted.
  - Waiting for the user to confirm: (1) that clean filing text may leave the laptop (SEC filings are public); (2) the handoff location; (3) which cloud (Claude or Codex).
  - This will become a task after TASK-08 (TASK-08b).
- **2026-10-12 — TASK-09 (local models) stays in its place, but everything before it is designed for real models.** Concretely:
  - Chunk size (TASK-06) must fit the tier-2 context per request: prompt + definitions + context + chunk within `ctx_per_slot` (planned 8k tokens), with room for the answer. Chunk token counts stay exact (o200k_base) and also allow a model-tokenizer margin.
  - Segmentation, keep rules and compaction must not depend on which model is used. Only the classify stage and its outputs are keyed by model tag, so changing or comparing models reruns classify → compact → publish and never segmentation.
  - The fake client and the real client keep the same interface (`complete`, `describe`, `tag`). Every rule built with the fake model (failures → UNCERTAIN, one filing per request, verbatim kept text) is enforced in stage code, not in the client.
  - Reports and the monitor already keep results per model tag. TASK-07/08 must do the same (kept tokens and clean files per model), so results from several models can sit side by side.
  - Timings are recorded per request (`requests.jsonl`), so throughput with real models can be measured from the same outputs.

## Protected throughout Iteration 1

These must not change during any task:
- `convertmd.py` is not edited, except for the approved TASK-05 change (`FORMS`, `VERSION`, `COMPATIBLE_CHECKED_HASHES`; now 1.2.0, SHA-256 `1512c2b63d4a…`). Any further change needs the user's approval and must keep reusing every existing output.
- `10K_Filings/` and the existing files in `Markdown_10K/` are never modified.
- All 24 existing tests in `tests/test_conversion.py` keep passing.
- No request to a model ever contains text from more than one filing.

---

## Iteration 1 — first version

### TASK-01 — Project skeleton, status database, filing index
- **Goal:** Build a runnable `pipeline` package that knows every filing and its conversion status, without converting anything again.
- **Files:**
  - new: `pipeline/__init__.py`, `__main__.py`, `cli.py`, `config.py`, `corpus.py`, `state.py`, `records.py`, `tokens.py`, `convert_stage.py`; `config.toml`
  - changed: `pytest.ini` (writable basetemp)
- **Dependencies:** none.
- **What it does:**
  - `config.toml` holds the paths, tokenizer and `vram_budget_mib`.
  - `corpus.py` lists filings from `10K_Filings/*/filings.csv`, reusing `convertmd.discover()`, and assigns stable filing IDs.
  - `state.py` creates the SQLite tables (§2) in WAL mode.
  - `records.py` does atomic writes, record files and the skip-or-rerun decision.
  - `convert_stage.py`:
    - indexes the existing `Markdown_10K/*.audit.json` files into the DB;
    - converts only filings that have no Markdown yet (none today);
    - marks the 2 incomplete sources as `SOURCE_INCOMPLETE`.
  - CLI commands: `convert`, `status`, `rebuild-index`.
- **Acceptance criteria:**
  - `python -m pipeline convert` finishes and rewrites no existing Markdown file.
  - `python -m pipeline status` reports 2,894 converted and 2 source-incomplete filings, with their quality-status counts.
  - Deleting `runs/pipeline.sqlite` and then running `rebuild-index` restores the same counts.
  - `pytest -q` runs without the `-o addopts` workaround.
- **Tests to add:** `test_corpus.py` (IDs stable and unique), `test_records.py` (atomic write; skip/rerun decision on input, version and settings changes), `test_convert_stage.py` (fixture corpus: indexes existing results, converts only a missing file, never rewrites an existing one).
- **Status:** done (the user approved moving on to TASK-02 on 2026-10-12)
- **Verification (2026-10-12):**
  - `pytest -q`: 37 passed (24 existing + 13 new), with no basetemp workaround.
  - Real corpus, `python -m pipeline convert` (82 s, exit 0):
    - 2,896 filings: 2,894 converted (CHECKED_TEXT 1,065, REVIEW_REQUIRED 1,829) and 2 SOURCE_INCOMPLETE; 0 newly converted.
    - 588,352,290 Markdown tokens, the same figure as `conversion_summary.json`.
    - All 5,798 files in `Markdown_10K/` have the same size and modification time before and after.
  - `status` shows the counts above. Deleting `runs/pipeline.sqlite` and running `rebuild-index` restores identical counts.
  - `convertmd.py` SHA-256 is unchanged.
  - Not done in this task: raw-file hash re-verification. That belongs to `pipeline verify` (now TASK-12); `verify_outputs.py` still covers it today.
  - Decision: for the convert stage, the existing `.audit.json` serves as the per-filing record. No duplicate record files are written into `Markdown_10K/`. The corpus summary goes to `outputs/convert/conversion_index.{csv,json}`.

### TASK-02 — Minimal end-to-end workflow: segment → classify → outputs
- **Goal:** A single filing goes all the way from Markdown to labels and kept text, using simple chunking and a model client that can be faked.
- **Files:**
  - new: `pipeline/segment/blocks.py`, `chunks.py`, `stage.py`; `pipeline/classify/client.py`, `prompt.py`, `rules.py`, `stage.py`; `prompts/definitions.md`, `classify_v1.md`, `schema.json`; `tests/fixtures/` (small HTML and legacy filings)
- **Dependencies:** TASK-01.
- **What it does:**
  - **Segmentation (simple first version):**
    - Markdown is split into blocks with exact offsets (§1.1 step 1).
    - Chunks are built from whole blocks up to `chunk_max_tokens`, all under one `UNSTRUCTURED` section.
    - The lossless check is enforced.
  - **Model client:**
    - The client works with any OpenAI-compatible server and uses JSON schema output.
    - `FakeClient` is available for tests.
  - **Classification:**
    - Filings are processed strictly one at a time, with up to N parallel requests for chunks of the same filing.
    - Failures and invalid output become UNCERTAIN.
    - It writes the per-filing outputs: `labels.jsonl`, `requests.jsonl`, `kept.md`, `summary.json`, and the record written last.
    - Resume works for both stages; a prompt change reruns classification only.
  - **Prompt:** `prompts/definitions.md` copies the definitions table from the .docx verbatim.
  - **CLI:** `segment`, `classify`, `all`, with `--filing/--company/--limit/--resume`.
- **Acceptance criteria:**
  - Running `python -m pipeline all --filing <id>` with the fake client produces readable outputs for one HTML and one legacy fixture.
  - For every segmented filing, the chunks joined in order equal the Markdown exactly.
  - Every chunk has exactly one label, and failures show up as UNCERTAIN with an error.
  - Every request entry has exactly one filing ID.
  - `kept.md` consists only of verbatim slices, in order.
  - A second run skips the finished filing; editing the prompt reruns classification only.
  - A crash in the middle of a filing leaves no record; the next run redoes that filing with no duplicate rows.
  - Raw and Markdown fixtures have the same hashes before and after the run.
- **Tests to add:** `test_blocks.py` (offsets and lossless join on all fixtures), `test_classify.py` (failure → UNCERTAIN, request isolation, verbatim kept text, crash mid-filing), `test_e2e.py` (full run plus resume).
- **Status:** done (the user asked to proceed to the next task on 2026-10-12)
- **Verification (2026-10-12):**
  - `pytest -q`: 51 passed. That is 14 new tests, including a lossless check on a spread sample of about 25 real Markdown files.
  - Real filings, `pipeline all --fake-model --filing ...`:
    - Apple 2025 (HTML) gave 56 chunks; Apple 1996 (legacy text) also ran.
    - All 5 output files were written for each.
    - A second run skipped both filings.
  - Full corpus, `pipeline segment` (about 12 min):
    - 2,894 filings segmented and 2 source-incomplete, with 0 failures.
    - The lossless check is enforced for every filing: a mismatch would have failed it.
    - 382,553 chunks in total; median 107 and max 1,853 chunks per filing.
    - Chunk token sum is 588,440,244. It is 0.015% higher than the whole-file count because token boundaries differ at chunk edges.
  - Startup: discovering 2,896 filings takes about 8.6 s, now done once per command.
- **Decisions and notes:**
  - Finished filings are skipped automatically; `--force` redoes them. There is no `--resume` flag (AGENTS.md updated).
  - `--fake-model` labels every chunk UNCERTAIN, and its results go to `outputs/classify/fake/`. With `backend = "openai"` and no `model_tag`, `classify` stops and explains what to configure.
  - `prompts/definitions.md` holds the .docx categories table verbatim. The .docx line "Items: 1,6,7,8,14,15" is deliberately left out of the prompt so the model is not steered toward particular Items (see SPEC §1).
  - A record left by an older run is deleted before a filing is redone. A filing interrupted mid-classification therefore shows `running` in the DB until the next run.

---

**Scope change (2026-10-12).** This tool is the preparation step before final extraction:
- Downloading is done by the user's own code. All filings live in `10K_Filings/`, and new companies keep being added.
- This tool converts the filings, segments them, removes clearly irrelevant sections with local models, compacts what is kept, and publishes clean files plus a work list. Codex or Claude scheduled tasks then do the final extraction.
- The target is about 5,000 companies, 1993 to the present, forms 10-K, 10-K405, 10-KSB(405), 10-KT and amendments. The first 100 companies are the development sample (SPEC §0, IMPLEMENTATION_PLAN §0.1).

The tasks below replace the earlier TASK-03 to TASK-08.

### TASK-03 — Registry and incremental scan
- **Goal:** New filings added to `10K_Filings/` are registered quickly, and every command selects filings from the DB instead of rescanning the disk.
- **Files:** changed: `pipeline/state.py` (registry columns, indexes), `pipeline/corpus.py` (`scan`, DB selection), `pipeline/convert_stage.py`, `pipeline/cli.py`; tests.
- **Dependencies:** TASK-02.
- **What it does** (§1.4):
  - **`scan`:**
    - reads the manifests, registers rows not yet known, and uses the manifest `sha256` (no file hashing);
    - a missing file becomes `missing_source`;
    - a changed `sha256` sets `source_changed` and forces the later steps to redo;
    - amendments are marked and linked.
  - **DB selection:** every other command selects from the DB, with filters for company/CIK, year, form and stage status.
  - **`rebuild-index`:** `scan` plus re-indexing of per-filing records.
- **Acceptance criteria:**
  - On the development corpus, a repeated `scan` registers 0 new filings in a few seconds.
  - `status` and `segment --filing X` start in under 2 s (about 8.6 s today).
  - Adding a fixture company folder and rescanning registers exactly its filings.
  - A manifest that lists a missing file yields exactly one failed filing.
  - An edited manifest `sha256` causes a rerun of that filing only.
  - Development-corpus IDs and outputs are unchanged.
- **Tests to add:** `test_scan.py` (new rows only, missing file, changed source, amendment link), timing recorded in the verification note.
- **Status:** done (reviewed and approved by the user, 2026-10-12)
- **Verification (2026-10-12):**
  - `pytest -q`: 58 passed (7 new in `test_scan.py`; the existing tests moved to the DB registry).
  - **Real corpus.** The old DB was migrated by the first scan (2,896 rows updated in about 5 s). Afterwards:

    | Command | Result | Time |
    |---|---|---|
    | repeated `scan` | 0 new, 2,896 unchanged | 2.3 s (was 8.6 s via `discover()`) |
    | `status` | — | 1.5 s |
    | `segment --filing <Apple 2025>` | — | 1.9 s |

    Python startup alone is about 0.8 s on this machine while it is busy. Another long-running Python process (anaconda env `dev`, not part of this project) was using CPU.
  - **Development outputs unchanged:** `segment` on the full corpus reported 2,894 `already_done` and 2 source-incomplete, so filing IDs and outputs were untouched.
  - **Rebuild:** `rebuild-index` (drop DB → scan → index conversions → re-index stage records) restored identical counts:
    - convert: done 2,894, source_incomplete 2;
    - segment: done 2,894, source_incomplete 2;
    - classify [fake]: done 2.
  - `convertmd.py` SHA-256 is unchanged.
- **Decisions and notes:**
  - **Fast rescans:** `scan` reads `10K_Filings/*/filings.csv` itself instead of `convertmd.discover()`, which resolves every path. It applies the same rules: listed statuses, paths must stay inside the raw folder, supported extensions, case-insensitive collisions. It uses the manifest `sha256` and hashes a file only when the manifest has none.
  - **Changed raw files:** a raw file that changed after conversion is reported, not reconverted. `convert --reconvert-changed` rebuilds those Markdown files on request. This is the one exception to "never rewrite Markdown", and it runs only when the user asks.
  - **Missing files:** a missing raw file is registered as `missing_source` and fails only itself. The next scan picks it up once the file exists.
  - **`all` scans first;** other commands scan only if nothing is registered yet. A `--form` filter was added; `--company` also matches the CIK.
  - **Faster startup:** stage modules are imported lazily, and `records.py` has its own `atomic_text` (same behaviour as convertmd's), so `status` and `scan` no longer load the converter.
  - **Test timing:** the first test in a run that uses a temp folder spends 15–40 s in setup. That is pytest cleaning its temp folder on this busy machine, not pipeline code. A full run takes 1–2.5 min.

### TASK-04 — Monitor (Flask, read-only)
- **Goal:** The user can watch long runs in the browser and inspect any filing's results, problems and token savings.
- **Files:** new: `monitor/app.py`, `monitor/templates/*.html`, `monitor/static/monitor.js`, `pipeline/disk.py`; changed: `requirements.txt` (Flask 3.1.3).
- **Dependencies:** TASK-03.
- **What it does:**
  - **Overview:** per-stage counts; token totals for each step (raw → Markdown → kept → clean) as they become available; free disk space with a warning below `min_free_gb`.
  - **Current run:** filing, chunk i/n, speed, estimated time left, last update.
  - **Filing list:** filters for company/CIK, year, form, status and stage.
  - **Filing detail:** stages, errors, chunks with label, reason and (later) tier-1 score, and token counts per step. Clicking a chunk shows its original text.
  - **Problem lists:** conversion failures, missing sources, UNCERTAIN-by-error chunks, (later) compaction fallbacks, and nothing-kept filings.
  - **Safety:** read-only DB; files looked up by filing ID only; escaped text; auto-refresh; binds to 127.0.0.1.
- **Acceptance criteria:**
  - During `pipeline all --fake-model --limit 3` (with an artificial delay), the current-run page updates.
  - When idle, the overview is correct.
  - A filing's chunks and text are viewable.
  - An unknown ID returns 404, and path-like IDs cannot read other files.
- **Tests to add:** `test_monitor.py` (Flask `test_client()`, temp DB: each page, filters, 404, escaping, traversal, disk warning with patched free space).
- **Status:** done (the user asked to proceed to the next task on 2026-10-12)
- **Verification (2026-10-12):**
  - `pytest -q`: 67 passed (9 new in `test_monitor.py`). They cover:
    - every page, filters, 404 for unknown and path-like IDs;
    - escaped filing HTML, the low-disk banner, active-run progress and the problem lists;
    - the monitor never writing to the DB;
    - the 503 message when no DB exists.
  - **Live run:** `python -m monitor` was serving while `pipeline all --fake-model --fake-delay 0.5 --company ABBV --limit 3` ran. `/api/status` sampled every 5 s showed:
    - filings 0/3 → 1/3 → 2/3, then "no active run";
    - the current filing and chunk counts advancing (55/74 → 20/85 → 60/85 → 5/110 → 85/110).
  - **Pages on the real 2,896-filing DB** all return 200:
    - overview 0.9 s; filings 0.5 s; problems 0.2 s;
    - the largest filing detail (Realty Income 2017, 1,853 chunks) 1.6 s; chunk text 0.2 s.
    - `/filing?id=..%2F..%2Fconfig.toml` returns 404.
- **Decisions and notes:**
  - Start with `python -m monitor`. It is fixed to 127.0.0.1:5000 with debug off. `flask --app monitor.app run` also works.
  - **Pages:** Overview, Runs, Filings (filters plus 200 rows per page), Filing detail, Chunk text, Problems, and `/api/status` (JSON used by the auto-refresh).
  - Supporting pipeline changes:
    - `runs` records `planned` and `model_tag` (for progress and time left);
    - `filings.raw_tokens` comes from the audits (for the raw → Markdown → kept token steps; clean files appear with TASK-08);
    - classify records "N chunks failed and were kept as UNCERTAIN" on the stage row;
    - `--fake-delay` slows the fake model so progress can be watched;
    - `[disk] min_free_gb = 50` in `config.toml`.
  - A run killed without finishing shows as "interrupted?" once it has been idle for 10 minutes.
  - The monitor tests share one prepared corpus (module fixture) to keep them under a minute.

### TASK-04b — Start and stop processing from the monitor (simple version)
- **Goal:** The user can start processing all registered filings, or a filtered subset, with a button in the monitor, watch it, and stop it. The command line stays fully equivalent.
- **Files:**
  - new: `monitor/runner.py`, `monitor/templates/control.html`, `pipeline/control.py`;
  - changed: `monitor/app.py`, `monitor/templates/base.html`, `overview.html`, `monitor/queries.py`, `pipeline/cli.py`, the stage `run()` loops (convert, segment, classify).
- **Dependencies:** TASK-04.
- **What it does** (IMPLEMENTATION_PLAN §1.10):
  - **Control page:**
    - a Start form with company/year/form/limit filters and a fake-model option, which launches `pipeline all` as a subprocess and writes its log;
    - Stop after current filing (stop-request file);
    - Force stop (terminate);
    - the tail of the log.
  - **One run at a time:** Start is refused while a web run or a command-line run is active.
  - **Safety:** POST requests with a token, local requests only; the DB stays read-only for the page.
- **Acceptance criteria:**
  - Start from the page processes the selected filings, and progress shows on the overview.
  - A second Start while running is refused with a message.
  - "Stop after current filing" ends the run after the filing in progress: the run's exit code is 3, and the remaining filings stay untouched.
  - Starting again continues with the unfinished filings and skips the done ones.
  - Force stop ends the process, and the next start redoes the interrupted filing.
  - A POST without the token is rejected.
- **Tests to add:** `test_control.py` (stop file stops each stage between filings; runner start/refuse/stop/force-stop with a real subprocess on the fixture corpus; token check).
- **Status:** done (approved by the user, 2026-10-12)
- **Verification (2026-10-12):**
  - `pytest -q`: 85 passed. The 7 new tests in `test_control.py` cover:
    - each stage stopping before the next filing (classify finishes the filing in progress);
    - a leftover stop request being cleared by a new command;
    - argument validation;
    - the token requirement;
    - with real subprocesses on the fixture corpus: start, refusal of a second start, stop then resume, and force stop then redo.
  - **Live on the real DB** (monitor at 127.0.0.1:5000, actions sent as the page's form POSTs):
    - a POST without the token returned 400;
    - Start for MSFT (limit 3, fake model) launched `pipeline all` in the background, and an immediate second Start was refused;
    - "Stop after current filing", pressed after the first filing, ended that run with exit code 3 (run #11);
    - the next Start continued with the remaining 2 filings (runs #12–15);
    - a further Start skipped all 3 as already done.
  - `convertmd.py` was not changed by this task.
- **Decisions and notes:**
  - **Force stop kills the whole process tree.** On Windows a venv's `python.exe` is a launcher that runs the real interpreter as a child, so terminating only the launcher left the run going. This was found by the force-stop test. Force stop now uses `taskkill /PID <pid> /T /F`.
  - **Recorded PIDs:**
    - every `runs` row records the PID of the process doing the work, so a run whose process is gone shows as "interrupted" immediately and no longer blocks Start;
    - the web run's PID is checked together with its creation time, so a reused PID is never killed.
  - **Exit code 3** means "stopped on request", and the monitor shows such runs as "stopped on request".
  - The monitor writes only `runs/web_run.json`, `runs/stop.request` and `runs/logs/web_*.log`. It never writes the status DB.

### TASK-04c — Clearer runs page and a structure view per filing (user request, 2026-10-13)
- **Goal:**
  - The user can see at a glance what each run processed, how far each step got, and the result.
  - The user can inspect how any filing was segmented.
- **Files:**
  - new: `monitor/templates/_job.html`, `structure.html`;
  - changed: `monitor/queries.py`, `app.py`, templates `runs.html`, `overview.html`, `control.html`, `filings.html`, `filing.html`, `base.html`, `static/monitor.js`;
  - pipeline: `state.py` (`runs.job_id`, `stage_status.detail`), `cli.py` (an `all` job row grouping its steps), `segment/stage.py` and `reindex.py` (a structure summary in `detail`);
  - removed: `monitor/templates/_run.html`.
- **What it does:**
  - **Runs page:**
    - one row per action, so an `all` command and its scan/convert/segment/filter steps show as one job;
    - each row shows the selection in plain words, step chips with filings done / planned, a result pill (Finished / Finished with errors / Stopped on request / Interrupted / Running), duration, and expandable step details;
    - the running job appears on top as a four-step pipeline with progress bars, the current filing and chunk, speed and time left;
    - older runs recorded before job IDs existed are grouped by identical arguments.
  - **Structure page (`/structure?id=…`):**
    - summary cards: structure found, Items, notes, subsections, chunks;
    - a document map bar (width = length, one colour per Item or attached document; click to jump);
    - when classified, a chunk bar coloured by KEEP/UNCERTAIN/DROP;
    - a collapsible outline (document → Part → Item → Note → subsection) with tokens, chunk counts, label counts and links to each chunk's text.
  - **Filings list and filing page:**
    - the list gets a "Structure" column (complete / partial / not found, chunk count) linking to the structure page;
    - the filing page links to it.
- **Status:** done (approved by the user, 2026-10-26)
- **Verification (2026-10-13):**
  - `pytest` monitor and control tests: 16 passed. The new assertions cover: one history row for an `all` command, the structure page (map, outline, chunk links, labels), 404 for unknown IDs, "not segmented yet", the structure links in the filings list, and the running-job panel.
  - Live on the real DB: `/runs`, `/structure` (Apple 1995 and Apple 2025), `/filings`, and `/` all return 200 in 0.07–0.35 s. The running re-segmentation shows as "all registered filings · Segment n/2,896".

### TASK-05 — Converter: extra forms (changes `convertmd.py`; needs user approval of the diff)
- **Goal:** 10-KSB, 10-KSB405, 10-KT and amendments convert, and no existing output is reconverted.
- **Files:** changed: `convertmd.py` (only `FORMS`, `VERSION`, `COMPATIBLE_CHECKED_HASHES`), `verify_outputs.py` (accept compatible hashes); new test fixtures.
- **Dependencies:** TASK-03.
- **What it does** (§1.5): shows the diff to the user, then applies it.
- **Acceptance criteria:**
  - SGML fixtures of each new form convert to CHECKED_TEXT.
  - `convertmd.py --resume` on the full development corpus reuses every existing output: 0 Markdown files change (sizes and modification times compared).
  - `verify_outputs.py` passes.
  - All existing tests pass.
- **Tests to add:** `test_conversion_forms.py` (per-form fixtures, compatible-hash resume).
- **Status:** done (the user approved the diff and asked to commit and push on 2026-10-12)
- **Verification (2026-10-12):**
  - **`convertmd.py` is now 1.2.0** (SHA-256 `1512c2b63d4a…`). Only `VERSION`, `COMPATIBLE_CHECKED_HASHES` (adds the 1.1 hash `c02774c2…`) and `FORMS` changed. `verify_outputs.py` now accepts outputs whose converter, and whoever verified them, is current or in the compatible set.
  - `pytest -q`: 78 passed. The 11 new tests in `test_conversion_forms.py` cover:
    - 10-KSB, 10-KSB405, 10-KT, 10-KT405, 10-K/A, 10-KSB/A and 10-KT/A convert to CHECKED_TEXT;
    - 20-F still counts as "no annual report";
    - a 1.1 output is reused under `--resume` with its Markdown unchanged;
    - `verify()` accepts 1.0 and 1.1 outputs and rejects an unknown converter;
    - a 10-KSB goes through scan → convert → segment → classify.
  - **Development corpus:**
    - `verify()` passes (2,896 audits, 0 problems). It was called as a function, so `Markdown_10K/corpus_validation.json` was not rewritten.
    - The `--resume` dry run with the new compatible set: 2,894 reused, 2 placeholders would be regenerated with identical text.
    - `pipeline convert`: 0 newly converted, 588,352,290 tokens.
    - All 5,798 files in `Markdown_10K/` have the same size and modification time before and after.
- **Findings before the change (read-only):**
  - **Form types present today:** the development corpus contains only 10-K (2,713) and 10-K405 (183) filings. A raw-file search found no `<TYPE>10-KSB` or `<TYPE>10-KT` documents, so widening `FORMS` cannot change any existing conversion.
  - **Audit hashes:** 2,891 audits have `converter_sha256` = v1.0 (`6f5ee1ee…`) with `verified_by` = v1.1. The other 5 were converted by v1.1 (`c02774c2…`).
  - **Dry-run of `--resume` with the proposed compatible set** (script in the session scratchpad, no writes):
    - 2,894 outputs would be reused;
    - 2 SOURCE_INCOMPLETE placeholders would be regenerated with identical text, because placeholders have no document checks.
    - The pipeline's own convert stage never re-runs the converter on existing outputs.
  - **Placeholder message:** the `IncompleteSource` text "no 10-K/10-K405 primary document" is deliberately left unchanged, so regenerated placeholders stay byte-identical.

### TASK-06 — Structural segmentation (Part → Item → subsection → note → table)
- **Goal:** Replace the single `UNSTRUCTURED` section with real document structure for all raw formats and forms, without ever using keywords for relevance.
- **Files:** new: `pipeline/segment/html_hints.py`, `legacy_hints.py`, `structure.py`; changed: `chunks.py`, `stage.py`; new fixtures in `tests/fixtures/` and `tests/expected/`.
- **Dependencies:** TASK-02, TASK-05.
- **Needs user action:** add a batch of companies outside the first 100 to `10K_Filings/`, covering 10-KSB, 10-KT, amendments, small companies and every era.
- **What it does** (§1.1 steps 2–8):
  - **Heading hints:** raw-HTML hints aligned by text order; legacy line hints.
  - **Item structure:**
    - TOC and prose mentions are excluded as boundaries;
    - Part/Item/Note hierarchy;
    - 10-KSB numbering recognized on its own terms;
    - EX-13 as its own node.
  - **Tables:** table units keep caption, unit line and footnotes together; split tables repeat the header in the model input.
  - **Fallback:** filings with no structure use `segmentation_quality=LOW`.
  - Bump `stage_version` to `segment-2`.
- **Acceptance criteria:**
  - The lossless join holds on all fixtures and every registered filing.
  - TOC and prose fixtures create no boundaries.
  - Item start positions match the boundaries the user checked in `tests/expected/` for at least 8 development and 8 external filings. Together these cover every raw format, every era, 10-KSB and 10-KT.
  - Tables are never separated from their caption, unit line or footnotes.
  - The LOW-quality share is reported by form, format and era.
- **Tests to add:** `test_segment_items.py`, `test_segment_tables.py`, `test_segment_legacy.py`, `test_segment_real.py` (skips without the corpus).
- **Status:** done (approved by the user, 2026-10-26; the 8 reference lists in `tests/expected/` are confirmed). One check is deferred:
  - The 8 external-filing reference lists wait for companies outside the first 100 to be added to `10K_Filings/`; they will be added then (at the latest with the external sample in TASK-11).
- **Verification (2026-10-12):**
  - `pytest -q`: 104 passed. There are 19 new tests:
    - `test_segment_items.py` covers: a TOC table, a paragraph TOC with page numbers, "see Item 7" in prose, "Continued" page headers, spacer-row heading tables, a minimal-HTML body with `<br>` headings, notes ("NOTE 1" + title, "Note 2 –", "3. Title"), Items 7/8 for 10-KSB, EX-13 notes, the UNSTRUCTURED fallback, and chunks never crossing an Item or Note.
    - `test_segment_tables.py`: caption, unit line and footnotes stay with their table at every budget; a split pipe table repeats its header in the model input only.
    - `test_segment_legacy.py`: legacy TOC lines and wrapped "Item 8 on this Form" prose are not boundaries; boundaries inside a fence split it, and the model input stays fenced.
    - `test_segment_real.py`: 8 real filings match their reference lists exactly. These lists come from my own check of the Markdown and are awaiting the user's confirmation. The 8 filings cover:
      - Apple 2025 (iXBRL), Apple 1996 (legacy), Apple 2002 (minimal-HTML fallback);
      - ADM 2006 (pipe-table headings), Atmos 2002 (legacy, numbered notes);
      - Cisco 2015 (spacer-row tables), Realty Income 2017 (largest filing), Wells Fargo 2020 (Items in EX-13).
  - **Full corpus, `segment --force` (34.7 min, 0 failures):**
    - 2,894 filings, 470,496 chunks, 588,436,815 tokens; every filing passed the lossless check;
    - chunk tokens: median 1,345, p95 1,975, maximum 2,000 (none over the limit);
    - sections per filing: median 143.
  - **Structure quality:**

    | | HIGH | MEDIUM | LOW |
    |---|---|---|---|
    | Filings | 2,826 (97.7%) | 30 | 38 |

    | Era / format | HIGH / filings |
    |---|---|
    | ≤2000 legacy | 459/490 |
    | 2001–08 HTML | 576/591 |
    | 2009–18 HTML | 959/961 |
    | 2019+ iXBRL | 685/699 |

    - Notes were found in 2,065 filings (71%).
    - LOW filings are mostly companies with a non-standard layout: McDonald's and Morgan Stanley use a cross-reference index with topic-based bodies; Nucor and Wells Fargo in the 1990s wrap the annual report. They still get subsection splits and full classification.
  - Downstream works unchanged on the new segments. `all --fake-model` reclassified Apple 2025 automatically because its segments changed, and the monitor shows the new heading paths.
  - `convertmd.py` was not changed.
- **Decisions and notes:**
  - **Markdown only:** structure comes from the converter's Markdown; raw-HTML hints were not needed (IMPLEMENTATION_PLAN §1.1 "As built").
  - **Subsection level added** under Items and Notes. Small subsections share chunks; chunks never cross Part, Item, Note or attached-document boundaries.
  - **Cost of the finer chunking:** 23% more chunks than segment-1 (470k vs 383k), and so more model requests. The benefit: TASK-07 can keep or drop per subsection instead of per Item.
  - **Two fixes found by checking real filings:**
    - the TOC-run rule ends a run when Item numbering steps back, since the TOC's last line can sit right before the body's Item 1;
    - notes need a median gap of at least 800 characters, so a "1. Financial Statements 2. Schedules 3. Exhibits" list is not taken for notes.
  - Whole-corpus segmentation now takes about 30–35 minutes on this machine (it was 12 minutes without structure). It runs CPU-only and can be resumed.

### TASK-07 — Keep rules and filtering reports
- **Goal:** Kept text follows the SPEC rules at the section level, and the reports show what was kept.
- **Files:** new: `pipeline/classify/crossref.py`, `pipeline/reports.py`; changed: `pipeline/classify/rules.py`, `stage.py`.
- **Dependencies:** TASK-06.
- **What it does:**
  - **Section expansion:** if any chunk is KEEP or UNCERTAIN, the whole section is kept.
  - **Cross-references:** referenced notes, Items and exhibits in the same filing are kept; unresolved references are listed.
  - **Model input:** heading path plus the end of the previous chunk as context.
  - **Report:** per filing: kept tokens per Item and failures; a list of filings with nothing kept in Item 7 or 8.
- **Acceptance criteria:**
  - A section with one KEEP chunk is kept whole.
  - "See Note 7" causes Note 7 to be kept, with the reason recorded.
  - The report is correct on fixtures.
  - A rules change reruns classification only.
- **Tests to add:** `test_rules.py`, `test_crossref.py`, `test_reports.py`.
- **Status:** done (approved by the user, 2026-10-26)
- **Verification (2026-10-26):**
  - `pytest -q`: 122 passed. The 18 new tests:
    - `test_rules.py`: one KEEP chunk keeps its whole section; failures and UNCERTAIN do too; "see Note 7" keeps Note 7 with the reason recorded, one step only; a reference kind can be switched off; the model input has the heading path and the end of the previous chunk; a keep-rule change reruns classify only, sends no request and keeps the request log; every chunk has one row with its keep decision.
    - `test_crossref.py`: numbered references, lists and ranges are found; debt notes ("6.5% Notes due 2030"), percentages and years are not; each reference gets a status (front matter, self, follow, locator, unresolved, index); an index of notes and a fixed-width exhibit list are not followed, but "Intangible assets (Note 7)" in a statement row is; an exhibit reference keeps the attached EX-13; a non-attached exhibit is `not_in_filing`.
    - `test_reports.py`: kept tokens per Item and per keep reason; alerts for MD&A / statements Items with nothing kept or not found (6/7 for 10-KSB); the corpus report is weighted and its CSV files are written.
    - `test_monitor.py`: the filing page says why a DROP chunk was kept.
  - One existing test was changed on purpose: `test_classify.py::test_kept_text_is_verbatim_and_in_order` now expects DROP chunks kept with their section (SPEC F4). Its checks that kept text is verbatim and in order are unchanged.
  - **Real filings:** `classify --fake-model --company AAPL --force` on 32 filings: report files written; 2 alerts (2004: Item 8 not found; 2008: Item 7 not found). Spot checks on Apple 2025: "refer to Note 7" → Note 7 followed; "Notes … in Part II, Item 8" → Item 8 treated as a locator; Item headings → self. The monitor's filing, chunk, structure, overview and problems pages return 200 on the real DB.
  - **Reference statistics on all 2,894 filings** (read-only script, every section treated as kept, so this is an upper bound):

    | Kind | Followed | Unresolved | Resolved share | Not followed (front matter / self / index / locator) |
    |---|---|---|---|---|
    | Note | 112,911 | 20,253 | 84.8% | 30,438 |
    | Item | 31,149 | 1,597 | 95.1% | 122,677 |
    | Exhibit | 3,478 | (104,800 not attached) | 100% of attached | 9,084 |

    - Unresolved notes are concentrated in 926 filings, mostly those where segmentation found no notes (29% of filings), plus lettered notes ("Note A").
    - Text that followed references point to, as a share of all text: notes 25%, Items 55%, exhibits 2.6%. With a real model only kept text is a source, so the real share is lower; but Item references ("see Item 1A", "Item 8") can keep whole large Items. `[keep] reference_kinds` can switch a kind off, and only classify reruns, without model requests. To be decided with real labels in TASK-11.
    - Speed: at most 1.7 s per filing (Realty Income) for finding and resolving references.
- **Decisions and notes:**
  - `final_label` and `kept_by` keep the model's answer; the keep decision is in `kept`, `kept_reason`, `kept_detail`.
  - Reference statuses and the rules that skip them are listed in IMPLEMENTATION_PLAN §1.2 "As built in TASK-07".
  - The model input (heading path + end of previous chunk) already existed since TASK-02; a test now covers it.
  - Classify outputs from before this task (`classify-1`) are redone on the next run; for the fake model this costs nothing.

### TASK-08 — Compaction and published output for scheduled tasks
- **Goal:** Each filing gets a validated, compacted clean file, a provenance map and a row in the work list that the scheduled tasks consume. Weighted token savings are reported for every step.
- **Files:** new: `pipeline/compact/rules.py`, `validate.py`, `stage.py`, `pipeline/publish.py`; changed: `pipeline/cli.py` (`compact`, `publish`, `compact --profile`), `pipeline/reports.py`, `config.toml` (`[compact]`).
- **Dependencies:** TASK-07. It works with the fake model, so no GPU is needed.
- **What it does** (§1.7–1.8):
  1. **Profile:** `compact --profile` measures each candidate rule's saving on the kept text by raw format. Only rules that save tokens are enabled.
  2. **Compact:** apply the enabled rules to the kept sections; validate the visible-text sequence, numbers with signs, and cell grids; fall back to the uncompacted text on any failure or when there is no gain.
  3. **Publish:**
     - `clean.md` with a YAML header and heading paths once per section;
     - `clean.map.json` with provenance;
     - `clean_index.csv` / `.jsonl` rewritten atomically, with nothing-kept filings explicitly marked;
     - `clean_summary.json` with weighted savings: Markdown → kept, kept → clean, Markdown → clean, raw → clean.
- **Acceptance criteria:**
  - Every rule has positive and negative tests (fenced text, `<pre>`, multi-paragraph cells, spans, `$`/`%` cells).
  - On the development corpus with the fake model (everything kept), the following are reported per format with weighted totals: compaction savings, validation failures and fallbacks.
  - Every clean file has a header, and every part maps back to exact Markdown ranges.
  - A filing with nothing kept says so in its header and in the index.
  - A compaction-rule change reruns compact and publish only.
- **Tests to add:** `test_compact.py`, `test_publish.py`, `test_reports.py` (savings math).
- **Status:** todo

### TASK-09 — Local model registry, download, memory check, servers
- **Goal:** Any local model from Hugging Face can be added, downloaded, checked against the 14 GB budget and served, with no code change. This covers both the tier-1 scorer and the tier-2 instruction model.
- **Files:** new: `pipeline/models/registry.py`, `fetch.py`, `server.py`, `vram.py`; `models.toml`.
- **Dependencies:** TASK-02.
- **What it does** (§1.3):
  - CLI: `models list | fetch | check | serve`. Each entry has a `role`; both servers run together on different ports.
  - Memory: the combined estimate for the loaded models is checked against `vram_budget_mib`.
  - The llama.cpp build is pinned.
- **Needs user approval before:** downloading the llama.cpp CUDA build and the first models.
- **Acceptance criteria:**
  - A new `models.toml` entry appears without any code change.
  - `check` refuses an over-budget combination and suggests what to change.
  - With approved models, both servers start and one real filing is filtered end to end.
  - Peak GPU memory is recorded.
- **Tests to add:** `test_models.py` (registry, memory estimate and refusal, fetch resume and hash mismatch with a local fake HTTP server, server start with a stub executable).
- **Status:** todo

### TASK-10 — Two-tier filtering
- **Goal:** Tier 1 scores every chunk quickly; tier 2 labels only the undecided chunks. Recall is protected by thresholds and fallbacks.
- **Files:** new: `pipeline/classify/score.py`; changed: `pipeline/classify/stage.py`, `rules.py`, `config.toml`.
- **Dependencies:** TASK-07, TASK-09.
- **What it does** (§1.6):
  - **Tier 1:** scores each chunk against each category definition; the score is the maximum over categories.
  - **Thresholds:** `t_keep` and `t_drop` (part of the settings hash) decide which chunks tier 1 settles; the rest go to tier 2.
  - **Failures:** a tier-1 failure sends the chunk to tier 2.
  - **Labels:** rows record `tier1_score`, `decided_by`, the models and the thresholds.
  - **Fallback:** with `t_drop` unset, every chunk goes to tier 2.
- **Acceptance criteria:**
  - Every chunk has a tier-1 score or a recorded tier-1 failure, and exactly one final label.
  - Chunks between the thresholds, and every chunk that failed tier 1, reach tier 2.
  - A threshold change reruns filtering, compact and publish only.
  - Requests never mix filings.
- **Tests to add:** `test_two_tier.py` (fake scorer and fake tier-2 client).
- **Status:** todo

### TASK-11 — Labeled sample (development + external) and model evaluation
- **Goal:** Measure tier-1 and tier-2 candidates and thresholds against the user's labels, including filings from companies outside the first 100, and record the choice.
- **Files:** new: `pipeline/selection/sample.py`, `labels.py`, `evaluate.py`; changed: `requirements.txt` (openpyxl 3.1.5).
- **Dependencies:** TASK-10, and external companies present in `10K_Filings/`.
- **What it does:**
  - **Sample:** 12 development filings (8 tuning, 4 held out) and about 12 external filings, stratified by form, format, era and size.
  - **Labels:** `export-labels` writes `.xlsx` files; `import-labels` reads them back.
  - **Evaluation:** `evaluate` reports recall (development and external separately), tokens removed, speed, GPU memory, error rate and projected time for the target corpus, for tier-1 thresholds and tier-1 + tier-2 combinations.
- **User work in this task:** label the spreadsheets; choose the models and thresholds after seeing the report.
- **Acceptance criteria:**
  - Label files round-trip without losing text or IDs.
  - The report has all metrics above.
  - The chosen combination reaches section-level recall ≥ 95% on the held-out development filings and on the external filings.
  - The choice is recorded in `config.toml` and IMPLEMENTATION_PLAN §1.3/§1.6.
- **Tests to add:** `test_selection.py` (sampling, xlsx round trip, metrics with fake clients and known labels).
- **Status:** todo

### TASK-12 — Streaming run, pilot at scale, corpus verification
- **Goal:** Prove the whole system works on several hundred unseen filings with the chosen models, and measure the time, disk use and token savings of the full 5,000-company run.
- **Files:** new: `pipeline/flow.py`; changed: `pipeline/cli.py` (`run`, `verify`), `pipeline/reports.py`.
- **Dependencies:** TASK-04, TASK-08, TASK-11.
- **What it does:**
  - **`pipeline run`:**
    - a CPU convert + segment worker runs ahead;
    - the GPU path filters, compacts and publishes one filing at a time;
    - disk guard;
    - clean stop on Ctrl+C (§1.9).
  - **`pipeline verify`:** checks input and Markdown hashes against the records, output hashes, the lossless join, one label per chunk, one filing per request, and compaction validation records.
  - **Pilot:**
    - several hundred filings from companies outside the first 100;
    - interrupted once and resumed;
    - watched in the monitor.
- **Acceptance criteria:**
  - The pilot completes and `verify` reports 0 problems.
  - The interrupted filing is redone with no duplicates.
  - The user reviews 3 pilot clean files and finds no obvious misses, or misses are logged as bugs with tests.
  - Measured throughput, disk use per filing, failure rates by stage and form, weighted token savings at each step, and the projected time and disk for the full target are written here.
- **Tests to add:** `test_flow.py` (fake models, prefetch ordering, disk-guard pause), `test_verify.py` (changed hash, broken join, missing label, mixed-filing request, failed compaction check are detected).
- **Status:** todo

---

## Fast Lane changes

### 2026-10-26 — Segmentation bug: a sentence cut by a page break hid a real Item heading (user report)
- **Found by:** the TASK-07 alert list (Apple 2004: Item 8 not found; Apple 2008: Item 7 not found). The user saw Notes 11–13 listed under Item 7A in the monitor.
- **Cause:** a page break left "Item 8 of this Form 10-K in order to …" as its own short paragraph before Item 7. The Item heading pattern accepts "Item N" + space + a capitalised title, but the whole pattern was case-insensitive, so the lowercase "of" also passed. That earlier false Item 8 then blocked the real one (Items must increase).
- **Fix:** `pipeline/segment/structure.py`: the title after "Item N " must start with a real capital letter. `segment` stage version → `segment-4`.
- **Checks:**
  - New test `test_segment_items.py::test_sentence_cut_by_a_page_break_is_not_an_item_heading` failed before the fix and passes after it. `pytest -q`: 123 passed; the 8 user-confirmed reference lists are unchanged.
  - Read-only comparison of all 2,894 filings before/after: 9 filings change, all only gain Items that were missed: Apple 2004 (Item 8), Apple 2008 (Item 7), HCA 1993/1998/1999/2000 (Items 11–14), Oracle 2001 (Items 11–14), UnitedHealth 2006 (Item 9A), Amazon 2007 (gains 7A; Item 8 moves from a footnote in MD&A to the real heading). No Item was lost; all 9 stay HIGH.
  - `segment` on all filings: 2,894 done, 0 failures, quality unchanged (2,826 / 30 / 38). `classify --fake-model --company AAPL`: only the 2 changed filings rerun; 0 alerts.
- **Seen, not fixed:** in Apple 2004 the converter's Markdown writes note headings as "Note 11Segment Information" (no space), so no notes are found there and "see Note 11" stays unresolved. To be discussed separately.
