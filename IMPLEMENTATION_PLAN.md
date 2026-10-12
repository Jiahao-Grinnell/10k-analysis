# IMPLEMENTATION_PLAN.md

How the pipeline in SPEC.md is built. Standing rules are in AGENTS.md.

## 0. Starting point (inspected 2026-10-12)

**Code that already exists**
- `convertmd.py` (v1.1.0) converts raw filings to `Markdown_10K/`. It writes a `.audit.json` per filing plus `conversion_report.csv` and `conversion_summary.json`.
  - Its `--resume` option reuses an output only when that output's stored `CODE_HASH` (a SHA-256 of `convertmd.py`) matches the current file.
- `verify_outputs.py` re-checks the converted corpus; `tests/test_conversion.py` holds 24 tests for the converter. All 24 pass.
- The venv was created with **uv** (`.venv`, Python 3.12.14) and contains no pip.
- The old pytest temp folder `.cache/pytest-tmp` has broken permissions (access denied), and `pytest.ini` points there. Until that folder is removed by the user, run the tests with `-o addopts="" --basetemp=<another folder>`. TASK-01 moves the basetemp to a fresh folder.

**What the Markdown keeps and loses** (this matters for segmentation)
- Only real `<h1>`–`<h6>` become Markdown headings.
- Bold, font size and centering are removed, so styled HTML headings become plain paragraphs.
- Internal anchor links (`href="#..."`) and element ids are dropped. Table-of-contents link targets therefore exist only in the raw HTML.
- Legacy fixed-width filings are written entirely as fenced text blocks, so their headings sit inside fences.

**Machine**
- NVIDIA RTX 5000 Ada Laptop, 16,376 MiB, driver 581.95 (CUDA 13.0); 64 GB RAM; Windows 11.
- git 2.52 is installed, with global user configured.

## 1. Structure and main components

The design has three parts:
- a Python package `pipeline/` that runs every stage from the command line;
- a separate local model server;
- a read-only Flask monitor that reads the pipeline's status database.

| Component | Responsibility |
|---|---|
| `convertmd.py` (unchanged) | Raw → Markdown. Not moved or edited in v1, so `CODE_HASH` and the existing outputs stay valid. |
| `pipeline/corpus.py` | Builds the filing list from `10K_Filings/*/filings.csv`, reusing `convertmd.discover()`. Assigns each filing an ID (`<company_dir>/<file stem>`) and attaches company, year, raw path and Markdown path. |
| `pipeline/convert_stage.py` | Indexes existing `Markdown_10K/` results into the status database from their `.audit.json`. Calls `convertmd.conversion_job()` only for filings with no Markdown yet. Never rewrites an existing Markdown file. |
| `pipeline/segment/` | Markdown (+ raw HTML hints) → sections and chunks. Details in §1.1. |
| `pipeline/classify/` | Sends each chunk of one filing to the local model, applies the keep rules, and writes the per-filing outputs. Details in §1.2. |
| `pipeline/selection/` | Builds the labeled sample, exports/imports label spreadsheets, and evaluates candidate models. Details in §1.3. |
| `pipeline/state.py` | SQLite status database: runs, per-filing stage status, live progress. |
| `pipeline/records.py` | Atomic file writes, per-filing record files, and the skip-or-rerun (resume) decision. |
| `pipeline/tokens.py` | o200k_base token counting, reusing the tiktoken cache in `.cache/tiktoken`. |
| `pipeline/cli.py` | `python -m pipeline <command>`. |
| `monitor/` | Flask app with server-rendered pages and one small JS file for auto-refresh. |
| llama.cpp `llama-server` (external) | Runs the local model on the GPU. Bound to `127.0.0.1`, OpenAI-compatible, with JSON-schema-constrained output. |

### 1.1 Segmentation

Segmentation has two layers: logical **sections** (what gets kept or dropped) and reading **chunks** (what the model reads).

