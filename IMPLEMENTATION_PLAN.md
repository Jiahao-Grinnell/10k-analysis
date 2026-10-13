# IMPLEMENTATION_PLAN.md

How the pipeline in SPEC.md is built. Standing rules are in AGENTS.md.

## 0. Starting point (inspected 2026-10-12)

**Code that already exists**
- `convertmd.py` (v1.1.0) converts raw filings to `Markdown_10K/`. It writes a `.audit.json` per filing plus `conversion_report.csv` and `conversion_summary.json`.
  - Its `--resume` option reuses an output when the stored converter hash matches the current `CODE_HASH` (SHA-256 of `convertmd.py`) or is in `COMPATIBLE_CHECKED_HASHES` with all checks passed.
- `verify_outputs.py` re-checks the converted corpus; `tests/test_conversion.py` holds 24 tests for the converter.
- The venv was created with **uv** (`.venv`, Python 3.12.14) and contains no pip.

**What the Markdown keeps and loses** (this matters for segmentation)
- Only real `<h1>`–`<h6>` become Markdown headings.
- Bold, font size and centering are removed, so styled HTML headings become plain paragraphs.
- Internal anchor links (`href="#..."`) and element ids are dropped. Table-of-contents link targets therefore exist only in the raw HTML.
- Legacy fixed-width filings are written entirely as fenced text blocks, so their headings sit inside fences.

**Machine**
- NVIDIA RTX 5000 Ada Laptop, 16,376 MiB, driver 581.95 (CUDA 13.0); 64 GB RAM; Windows 11.
- 566 GB free on C:.
- git 2.52.

**Already built** (TASK-01, TASK-02):
- status DB and filing index;
- convert, segment and classify stages with resume;
- FakeClient.

Measured on the full development corpus:
- Segmentation: 2,894 filings, 382,553 chunks, all lossless, in about 12 minutes.
- Discovering the 2,896 filings via `convertmd.discover()` takes about 8.6 s.

## 0.1 Purpose and target scale (revised 2026-10-12)

**Purpose.** This tool prepares filings for the final extraction, which Codex or Claude scheduled tasks perform one filing at a time. It does not download filings and does not extract amounts. The goal is the fewest tokens for the scheduled tasks without losing relevant content.

**Input.** Everything lives in `10K_Filings/` in the downloader's layout, and the user keeps adding new companies there. The first 100 companies are the development sample.

**Target scale.** About 5,000 companies, 1993 to the present. Forms: 10-K, 10-K405, 10-KSB, 10-KSB405, 10-KT and amendments.

| | Estimate |
|---|---|
| Filings | 50k–150k |
| Markdown | 30–90 GB, 10–30B tokens |
| Segmentation | 10–20 h |
| Generative classification of every chunk at 3–6k tokens/s | 1–3 months |

**Consequences for the design:**
- an incremental scan into a DB registry (§1.4);
- the converter forms change (§1.5);
- two-tier filtering (§1.6);
- compaction and published output (§1.7–1.8);
- a streaming flow with a disk guard (§1.9);
- validation on companies outside the first 100 (§1.3).

All of these numbers are re-measured in the pilot (TASK-12).

## 1. Structure and main components

The design has three parts:
- a Python package `pipeline/` that runs every stage from the command line;
- local model servers;
- a Flask monitor that reads the pipeline's status database (read-only) and can start or stop a pipeline run as a separate process.

| Component | Responsibility |
|---|---|
| `convertmd.py` | Raw → Markdown. The only planned change is accepting the extra forms (§1.5). It is never moved. |
| `pipeline/corpus.py` | Incremental `scan` of `10K_Filings/*/filings.csv` into the DB registry; selects filings from the DB (§1.4). |
| `pipeline/convert_stage.py` | Indexes existing results from their `.audit.json`. Converts newly registered filings into `Markdown_10K/`. Never rewrites an existing Markdown file. |
| `pipeline/segment/` | Markdown (+ raw HTML hints) → sections and chunks (§1.1). |
| `pipeline/classify/` | Tier 1 scores every chunk (`score.py`); tier 2 sends the undecided chunks to the instruction model. Applies the keep rules and writes per-filing labels and kept text (§1.2, §1.6). |
| `pipeline/compact/` | Deterministic compaction of the kept text, with validation and fallback (§1.7). |
| `pipeline/publish.py` | Clean file + map per filing, and the corpus index for scheduled tasks (§1.8). |
| `pipeline/selection/` | Labeled sample, label spreadsheets, evaluation of models and thresholds (§1.3). |
| `pipeline/flow.py` | Streaming run: convert/segment ahead on CPU, filter/compact/publish one filing at a time (§1.9). |
| `pipeline/disk.py` | Free-space checks; pauses the run below a threshold. |
| `pipeline/state.py` | SQLite status database: registry, per-filing stage status, runs, live progress. |
| `pipeline/records.py` | Atomic file writes, per-filing record files, and the skip-or-rerun decision. |
| `pipeline/tokens.py` | o200k_base token counting, reusing the tiktoken cache in `.cache/tiktoken`. |
| `pipeline/cli.py` | `python -m pipeline <command>`. |
| `monitor/` | Flask app with server-rendered pages and one small JS file for auto-refresh. `monitor/runner.py` starts `pipeline all` as a subprocess and stops it (§1.10). |
| `pipeline/control.py` | Stop-request file checked by every stage between filings. |
| llama.cpp `llama-server` (external) | One instance for the tier-2 instruction model (OpenAI-compatible, JSON-schema output) and one for the tier-1 scorer (embedding/rerank). Both bound to `127.0.0.1`. |

