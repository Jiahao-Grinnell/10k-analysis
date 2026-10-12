# TASKS.md

Source of truth for progress. Rules: AGENTS.md. Behavior: SPEC.md. Design: IMPLEMENTATION_PLAN.md (§ numbers below refer to it).

Status flow: todo → in progress → ready for review → done (or blocked)

## Protected throughout Iteration 1

These must not change during any task:
- `convertmd.py` is unchanged, byte for byte (SHA-256 `c02774c2…3823`), so `--resume` keeps reusing `Markdown_10K/`.
- `10K_Filings/` and the existing files in `Markdown_10K/` are never modified.
- All 24 existing tests in `tests/test_conversion.py` keep passing.

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
  - Not done in this task: raw-file hash re-verification. That belongs to `pipeline verify` (TASK-08); `verify_outputs.py` still covers it today.
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
- **Status:** todo

### TASK-03 — Monitor (Flask, read-only)
- **Goal:** The user can watch a run in the browser and inspect any filing's results.
- **Files:** new: `monitor/app.py`, `monitor/templates/*.html`, `monitor/static/monitor.js`; changed: `requirements.txt` (Flask 3.1.3).
- **Dependencies:** TASK-02.
- **What it does:**
  - Pages: overview (per-stage counts and tokens), current run (filing, chunk i/n, speed, estimated time left, last update), filing list with filters (company / year / status / stage), filing detail (stages, errors, chunks with labels and reasons, original text of a chunk), error list.
  - Data access: the DB is opened read-only; files are looked up by filing ID only; all text is escaped.
  - Pages auto-refresh while a run is active and binds to 127.0.0.1.
- **Acceptance criteria:**
  - While `pipeline all --limit 3` runs with the fake client (with an artificial delay), the current-run page updates.
  - When idle, the overview shows correct counts.
  - Opening a filing shows its chunks, labels and reasons, and clicking a chunk shows its original text as plain text.
  - An unknown filing ID returns 404, and path-like IDs cannot read other files.
- **Tests to add:** `test_monitor.py` using Flask `test_client()` against a temporary DB and output folder: each page, filters, 404, HTML in filing text is escaped, traversal attempt rejected.
- **Status:** todo

### TASK-04 — Structural segmentation (Part → Item → subsection → note → table)
- **Goal:** Replace the single `UNSTRUCTURED` section with real document structure for all four raw formats, without ever using keywords for relevance.
- **Files:** new: `pipeline/segment/html_hints.py`, `legacy_hints.py`, `structure.py`; changed: `chunks.py`, `stage.py`; new fixtures in `tests/fixtures/`; new `tests/expected/`.
- **Dependencies:** TASK-02.
- **What it does** (§1.1 steps 2–8):
  - **Heading hints:**
    - HTML/iXBRL: raw-HTML heading candidates with formatting and anchor-target features, aligned to Markdown blocks by text order. `convertmd.HtmlConverter.prepare()` is imported, not changed.
    - Legacy: line-level hints inside fenced blocks.
  - **Structure:**
    - table-of-contents and prose mentions are excluded as boundaries;
    - Part/Item/Note hierarchy, with EX-13 as its own top-level node;
    - table units keep caption, unit line and footnotes together;
    - oversized tables are split by rows with the header repeated in the model input.
  - **Fallback:** filings with no structure use `segmentation_quality=LOW`.
  - Bump `stage_version` to `segment-2`, so all filings are re-segmented once.
- **Acceptance criteria:**
  - The lossless join still holds on all fixtures and on a real sample of at least 40 filings (each format × each era).
  - On fixtures, a TOC table, an anchor-linked TOC, a "see Item 7" sentence and a legacy TOC create no boundaries.
  - For at least 8 real filings (each raw format, each era), the detected Item start positions match the boundary lists the user checked, in `tests/expected/`.
  - No table is separated from its caption, unit line or footnotes; split tables repeat the header.
  - The share of filings with LOW quality is reported per format and era in the segmentation summary.
- **Tests to add:** `test_segment_items.py`, `test_segment_tables.py`, `test_segment_legacy.py`, `test_segment_real.py` (skips when the corpus is missing).
- **Status:** todo

### TASK-05 — Keep rules and corpus reports
- **Goal:** Kept text follows the SPEC rules at the section level, and the corpus report shows what was kept.
- **Files:** new: `pipeline/classify/crossref.py`, `pipeline/reports.py`; changed: `pipeline/classify/rules.py`, `stage.py`.
- **Dependencies:** TASK-04.
- **What it does:**
  - **Section expansion:** if any chunk is KEEP or UNCERTAIN, the whole section is kept (`kept_by=section`).
  - **Cross-references:** references in kept text (`Note N`, `Item N`, `Exhibit 13`) are resolved to sections of the same filing and kept (`kept_by=reference:…`). Unresolved references are listed.
  - **Model input:** includes the heading path and the end of the previous chunk as context.
  - **Corpus report** (`classification_report.csv` and `classification_summary.json`):
    - per-filing status, tokens before and after, share kept;
    - kept tokens per Item;
    - failures;
    - a list of filings where nothing is kept in Item 7 or 8.