1. **Blocks.** Parse the Markdown into an ordered list of blocks, each with exact character start and end offsets: heading, paragraph, pipe table, HTML table, HTML list, fenced block. Every character, including blank lines, belongs to exactly one block.
2. **Heading candidates** come from structure, by source format:
   - **HTML / iXBRL:** re-parse the raw HTML with `HtmlConverter.prepare()` (imported, not modified). Record block elements whose visible text is short and standalone, together with:
     - formatting features: bold, larger font, centered, all-caps, page break before;
     - whether the element is the target of an internal `#anchor` link.
     Then align these candidates to Markdown blocks in document order by their whitespace-compacted visible text. This is reliable because conversion already verified that the visible text is equal and in order.
   - **Legacy fixed-width:** line-level features inside fenced blocks: a short line, all-caps or title case, surrounded by blank lines, centered by leading spaces, or right after a page break. The fence is split at these lines.
   - **Real Markdown headings (`#`)** are used directly.
3. **Table of contents and prose.** A candidate is treated as a table-of-contents entry, not a boundary, when:
   - it sits in a table or block run whose rows are Item references followed by page numbers; or
   - its Item sequence repeats later in the document as real headings.

   Item references inside a sentence (a candidate that is not a standalone block) never become boundaries. For HTML, the targets of TOC anchor links are the preferred Item boundaries.
4. **Hierarchy.** Assign Part → Item → subsection → note using numbering patterns:
   - `PART I–IV`, `Item 1`/`1A`/`7A`… (Items must be monotonic within a Part);
   - `Note N` / `N.` headings inside Item 8 or EX-13;
   - remaining candidates become subsections.

   These rules recognize document structure only; they never decide relevance. Each EX-13 document in an SGML filing gets its own top-level node.
5. **Table units.** A table block is grouped with:
   - up to 2 short blocks right before it (caption, unit line such as "in millions");
   - short footnote blocks right after it (blocks starting with `(1)`, `*`, `(a)` and similar).

   A unit is never split, unless it exceeds the chunk limit by itself (see step 6).
6. **Chunks.** Sections are split into chunks of whole blocks/units with at most `chunk_max_tokens` tokens (default 2,000, set in `config.toml`).
   - An oversized table unit is split by rows. Its header rows are repeated in the model input of each piece, but the stored character ranges stay verbatim.
   - An oversized prose block is split at paragraph or line breaks.
7. **Fallback.** If no Item structure is found, the filing is still split into chunks under a single `UNSTRUCTURED` section and marked `segmentation_quality=LOW`. It is still fully classified.
8. **Check.** Joining all chunk ranges must reproduce the Markdown file byte for byte. If not, the filing fails segmentation and is not classified.

Each chunk record contains: `filing_id`, `chunk_id`, `section_id`, `heading_path`, `item`, `start`, `end`, `tokens`, `kind` (prose/table/mixed), `quality`.

### 1.2 Classification

**Order**
- Filings are processed strictly one after another. Within one filing, up to `parallel_requests` chunk requests (default 4) run at the same time against the server's parallel slots. Every request contains text from that filing only.
- The filing's outputs are written before the next filing starts.

**Prompt**
- The prompt is versioned: `prompts/classify_v1.md` plus `prompts/definitions.md`, where the definitions are copied verbatim from the .docx table.
- The prompt asks for label, categories and a one-sentence reason, and states the rule "prefer UNCERTAIN over DROP when unsure".

**Request**
- `/v1/chat/completions` with:
  - `response_format = {"type": "json_schema", "schema": ...}`, using `prompts/schema.json`;
  - `temperature 0`;
  - `chat_template_kwargs = {"enable_thinking": false}` for models with a thinking mode;
  - a per-request timeout.
- Input: definitions, filing name and year, heading path, the end of the previous chunk (≤ 300 tokens) as context, and the chunk text. Fenced fragments are re-closed in the model input only.

**Keep rules**
- Any failure becomes UNCERTAIN with an error code: timeout, HTTP error, invalid JSON, or schema mismatch after 1 retry.
- Section expansion: if any chunk of a section is KEEP or UNCERTAIN, all chunks of that section are kept, with `kept_by=section`.
- Cross-references: in kept chunks, find structural references (`Note N`, `Item N`, `Exhibit 13`) and resolve them to sections of the same filing. The targets are kept with `kept_by=reference:<chunk_id>`. Unresolved references are listed in the summary.