### 1.1 Segmentation

Segmentation has two layers: logical **sections** (what gets kept or dropped) and reading **chunks** (what the models read).

1. **Blocks.** Parse the Markdown into an ordered list of blocks, each with exact character start and end offsets: heading, paragraph, pipe table, HTML table, HTML list, fenced block. Every character, including blank lines, belongs to exactly one block. *(Built in TASK-02.)*
2. **Heading candidates** come from structure, by source format:
   - **HTML / iXBRL:** re-parse the raw HTML with `HtmlConverter.prepare()` (imported, not modified). Record block elements whose visible text is short and standalone, together with:
     - formatting features: bold, larger font, centered, all-caps, page break before;
     - whether the element is the target of an internal `#anchor` link.

     Then align these candidates to Markdown blocks in document order by their whitespace-compacted visible text.
   - **Legacy fixed-width:** line-level features inside fenced blocks: a short line, all-caps or title case, surrounded by blank lines, centered by leading spaces, or right after a page break.
   - **Real Markdown headings (`#`)** are used directly.
3. **Table of contents and prose.** A candidate is treated as a table-of-contents entry, not a boundary, when:
   - it sits in a table or block run whose rows are Item references followed by page numbers; or
   - its Item sequence repeats later in the document as real headings.

   Item references inside a sentence never become boundaries. For HTML, the targets of TOC anchor links are the preferred Item boundaries.
4. **Hierarchy.**
   - Part → Item → subsection → note, using numbering patterns: `PART I–IV`; `Item 1`/`1A`/`7A`… (monotonic within a Part); `Note N` / `N.` inside Item 8 or EX-13.
   - 10-KSB item numbering is recognized as its own scheme.
   - Each EX-13 document gets its own top-level node.

   These rules recognize document structure only; they never decide relevance.
5. **Table units.** A table block is grouped with:
   - up to 2 short blocks right before it (caption, unit line);
   - short footnote blocks right after it.
6. **Chunks.** Sections are split into chunks of whole blocks/units with at most `chunk_max_tokens` tokens (default 2,000).
   - An oversized table is split by rows; its header is repeated in the model input only.
   - Oversized prose is split at paragraph or line breaks. *(Basic version built in TASK-02.)*
7. **Fallback.** If no Item structure is found, the filing has a single `UNSTRUCTURED` section and `segmentation_quality=LOW`. It is still fully filtered.
8. **Check.** Joining all chunk ranges must reproduce the Markdown file byte for byte. *(Enforced since TASK-02.)*

Each chunk record contains: `filing_id`, `chunk_id`, `section_id`, `heading_path`, `item`, `start`, `end`, `tokens`, `kind`, `quality`, and fence flags.

**As built in TASK-06 (segment-2).** These points replace steps 2–6 above where they differ.
- **Heading candidates come from the Markdown alone.** Raw-HTML formatting hints were not needed: on the development corpus the Markdown gives complete structure for 97.7% of filings. The candidates are:
  - short standalone paragraphs;
  - tiny heading tables (at most one row with text; spacer rows are common);
  - paragraph-level pieces inside HTML blocks, including `<br>`-delimited lines in minimal-HTML fallback bodies;
  - paragraph-starting lines inside fenced legacy text.
- **Tables of contents** are recognized in three ways:
  - tables that list three or more different Items;
  - short Item lines that end in a page number;
  - an increasing run of closely spaced Item lines (< 400 characters apart) that covers both an early Item (< 5) and a late one (≥ 7). A step back in numbering ends the run.