- **Acceptance criteria:**
  - In fixtures, a section with one KEEP chunk is kept whole.
  - A kept chunk that says "see Note 7" causes Note 7 to be kept, with the reason recorded.
  - The report contains all fields above, and the Items 7/8 list is correct on fixtures.
  - Classification reruns (because the rules version changed), while segmentation is not redone.
- **Tests to add:** `test_rules.py`, `test_crossref.py`, `test_reports.py`.
- **Status:** todo

### TASK-06 — Local model registry, download, memory check, server
- **Goal:** Any local model from Hugging Face can be added, downloaded, checked against the 14 GB budget and served, with no code change.
- **Files:** new: `pipeline/models/registry.py`, `fetch.py`, `server.py`, `vram.py`; `models.toml` (the 4 candidates from §1.3 with repo, file and sha256 filled in from current model pages).
- **Dependencies:** TASK-02.
- **What it does:**
  - CLI: `models list | fetch | check | serve` (§1.3).
  - Fetch: stdlib download with resume and SHA-256 check; asks before downloading and shows size and free disk.
  - Memory: estimate checked against `vram_budget_mib`; real peak read from `nvidia-smi`.
  - Server: started on 127.0.0.1 with the entry's settings; the pipeline waits for `/health`.
  - The llama.cpp build is pinned in `config.toml`.
- **Needs user approval before:** downloading the llama.cpp CUDA build and the first model.
- **Acceptance criteria:**
  - Adding a new entry to `models.toml` makes it appear in `models list` without any code change.
  - `models check` refuses a configuration over budget and suggests fewer slots, a smaller context or a smaller quantization.
  - With one approved model: `models serve` starts it, and `pipeline all --filing <real filing>` classifies a real filing.
  - Peak GPU memory is recorded, and results land under `outputs/classify/<model_tag>/`.
- **Tests to add:** `test_models.py` covering registry parsing, memory estimate and refusal, a fetch resume and hash mismatch using a local fake HTTP server, and server start with a stub executable.
- **Status:** todo

### TASK-07 — Labeled sample and model evaluation
- **Goal:** Measure candidate models against the user's labels and record the choice.
- **Files:** new: `pipeline/selection/sample.py`, `labels.py`, `evaluate.py`; changed: `requirements.txt` (openpyxl 3.1.5).
- **Dependencies:** TASK-05, TASK-06.
- **What it does:**
  - `select-sample` picks 12 filings, stratified by format × era across companies: 8 dev and 4 test.
  - `export-labels` writes one `.xlsx` per filing.
  - `import-labels` reads the files back; blank rows count as unlabeled.
  - `evaluate --model <tag>` reports chunk and section recall, tokens dropped, speed, peak GPU memory, error rate and projected corpus hours.
  - A comparison report covers all evaluated models.
- **User work in this task:** label the 12 spreadsheets; choose the model after seeing the report.
- **Acceptance criteria:**
  - Label files open in Excel, and round-trip without losing text or IDs.
  - The report lists all metrics above for each evaluated model.
  - The chosen model reaches section-level recall ≥ 95% on the 4 test filings.
  - The choice (links, file SHA-256, quantization, llama.cpp build, prompt version) is written to `config.toml` and IMPLEMENTATION_PLAN.md §1.3.
- **Tests to add:** `test_selection.py` covering stratified sampling, xlsx round trip, and metric computation with FakeClient and known labels.
- **Status:** todo

### TASK-08 — Pilot run and corpus verification
- **Goal:** Prove the full pipeline works on real data with the chosen model before any full-corpus run.
- **Files:** changed: `pipeline/cli.py` (`verify` command), `pipeline/reports.py`.
- **Dependencies:** TASK-03, TASK-07.
- **What it does:**
  - `pipeline verify` re-checks:
    - raw file and Markdown hashes against the records;
    - output hashes;
    - the lossless join;
    - one label per chunk;
    - one filing per request.
  - Pilot: about 20 real filings across formats and eras, run with the chosen model and watched in the monitor.
  - The pilot is interrupted once and resumed.
  - Measured speed and a full-corpus time estimate are recorded here.
- **Acceptance criteria:**
  - The pilot completes, and `verify` reports 0 problems.
  - The interrupted filing is redone with no duplicates.
  - The monitor shows correct live progress during the pilot.
  - The user reviews 3 pilot filings' kept text and finds no obvious missing relevant section, or the misses are logged as bugs with tests.
  - The full-corpus time estimate is written here.
- **Tests to add:** `test_verify.py` (detects a changed raw hash, a broken join, a missing label, a mixed-filing request).
- **Status:** todo

---

## Fast Lane changes
(none yet)