**Outputs per filing**, written atomically in this order:
1. `.labels.jsonl` — one row per chunk with `model_label`, `final_label`, `kept_by`, `categories`, `reason`, `error` and the raw response.
2. `.requests.jsonl` — `request_id`, `filing_id`, `chunk_ids`, prompt SHA-256, timings.
3. `.kept.md` — verbatim chunk text in order, each preceded by a marker such as `<!-- chunk c0042 | Item 7 > Results of Operations | chars 120344-128930 | KEEP -->`.
4. `.summary.json`.
5. The record file. It is written last; a filing counts as done only when its record exists.

### 1.3 Model selection

**No Docker and no WSL.** Models are downloaded from Hugging Face as plain files into `models/` and run on this laptop by a native Windows server.

**Runtime: llama.cpp `llama-server`, native Windows CUDA build.** Pin the build tag (latest seen: b11429, 2026-10-05) and record it in `config.toml`.
- It runs natively on Windows; vLLM officially does not and needs WSL2.
- It supports JSON-schema output, parallel slots with continuous batching, prompt-prefix reuse (`cache_prompt`), and `/health` and `/slots` endpoints.
- Ollama is the fallback (it is built on llama.cpp), but it offers less control over slots and caching.

**Candidates** (verify current model cards and GGUF files at selection time):

| Candidate | Why |
|---|---|
| Qwen3.5-4B | Fast; Apache-2.0; thinking mode can be turned off. |
| Qwen3.5-9B | Stronger; a Q4/Q5 GGUF fits in 16 GB with room for the KV cache. |
| Gemma 4 E4B | Different model family, small and fast; Apache-2.0. |
| Gemma 4 12B | Stronger second family; check that Q4 plus context fits. |

Qwen3.5 uses a hybrid attention design, so llama.cpp's prompt-prefix reuse may be limited. Speed is measured, not assumed.

**Model registry: adding other local models for testing.** The four candidates above are only a starting list. The design makes adding any other model a config change, not a code change.
- **`models.toml`** has one entry per model:
  - `name` (used as `model_tag` in output paths);
  - Hugging Face `repo` and `file`, plus `revision`;
  - expected `sha256`;
  - `quant`;
  - `ctx_per_slot`, `parallel`;
  - `chat_template_kwargs` (for example, turning thinking off);
  - optional extra server flags.
- **`pipeline models fetch <name>`** downloads the file from Hugging Face into `models/<name>/`:
  - It uses the standard library HTTP client, can resume an interrupted download, and verifies the SHA-256. `HF_TOKEN` is used only if the repo requires login.
  - It asks for confirmation, shows the file size, and checks free disk space first (566 GB free on C: today).
- **`pipeline models serve <name>`** starts `llama-server` with that entry's settings on `127.0.0.1` and waits for `/health`. Only one model is loaded at a time.
- **`pipeline models list`** shows downloaded models, their sizes, and which have evaluation results.
- **Outputs are kept per model.** Results go under `outputs/classify/<model_tag>/` and `outputs/selection/eval/<model_tag>/`, so testing a new model never overwrites another model's results. The evaluation report compares all evaluated models side by side.
- **Any local server works.** The classifier only talks to an OpenAI-compatible endpoint (`base_url` in `config.toml`). A model that has no GGUF file could later be served by another local, non-Docker server (for example `transformers serve`, listed on the Qwen3.5 card). That would require installing PyTorch with CUDA, which needs the user's approval and is not part of v1.

**GPU memory budget (16 GB).**
- The GPU has 16,376 MiB in total. Windows and the display keep some of it, so the budget for model weights, KV cache and compute buffers is **≤ 14 GB**. This is set as `vram_budget_mib` in `config.toml`.
- Before serving, `pipeline models check <name>` estimates memory as GGUF file size + KV cache for `ctx_per_slot × parallel` + about 1 GB overhead. If the estimate is over budget, it refuses and suggests:
  - fewer parallel slots;
  - a smaller context;
  - a smaller quantization.

  After the model loads, it records the real peak from `nvidia-smi`.