- **Other non-boundaries:** "Continued" page headers are skipped. For repeated headings, the first occurrence counts. The final Item list is the longest strictly increasing chain.
- **Notes:**
  - They are looked for inside the financial-statements Item (Item 8; Item 7 for 10-KSB). If none are found there, the search runs from that Item to the end of the document (F-pages after Item 15). In EX-13, the whole exhibit is searched.
  - A notes sequence must start at 1 or 2, have at least 3 notes, and have a median gap of at least 800 characters between notes, which rejects numbered lists such as "1. Financial Statements 2. Schedules 3. Exhibits".
  - A bare "NOTE 4" takes the next line as its title.
- **Subsections (new level)** are short title-like standalone units inside the main document and EX-13: at most 100 characters and 14 words, in title case or capitals, no final period, no amounts. They must sit at least 200 characters after the previous boundary. Text that repeats three or more times in the filing (page headers and footers) is excluded.
- **Hard and soft boundaries:**
  - Attached document, Part, Item and Note are hard boundaries: a chunk never crosses them.
  - Subsections are soft: several small subsections of the same hard unit may share a chunk, and a subsection is kept whole whenever it fits in one chunk.
  - Each chunk records all `section_ids` it overlaps. Keep rules (TASK-07) expand at subsection level.
- **Per-filing outputs:** `segments.jsonl` (chunks) plus `sections.json`, which holds every section with its heading path, part/item/note keys, hard group and quality.
- **Quality:**
  - HIGH: 8 or more distinct Items found;
  - MEDIUM: 3–7;
  - LOW: fewer (a single `UNSTRUCTURED` section unless notes or subsections are found).

### 1.2 Tier-2 classification request, keep rules, outputs

**Order**
- Filings are processed strictly one after another.
- Within one filing, up to `parallel_requests` chunk requests (default 4) run at the same time against the server's slots. Every request contains text from that filing only.

**Prompt**
- Versioned files: `prompts/classify_v1.md` plus `prompts/definitions.md` (copied verbatim from the .docx table) and `prompts/schema.json`.
- The prompt says that the goal is to remove only clearly irrelevant text, and to prefer UNCERTAIN over DROP when unsure.

**Request**
- `/v1/chat/completions` with:
  - `response_format = {"type": "json_schema", "schema": ...}`;
  - `temperature 0`;
  - `chat_template_kwargs = {"enable_thinking": false}`;
  - a per-request timeout.
- Input: definitions, filing name and year, heading path, the end of the previous chunk (≤ 300 tokens), and the chunk text.

**Keep rules**
- Any failure becomes UNCERTAIN, after 1 retry.
- Section expansion: if any chunk of a section is KEEP or UNCERTAIN, the whole section is kept (`kept_by=section`).
- Cross-references (`Note N`, `Item N`, `Exhibit 13`) in kept chunks are resolved to sections of the same filing and kept (`kept_by=reference:<chunk_id>`). Unresolved references are listed.

**Outputs per filing**, written atomically, record last:
- `.labels.jsonl`
- `.requests.jsonl`
- `.kept.md` (verbatim slices with markers; the input to compaction)
- `.summary.json`
- `.record.json`

### 1.3 Model selection

**Runtime.** No Docker and no WSL. Models are downloaded from Hugging Face as plain files into `models/` and run by llama.cpp `llama-server`, native Windows CUDA build, with the build tag pinned (latest seen: b11429).
- It runs natively on Windows; vLLM does not.
- It supports JSON-schema output, parallel slots with continuous batching, prompt-prefix reuse, `/health` and `/slots`, and embedding and rerank modes.

**Tier-2 candidates** (verify current model cards at selection time):

| Candidate | Why |
|---|---|
| Qwen3.5-4B | Fast; Apache-2.0; thinking mode can be turned off. |
| Qwen3.5-9B | Stronger; a Q4/Q5 GGUF fits in 16 GB with room for the KV cache. |
| Gemma 4 E4B | Different model family, small and fast; Apache-2.0. |
| Gemma 4 12B | Stronger second family; check that Q4 plus context fits. |

**Tier-1 candidates:**
- Qwen3-Reranker-0.6B / 4B;
- Qwen3-Embedding-0.6B / 4B;
- one non-Qwen alternative (for example a BGE or GTE reranker).

Several GGUF conversions of these are community uploads, so provenance is checked at selection time.

**Model registry (`models.toml`).** Adding any model is a config change, not a code change.
- Each entry has:
  - `name` (used as the tag in output paths);
  - `role` (`scorer` or `instruct`);
  - Hugging Face `repo`, `file` and `revision`;
  - `sha256`, `quant`;
  - `ctx_per_slot`, `parallel`;
  - `chat_template_kwargs`;
  - extra server flags.
- `pipeline models list | fetch | check | serve`:
  - `fetch` uses stdlib HTTP, resumes interrupted downloads, verifies SHA-256, asks before downloading and checks free disk space;
  - `serve` starts `llama-server` on 127.0.0.1 and waits for `/health`.
- Results are stored per model tag, so testing a new model never overwrites another model's results.

**GPU memory budget (16 GB).**
- The budget for weights, KV cache and buffers is **≤ 14 GB** (`vram_budget_mib`), with tier 1 and tier 2 loaded together.
- `models check` estimates: GGUF size + KV for `ctx_per_slot × parallel` + about 1 GB per server. If the estimate is over budget, it refuses and suggests fewer slots, a smaller context or a smaller quantization. After loading, it records the real peak from `nvidia-smi`.
- Approximate weight sizes: 4B at Q8 about 4–5 GB; 9B at Q4–Q5 about 6–7 GB; 12B at Q4 about 7–8 GB; 0.6B scorer under 1 GB.
- Mixture-of-experts models with CPU expert offload are allowed only as an explicit per-model setting.

**Labeled sample**
- `select-sample` picks:
  - 12 development filings (8 for tuning, 4 held out);
  - about 12 external filings from companies added after the first 100, stratified by form (10-K, 10-K405, 10-KSB, 10-KT, amendment), raw format, era and company size.