- Approximate weight sizes:

  | Model | Approx. weights |
  |---|---|
  | 4B at Q8 | ~4–5 GB |
  | 9B at Q4–Q5 | ~6–7 GB |
  | 12B at Q4 | ~7–8 GB |

  All of these leave room for 4 slots × 8k context. Larger dense models (30B+) do not fit at useful quality.
- Mixture-of-experts models (such as Gemma 4 26B A4B) fit only by keeping some experts in system RAM with llama.cpp's CPU-offload option. That is slower, and it is allowed only as an explicit per-model setting.

**Labeled sample**
- `pipeline select-sample` picks 12 filings stratified by raw format × era and spread across companies: 8 for prompt tuning ("dev") and 4 held out ("test").
- `pipeline export-labels` writes one `.xlsx` per filing (openpyxl). Each row is one chunk, with ID, heading path, full chunk text (≤ about 12k characters, under Excel's 32,767-character cell limit), and empty `relevant` / `category` / `note` columns.
- `pipeline import-labels` reads the files back. Blank rows mean "not labeled" and are excluded from the metrics.

**Evaluation** (`pipeline evaluate --model <tag>`)
- Runs a candidate on the sample and reports:
  - chunk-level recall and section-level recall (after keep rules);
  - share of tokens dropped;
  - prefill and generation tokens/s;
  - peak GPU memory (from `nvidia-smi`);
  - error rate;
  - projected full-corpus hours.
- The user picks the model. The choice is written to `config.toml` and §1.3 of this file, with links, model file SHA-256, quantization and llama.cpp build.
- The v1 acceptance target is section-level recall ≥ 95% on the test filings.

**Rough cost**
- Full-corpus classification reads about 588M Markdown tokens plus the prompt overhead per chunk.
- At a measured prefill speed of 3,000–6,000 tokens/s, that is roughly 1–3 days of GPU time. Runs are resumable, so the work can be spread over several sessions.

## 2. Data storage

Files are the source of truth. The SQLite database is an index that can be rebuilt from them (`pipeline rebuild-index`). Everything below survives a restart.

| Path | Content | In git? |
|---|---|---|
| `10K_Filings/` | Raw input, read-only | no |
| `Markdown_10K/` | Existing conversion output, read-only, except that new files may be added for unconverted filings | no |
| `outputs/segments/<company>/<file>.segments.jsonl` + `.record.json` | Chunks and the segmentation record | no |
| `outputs/classify/<model_tag>/<company>/<file>.*` | labels, requests, kept, summary, record | no |
| `outputs/classify/<model_tag>/classification_report.csv`, `classification_summary.json` | Corpus report | no |
| `outputs/selection/` | Sample list, label xlsx files, evaluation reports | no (label files are backed up by the user) |
| `runs/pipeline.sqlite` | Status DB (WAL mode) | no |
| `runs/logs/` | Run logs | no |
| `tools/llama.cpp/`, `models/` | Server binaries and GGUF weights | no |
| `config.toml` | Paths, chunk size, parallelism, server URL, active model tag, prompt version, VRAM budget | yes |
| `models.toml` | Model registry: one entry per local model | yes |
| `prompts/` | Definitions, prompt, JSON schema | yes |

**Record file contents** (`.record.json`):
- `stage`, `stage_version` (an explicit string such as `segment-1`, not a file hash);
- input SHA-256s, `settings_sha256`, status, output SHA-256s, timings, error.
- For classification, the settings also include the prompt files' SHA-256, model file SHA-256, quantization and llama.cpp build.

**Skip-or-rerun rule:** a filing is skipped only if its record matches the current inputs, version and settings, and its output hashes still verify.

**Database tables**
- `filings` (id, company, ticker, year, form, raw_format, paths, conversion quality)
- `stage_status` (filing_id, stage, model_tag, status, started, finished, tokens_in, tokens_kept, error)
- `runs` (run_id, command, args, started, finished, exit)
- `progress` (run_id, filing_id, chunks_done, chunks_total, updated_at)

## 3. Main data flow

```text
10K_Filings/ (raw, read-only)
   │  convert (convertmd.py; existing results only indexed)
   ▼
Markdown_10K/<company>/<file>.md (+ .audit.json)
   │  segment (Markdown blocks + raw-HTML/legacy heading hints → sections → chunks; lossless check)
   ▼
outputs/segments/…segments.jsonl
   │  classify (one filing at a time; llama-server on 127.0.0.1; keep rules)
   ▼
outputs/classify/<model>/…labels.jsonl, .kept.md, .summary.json
   │
   └─ every stage writes status → runs/pipeline.sqlite ← monitor (Flask, read-only)

Model selection (once): select-sample → export-labels → user labels in Excel → import-labels → evaluate per candidate → user chooses → config.toml
```

## 4. Dependencies and runtime requirements

**Python packages.** Python 3.12, installed with uv into `.venv`; versions pinned in requirements.txt.

| Package | Version | Use |
|---|---|---|
| lxml | 6.1.3 | existing |
| mistune | 3.3.4 | existing |
| tiktoken | 0.14.0 | existing |
| pytest | 9.1.1 | existing |
| Flask | 3.1.3 | monitor (new) |
| openpyxl | 3.1.5 | label spreadsheets (new) |

The HTTP client is the standard library `urllib.request`, and settings are read with `tomllib`. No other packages are needed.

**External tools** (installed only with the user's approval; no Docker, no WSL):
- llama.cpp Windows CUDA release zip (pinned build), unpacked into `tools/llama.cpp/`. The driver supports CUDA 13.0.
- GGUF model files downloaded from Hugging Face with `pipeline models fetch`, into `models/<name>/`. The source is the official model publisher's repo, or the quantization repo named on its model card. Each file's SHA-256 is recorded in `models.toml`.

## 5. Testing approach

Tests use pytest. They need no GPU and no model: `pipeline/classify/client.py` has a `FakeClient` that returns scripted labels and failures. Real-model tests carry `@pytest.mark.model` and are skipped unless `--run-model` is given. Real-corpus tests skip when `10K_Filings/` is missing.

| SPEC acceptance item | Test |
|---|---|
| One command, raw → labels + kept text | `test_e2e.py`: small HTML and legacy fixtures in a temp dir, run `convert`, `segment`, `classify` with FakeClient, assert outputs |
| Rerun skips; prompt change reruns only classification | `test_records.py`, `test_e2e.py::test_resume` |
| Killed mid-filing → redone, no duplicates | `test_classify.py::test_crash_mid_filing` (FakeClient raises after N chunks) |
| Raw and `Markdown_10K` unchanged | `test_e2e.py` hashes the fixture inputs before and after; corpus check in `pipeline verify` |
| Chunks join to Markdown exactly | `test_blocks.py`, `test_segment_*.py` (property check on every fixture), plus a real-corpus sample test |
| Tables stay with caption/units/footnotes; split tables repeat header | `test_segment_tables.py` |
| TOC and prose mentions are not boundaries | `test_segment_items.py`, using fixtures with a TOC table, an anchor-linked TOC, prose "see Item 7", and legacy TOC; plus per-format real filings with user-checked boundary lists in `tests/expected/` |
| Every chunk has exactly one label; failures → UNCERTAIN | `test_classify.py` |
| One filing ID per request | `test_classify.py::test_request_isolation` |
| Kept text = verbatim slices, in order, all KEEP/UNCERTAIN | `test_classify.py::test_kept_text_verbatim` |
| Cross-referenced notes kept | `test_crossref.py` |
| Model selection report fields | `test_selection.py` (FakeClient) |
| Chosen model meets recall target | Manual run of `pipeline evaluate`; the result is recorded in TASKS.md (needs GPU) |
| Corpus report and "nothing kept in Items 7/8" list | `test_reports.py` |
| Monitor pages show progress, idle counts, filing detail and text | `test_monitor.py` with Flask `test_client()` against a temp DB, plus manual check during a real run |
| Existing converter behavior | existing `tests/test_conversion.py`, kept unchanged |

## 6. Folder structure

```text
intangible_analysis/
  AGENTS.md  SPEC.md  IMPLEMENTATION_PLAN.md  TASKS.md  README.md
  requirements.txt  pytest.ini  config.toml  models.toml  .gitignore  .gitattributes  .env.example
  convertmd.py  verify_outputs.py  smoke_conversion.py      (unchanged)
  pipeline/
    __init__.py  __main__.py  cli.py  config.py  corpus.py
    state.py  records.py  tokens.py  convert_stage.py  reports.py
    segment/   blocks.py  html_hints.py  legacy_hints.py  structure.py  chunks.py  stage.py
    classify/  client.py  prompt.py  rules.py  crossref.py  stage.py
    models/    registry.py  fetch.py  server.py  vram.py
    selection/ sample.py  labels.py  evaluate.py
  prompts/     definitions.md  classify_v1.md  schema.json
  monitor/     app.py  templates/  static/monitor.js
  tests/       test_conversion.py (existing)  test_*.py  fixtures/  expected/
  outputs/  runs/  tools/  models/                          (git-ignored)
```

## 7. Install and run

```powershell
# install / update Python packages
uv pip install --python .venv -r requirements.txt

# tests
& '.\.venv\Scripts\python.exe' -m pytest -q

# pipeline (examples)
& '.\.venv\Scripts\python.exe' -m pipeline convert                 # index existing Markdown, convert missing
& '.\.venv\Scripts\python.exe' -m pipeline segment --limit 20
& '.\.venv\Scripts\python.exe' -m pipeline classify --filing "AAPL_Apple_Inc_CIK0000320193/2025_2025-10-31_0000320193-25-000079"
& '.\.venv\Scripts\python.exe' -m pipeline all --company AAPL --resume
& '.\.venv\Scripts\python.exe' -m pipeline status

# local models (each step asks before downloading)
& '.\.venv\Scripts\python.exe' -m pipeline models list
& '.\.venv\Scripts\python.exe' -m pipeline models fetch qwen3.5-9b-q5      # download from Hugging Face into models/
& '.\.venv\Scripts\python.exe' -m pipeline models check qwen3.5-9b-q5      # estimate GPU memory vs 14 GB budget
& '.\.venv\Scripts\python.exe' -m pipeline models serve qwen3.5-9b-q5      # start llama-server on 127.0.0.1:8080
& '.\.venv\Scripts\python.exe' -m pipeline evaluate --model qwen3.5-9b-q5

# monitor
& '.\.venv\Scripts\python.exe' -m flask --app monitor.app run --host 127.0.0.1 --port 5000
```

## 8. Security measures

The tool is local and single-user, so the measures are small:
- **Network binding:** Flask and llama-server listen on `127.0.0.1` only. Flask debug mode is used only in local development.
- **Monitor is read-only:** it opens SQLite with `mode=ro`. Filing pages look up paths in the database by filing ID and never build file paths from URL text, which prevents path traversal.
- **Filing text is shown escaped:** Jinja autoescape, rendered in `<pre>`. Filings contain raw HTML, which is never rendered as HTML.
- **No filing content leaves the machine.** The only network use is downloading tools and weights, with the user's approval.
- **Integrity:** downloaded binaries and weights are checked against their published SHA-256 when one is available, and recorded.
- **Secrets:** an optional `HF_TOKEN` for model downloads comes from the environment and is documented in `.env.example`. No other secrets exist.

## 9. Key tradeoffs

- **Not changing `convertmd.py`:** this avoids reconverting 2,894 filings. The cost is that segmentation must re-read the raw HTML for heading hints and align them by text order.
- **Section expansion and UNCERTAIN-on-failure** favor recall over size. Kept text may be large for some filings, and that is accepted in v1.
- **Generative classification of every chunk** is simple and matches SPEC, but it is slow. If the measured corpus time is unacceptable, a faster pre-scoring tier is a later iteration, not part of v1.
- **llama.cpp natively on Windows instead of vLLM in WSL2:** setup is simpler, peak throughput is lower, and per-filing requests stay sequential across filings.

## Sources consulted
- [Qwen3.5-9B model card](https://huggingface.co/Qwen/Qwen3.5-9B)
- [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/latest)
- [vLLM GPU installation (Windows not supported natively)](https://docs.vllm.ai/en/v0.18.1/getting_started/installation/gpu/)
- [Gemma releases](https://ai.google.dev/gemma/docs/releases)
- [Flask on PyPI](https://pypi.org/project/Flask/), [openpyxl on PyPI](https://pypi.org/project/openpyxl/)