- `export-labels` writes one `.xlsx` per filing (openpyxl): chunk ID, heading path, full chunk text (≤ about 12k characters, under Excel's cell limit), and empty `relevant` / `category` / `note` columns.
- `import-labels` reads them back. Blank rows mean unlabeled.

**Evaluation** (`pipeline evaluate`)
- Covers tier-1 alone at candidate thresholds, and tier-1 + tier-2 combinations. It reports:
  - chunk- and section-level recall, development and external separately;
  - share of tokens removed;
  - tokens/s;
  - peak GPU memory;
  - error rate;
  - projected hours for the target corpus.
- Thresholds and prompts are tuned only on the 8 development tuning filings.
- The user picks. The choice is written to `config.toml` and here, with links, file SHA-256, quantization and llama.cpp build.
- Target: section-level recall ≥ 95% on held-out development and external filings.

### 1.4 Registry and incremental scan
- **Registry columns.** The `filings` table gets `cik`, `accession`, `form`, `is_amendment`, `amends_filing_id`, `filing_date`, `period`, `raw_sha256` and `first_seen`, plus indexes on `stage_status(stage, status)` and `filings(year)`, `filings(form)`.
- **`pipeline scan`**
  - Reads every `10K_Filings/*/filings.csv` and registers rows not yet in the DB. It reads the manifest's own `sha256` column instead of hashing files.
  - Unchanged known rows cost only a dictionary lookup, so a rescan at 150k filings stays fast. It does not use `convertmd.discover()`, which resolves every path.
  - Same layout rules as `discover()`: rows with status `downloaded` or `already_present`; paths must stay inside `10K_Filings/`.
  - A row whose file is missing is registered as `missing_source` and fails only itself.
  - A known filing whose manifest `sha256` changed is flagged `source_changed`; all its later steps are redone.
- **Amendments** are marked from the form (`/A`). They are linked to the original with the same CIK and `report_date` (latest non-amendment). If no original is found, the link is left empty and the amendment is listed.
- **Everything else selects from the DB**: `convert`, `segment`, `classify`, `compact`, `publish`, `run`, `status`. Filters: company/CIK, year, form, stage status, `--limit`, `--filing`.
- `rebuild-index` = drop the DB, `scan`, then re-index the per-filing records of every stage.

### 1.5 Converter change (separate task, shown to the user first)
- Extend `FORMS` in `convertmd.py` with 10-KSB, 10-KSB405, 10-KT and their `/A` forms.
- Add the current hash `c02774c2…3823` to `COMPATIBLE_CHECKED_HASHES`, so outputs produced by v1.1 stay reusable under `--resume`.
- Bump `VERSION` to 1.2.0.
- The pipeline's convert stage indexes existing outputs from their audits and never re-runs the converter on them, so `Markdown_10K/` is not touched.
- `verify_outputs.py` is updated to accept outputs whose `converter_sha256` or `verified_by_converter_sha256` is in the compatible set.

### 1.6 Two-tier filtering

**Tier 1: every chunk.** The scorer server compares the chunk with each category definition in `prompts/definitions.md`, using the rerank or embedding endpoint. The score is the maximum over categories.

**Thresholds** (`config.toml`, part of the settings hash):
- `score ≥ t_keep` → KEEP (decided by tier 1);
- `score ≤ t_drop` → DROP (decided by tier 1);
- anything in between → tier 2.

`t_drop` is conservative. If no safe `t_drop` exists, tier 1 only orders and prioritizes, and every chunk goes to tier 2.

**Failures**
- A tier-1 failure sends the chunk to tier 2.
- A tier-2 failure becomes UNCERTAIN.

**Labels row additions:** `tier1_score`, `decided_by` (`tier1_keep`, `tier1_drop`, `tier2`, `failure`), models, thresholds.

**Order:** one filing at a time. Both tiers finish before that filing's outputs are written.

### 1.7 Deterministic compaction of kept text

**Input:** a filing's kept sections (verbatim Markdown slices). The original Markdown is never modified.

**Measure first.** `compact --profile` reports, per rule and per raw format, the tokens each rule would save on the kept text. Only rules that save tokens are enabled (`[compact] rules = [...]` in `config.toml`, part of the settings hash).

**Candidate rules**, applied only inside HTML-table islands and normal prose, never inside fenced or `<pre>` text:
1. Unwrap a cell's single presentational `<p>`/`<div>`/`<span>`. A cell with several block children keeps them, because those are real paragraph breaks.
2. Remove empty non-cell wrappers.
3. Collapse whitespace runs in HTML and prose.
4. Convert an HTML table to a pipe table when, after unwrapping, it is rectangular, without spans, nested tables, images or multi-paragraph cells.
5. Drop columns that are empty in every row. Spacer columns are common in SEC tables.
6. Optional, only if profiling shows a large saving and the checks pass: merge a cell holding only `$` or `%` into its neighbouring number cell. The visible text sequence is unchanged; the record notes the change in cell count.

**Section headers.** Each kept section starts with its heading path once (for example `## Item 7 > Results of Operations`). Per-chunk markers go to the map file, not the text.

**Validation** (per filing, against the uncompacted kept text):
- the visible-text sequence, whitespace-normalized, is equal;
- the multiset of numeric tokens, including signs and parentheses, is equal;
- the table cell grid (text + rowspan/colspan) is equal, except for the declared effect of rules 5–6, which is checked separately (only empty cells removed; `$`/`%` moved without loss).

If anything fails, that filing uses the uncompacted kept text, and the reason is recorded. If compaction saves no tokens, the uncompacted text is kept.

**Records:** the rules applied, check results, and tokens before and after.

### 1.8 Published output for scheduled tasks

**`outputs/clean/<model_tag>/<company>/<file>.clean.md`**

Header (YAML front matter, short):

```yaml
filing_id: ...      # plus company, cik, ticker, form, period, filing_date, accession
is_amendment: ...   # plus amends (filing id)
source: ...         # plus conversion_quality
kept_sections: ...  # plus tokens_markdown, tokens_clean
note: ...           # only when nothing was kept: "no relevant sections kept by the local filter - not evidence of zero"
```

Body: the compacted kept sections in document order.

**`<file>.clean.map.json`:** for each section in the clean file, its chunk IDs, character ranges in the original Markdown, labels, reasons, deciding tier, and compaction rules applied.

**`outputs/clean/<model_tag>/clean_index.csv` and `.jsonl`**
- One row per filing: metadata, status (`ready`, `nothing_kept`, `source_incomplete`, `failed`), paths, token counts (raw, markdown, kept, clean), the settings hash, and an `updated` timestamp.
- Rewritten atomically after each published filing.
- This is the work list the scheduled tasks consume. A scheduled task can record which rows it has done, keyed by `filing_id` plus the settings hash, and pick up new rows as they appear.

**`clean_summary.json`:** weighted token savings for Markdown → kept, kept → clean, Markdown → clean and raw → clean, plus per-filing distribution figures.

### 1.9 Streaming flow, disk guard, long runs
- **`pipeline run`** works on the DB selection (default: everything not yet published). It uses:
  - a CPU worker that converts and segments up to `prefetch` filings ahead (default 20);
  - the GPU path, which filters, compacts and publishes one filing at a time, finishing each before the next.
- **Disk guard:** before each filing, check free space. Below `min_free_gb` (default 50), the run pauses and the monitor warns.
- **Clean stop:** `Ctrl+C` stops after the current request. A killed process leaves no record for the unfinished filing.

### 1.10 Run control from the monitor (TASK-04b)

**Start**
- `monitor/runner.py` launches `python -m pipeline --config <config> all [filters] [--fake-model]` with `subprocess.Popen`.
- It uses the same interpreter and an argument list (no shell). stdout and stderr go to `runs/logs/web_<timestamp>.log`.
- The PID, command, log path and start time are saved in `runs/web_run.json`.

**One at a time**
- Start is refused while the saved web run's process is alive, or while the DB shows an active run (started from the command line).
- Process liveness on Windows uses `OpenProcess` + `GetExitCodeProcess` through `ctypes`. `os.kill(pid, 0)` must not be used on Windows: there it terminates the process.

**Stop after current filing**
- The runner writes `runs/stop.request`.
- `convert`, `segment` and `classify` check it before each filing. `all` checks it between stages.
- A stopped command finishes its run row with exit code 3 (stopped). Every command clears a leftover stop file when it starts.

**Force stop**
- The runner terminates the process.
- The unfinished filing has no record and is redone by the next run. Its stage row stays `running` and appears under Problems → interrupted.

**Later:** TASK-12 replaces `all` with the streaming `run` command. The buttons stay the same.

## 2. Data storage

Files are the source of truth. The SQLite database is an index that can be rebuilt from them (`pipeline rebuild-index`). Everything below survives a restart.

| Path | Content | In git? |
|---|---|---|
| `10K_Filings/` | All raw input, written by the user's downloader; read-only for this tool | no |
| `Markdown_10K/` | Conversion output; existing files never rewritten, new filings added | no |
| `outputs/segments/<company>/<file>.segments.jsonl` + `.record.json` | Chunks and the segmentation record | no |
| `outputs/classify/<model_tag>/<company>/<file>.*` | labels, requests, kept, summary, record | no |
| `outputs/clean/<model_tag>/<company>/<file>.clean.md`, `.clean.map.json`, `.record.json` | Published output | no |
| `outputs/clean/<model_tag>/clean_index.csv`, `.jsonl`, `clean_summary.json` | Work list and savings for scheduled tasks | no |
| `outputs/selection/` | Sample list, label xlsx files, evaluation reports | no (label files backed up by the user) |
| `runs/pipeline.sqlite`, `runs/logs/`, `runs/tmp/` | Status DB (WAL), logs, temp | no |
| `tools/llama.cpp/`, `models/` | Server binaries and GGUF weights | no |
| `config.toml` | Paths, chunk size, models, thresholds, compaction rules, prompt version, VRAM budget, disk threshold | yes |
| `models.toml` | Model registry | yes |
| `prompts/` | Definitions, prompt, JSON schema | yes |

**Record file contents** (`.record.json`):
- `stage`, `stage_version` (an explicit string, not a file hash);
- input SHA-256s, `settings_sha256`, status, output SHA-256s, timings, error.

**Skip-or-rerun rule:** a filing is skipped only if its record matches the current inputs, version and settings, and its output hashes still verify. Each stage's inputs include the previous stage's output hash, so a change reruns exactly the affected stage and the stages after it.

**Database tables**
- `filings` (registry, §1.4)
- `stage_status` (filing_id, stage, model_tag, status, started, finished, tokens_in, tokens_kept, error)
- `runs` (run_id, command, args, started, finished, exit)
- `progress` (run_id, filing_id, chunks_done, chunks_total, updated_at)

## 3. Main data flow

```text
10K_Filings/<company>/filings.csv + files   (user's downloader adds new companies over time)
   │  scan (incremental; manifest → DB registry)
   ▼
   │  convert (convertmd.py; existing results only indexed)
   ▼
Markdown_10K/<company>/<file>.md (+ .audit.json)
   │  segment (blocks + raw-HTML/legacy heading hints → sections → chunks; lossless check)
   ▼
outputs/segments/…segments.jsonl
   │  filter, one filing at a time:
   │    tier 1 scores every chunk (scorer server)
   │    tier 2 labels the undecided chunks (instruction-model server); keep rules
   ▼
outputs/classify/<model>/…labels.jsonl, .kept.md
   │  compact (deterministic rules on kept text; validated; fallback to uncompacted)
   ▼
outputs/clean/<model>/…clean.md + .clean.map.json, clean_index.csv   ──► Codex / Claude scheduled tasks
   │
   └─ every stage writes status → runs/pipeline.sqlite ← monitor (Flask; reads the DB, can start/stop a run)

Model selection (once): select-sample (dev + external) → export-labels → user labels in Excel
                        → import-labels → evaluate tiers and thresholds → user chooses → config.toml
```

## 4. Dependencies and runtime requirements

**Python packages.** Python 3.12, installed with uv into `.venv`; versions pinned in requirements.txt.

| Package | Version | Use |
|---|---|---|
| lxml | 6.1.3 | existing; also compaction |
| mistune | 3.3.4 | existing |
| tiktoken | 0.14.0 | existing |
| pytest | 9.1.1 | existing |
| Flask | 3.1.3 | monitor (new) |
| openpyxl | 3.1.5 | label spreadsheets (new) |

The HTTP client is the standard library `urllib.request`, and settings are read with `tomllib`. No other packages are needed.

**External tools** (installed only with the user's approval; no Docker, no WSL):
- llama.cpp Windows CUDA release zip (pinned build), unpacked into `tools/llama.cpp/`.
- GGUF model files from Hugging Face via `pipeline models fetch`, into `models/<name>/`, with SHA-256 recorded.

## 5. Testing approach

Tests use pytest. They need no GPU, no model and no network: `FakeClient` (tier 2) and a fake scorer (tier 1) return scripted results. Real-model tests carry `@pytest.mark.model` and are skipped by default. Real-corpus tests skip when `10K_Filings/` is missing.

| SPEC acceptance item | Test |
|---|---|
| Raw filing → clean file, map, index row in one command | `test_e2e.py` (fixtures, fake models) |
| Incremental scan registers exactly new filings; missing file fails alone; changed source reruns | `test_scan.py` |
| Rerun skips; a change reruns only affected steps | `test_records.py`, `test_e2e.py` |
| Killed mid-filing → redone, no duplicates | `test_classify.py::test_crash_mid_filing` *(exists)*, plus the same for compact/publish |
| Inputs and `Markdown_10K` unchanged | `test_e2e.py` hashes; `pipeline verify` |
| 10-KSB / 10-KT / amendments convert; existing outputs reused; amendment link | `test_conversion_forms.py`, `test_scan.py` |
| Chunks join to Markdown exactly | `test_blocks.py` *(exists)*, `test_segment_*.py`, real-corpus sample |
| Tables stay with caption/units/footnotes | `test_segment_tables.py` |
| TOC and prose mentions are not boundaries | `test_segment_items.py` + user-checked lists in `tests/expected/` |
| Every chunk scored or tier-1 failure recorded; one final label; failures kept | `test_two_tier.py`, `test_classify.py` *(exists)* |
| One filing per request | `test_classify.py` *(exists)*, `test_two_tier.py` |
| Cross-referenced notes kept | `test_crossref.py` |
| Compaction preserves text/numbers/cells; fallback on failure; no gain → original | `test_compact.py` (one test per rule, negative cases for fences, `<pre>`, multi-paragraph cells, spans) and real-sample check |
| Clean header, map traces back, index rows and nothing-kept marking | `test_publish.py` |
| Weighted token savings per step | `test_reports.py` |
| Model selection report | `test_selection.py` |
| Chosen models meet recall | manual `pipeline evaluate` (GPU), recorded in TASKS.md |
| Streaming flow and disk guard | `test_flow.py` (patched free-space function) |
| Monitor pages | `test_monitor.py` (Flask `test_client()`) |
| Existing converter behavior | `tests/test_conversion.py`, kept passing |

## 6. Folder structure

```text
intangible_analysis/
  AGENTS.md  SPEC.md  IMPLEMENTATION_PLAN.md  TASKS.md  README.md
  requirements.txt  pytest.ini  config.toml  models.toml  .gitignore  .gitattributes  .env.example
  convertmd.py  verify_outputs.py  smoke_conversion.py      (convertmd: forms change only, §1.5)
  pipeline/
    __init__.py  __main__.py  cli.py  config.py  corpus.py  flow.py  disk.py  publish.py
    state.py  records.py  tokens.py  convert_stage.py  reports.py
    segment/   blocks.py  html_hints.py  legacy_hints.py  structure.py  chunks.py  stage.py
    classify/  client.py  score.py  prompt.py  rules.py  crossref.py  stage.py
    compact/   rules.py  validate.py  stage.py
    models/    registry.py  fetch.py  server.py  vram.py
    selection/ sample.py  labels.py  evaluate.py
  prompts/     definitions.md  classify_v1.md  schema.json
  monitor/     app.py  queries.py  runner.py  templates/  static/monitor.js
  tests/       test_conversion.py (existing)  test_*.py  fixtures/  expected/
  outputs/  runs/  tools/  models/                          (git-ignored)
```

## 7. Install and run

```powershell
# install / update Python packages
uv pip install --python .venv -r requirements.txt

# tests
& '.\.venv\Scripts\python.exe' -m pytest -q

# after the downloader adds filings to 10K_Filings/
& '.\.venv\Scripts\python.exe' -m pipeline scan                    # register only new filings
& '.\.venv\Scripts\python.exe' -m pipeline run                     # convert → segment → filter → compact → publish
& '.\.venv\Scripts\python.exe' -m pipeline status

# single steps / subsets
& '.\.venv\Scripts\python.exe' -m pipeline segment --limit 20
& '.\.venv\Scripts\python.exe' -m pipeline all --company AAPL --fake-model
& '.\.venv\Scripts\python.exe' -m pipeline compact --profile --limit 50

# local models (each step asks before downloading)
& '.\.venv\Scripts\python.exe' -m pipeline models list
& '.\.venv\Scripts\python.exe' -m pipeline models fetch qwen3.5-9b-q5
& '.\.venv\Scripts\python.exe' -m pipeline models check qwen3.5-9b-q5 qwen3-reranker-0.6b
& '.\.venv\Scripts\python.exe' -m pipeline models serve qwen3.5-9b-q5
& '.\.venv\Scripts\python.exe' -m pipeline evaluate

# monitor
& '.\.venv\Scripts\python.exe' -m monitor        # http://127.0.0.1:5000 (local only)
```

## 8. Security measures

The tool is local and single-user, so the measures are small:
- **Network binding:** Flask and llama-server listen on `127.0.0.1` only. Flask debug mode is used only in local development.
- **Monitor reads the DB read-only:** it opens SQLite with `mode=ro`. Filing pages look up paths in the DB by filing ID and never build file paths from URL text.
- **Run control:**
  - The only actions are start and stop. They are POST requests that carry a per-process random token (CSRF protection), and requests are accepted only from 127.0.0.1.
  - Filter values are passed to the subprocess as an argument list, never through a shell; the year and the limit must be integers.
  - The monitor writes only `runs/web_run.json`, `runs/stop.request` and `runs/logs/`.
- **Filing text is shown escaped:** Jinja autoescape, rendered in `<pre>`.
- **No filing content leaves the machine.** The only network use is downloading the model runtime and weights, with the user's approval. The scheduled tasks read the clean files; that happens outside this tool.
- **Untrusted input:** input files are parsed, never executed; temp files go only under `runs/tmp/`.
- **Integrity:** binaries and weights are hashed and checked against published SHA-256 values when available.
- **Secrets:** an optional `HF_TOKEN` comes from the environment and is documented in `.env.example`.

## 9. Key tradeoffs

- **Minimal change to `convertmd.py`:** only the accepted forms change, through its compatibility list, so no reconversion is needed. Heading hints for segmentation come from re-reading the raw HTML.
- **Recall over size:** section expansion and UNCERTAIN-on-failure keep more text. The model only removes what is clearly irrelevant; compaction never removes content.
- **Two tiers instead of generative-only:** needed at the target scale. The risk that tier 1 drops something relevant is controlled by:
  - a conservative `t_drop`;
  - recall measured on external filings;
  - the "tier 1 only orders" fallback.
- **Compaction after filtering:** compact only what survives the filter. Validation is per filing, with fallback to the verbatim kept text, so a bad rule costs savings, not content.
- **Provenance in a sidecar map:** keeps the clean files short for the scheduled tasks, while every piece stays traceable.
- **llama.cpp natively on Windows instead of vLLM in WSL2:** simpler setup, lower peak throughput.
- **Single SQLite registry:** simple and fast at 150k filings with indexes; rebuildable from files.

## Sources consulted
- [Qwen3.5-9B model card](https://huggingface.co/Qwen/Qwen3.5-9B)
- [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/latest)
- [vLLM GPU installation (Windows not supported natively)](https://docs.vllm.ai/en/v0.18.1/getting_started/installation/gpu/)
- [Gemma releases](https://ai.google.dev/gemma/docs/releases)
- [Flask on PyPI](https://pypi.org/project/Flask/), [openpyxl on PyPI](https://pypi.org/project/openpyxl/)
- Qwen3-Reranker GGUF builds on Hugging Face (several are community conversions; provenance checked at selection time)
