# SPEC.md — 10-K Cleanup and Pre-filtering Pipeline (version 1)

## 0. Purpose and scope (revised 2026-10-12)

**What this tool is.** It is the **preparation step before final extraction**. It takes 10-K filings that the user has already downloaded with separate code, then cleans, organizes and shrinks them as much as safely possible. The final extraction of intangible-investment amounts is done later, filing by filing, by Codex or Claude scheduled tasks reading this tool's output.

**What this tool is not.** It does not download filings. It does not produce final answers or extracted amounts.

**The local models' job** is narrow: remove sections that are **clearly** irrelevant to intangible investment. Anything relevant or uncertain is kept. Recall matters far more than size.

**Scale.** The finished tool must handle the filings of about 5,000 companies:
- years: all of 1993 to the present;
- forms: 10-K, 10-K405, 10-KSB (including 10-KSB405), 10-KT, and their amendments.

All filings live in `10K_Filings/`, in the downloader's layout. The user keeps adding new companies and filings there.

Today the folder holds 100 companies, 2,896 filings, 588M Markdown tokens. These first 100 companies serve as the **development sample**. Companies added later supply the external validation filings, and eventually the full target.

Estimated target size, extrapolated from the sample:

| | Estimate |
|---|---|
| Filings | 50,000–150,000 |
| Markdown tokens | 10–30 billion |

**Goal:** minimize the tokens that the scheduled tasks must read, without losing relevant content.

## 1. Core functionality and main workflow

Input: `10K_Filings/`, which has one folder per company with a `filings.csv` manifest and the filing files, written by the user's downloader. When new filings are added, a scan registers only the new ones. Filings already processed are not redone.

For each filing, one at a time:

1. **Convert.** Raw filing (HTML, inline XBRL, SGML complete submission, or legacy fixed-width text) → one Markdown file. The existing `convertmd.py` does this; it is extended to accept the additional forms.
2. **Segment.** Markdown → an ordered list of sections that follows the filing's own structure: Part → Item → subsection → financial note or table. Each section records where it came from.
3. **Filter in two tiers.** The goal is to remove clearly irrelevant sections.
   - **Tier 1:** a fast local scoring model reads **every** chunk and gives it a relevance score.
   - **Tier 2:** a local instruction model labels KEEP, DROP or UNCERTAIN every chunk that tier 1 does not clearly settle.
   - Every chunk is inspected by at least one local model. Failures and uncertain cases are kept.
4. **Compact.** Deterministic, validated compression of the **kept** text only:
   - remove presentational markup and wasted whitespace;
   - use compact table forms.

   Validation checks that text, numbers, signs and table cells are unchanged. If a check fails, the filing falls back to the uncompacted kept text.
5. **Publish.** One clean Markdown file per filing for the scheduled tasks, with a short metadata header. Also a corpus index listing every filing, its status, its clean file and its token counts. Provenance (where each kept piece came from) goes in a sidecar file, so it costs the scheduled tasks no tokens unless they choose to read it.

**Choosing models** (done once, and again when needed): candidate models and thresholds are measured on a labeled sample, which includes filings from companies outside the development set. The user picks.

**Monitor:** a local web page shows what has been processed, what is running, what failed, token savings, and free disk space.

Intangible-investment categories (from `Intangible_investment.docx`):

| Category | What counts |
|---|---|
| Research & Development | Creating new products, technologies and production processes; improving technology, design and production processes |
| Advertising / marketing-related intangibles | Advertising campaigns, market research, brand development, trademarks that build customer recognition and demand |
| Organizational capital (SG&A) | Management systems, business-process improvement, how the firm organizes production and management, data processing, employee training, firm-specific skills |
| Intellectual property | Patents, copyrights, trademarks, licenses; creating or controlling valuable knowledge and rights |

The definitions document says these categories are expected in Items 1, 6, 7, 8, 14 and 15, including tables and the notes to the financial statements. Version 1 uses that list only for reporting and checks. It is **never** used to choose which sections a model reads.

## 2. Essential features

### F1. Input and conversion
- **Input:** `10K_Filings/<company>/filings.csv` plus the filing files.
  - A manifest row whose file is missing becomes one failed filing; the other filings still run.
- **Registry:**
  - `scan` reads the manifests and registers new filings in the status database. Already-registered filings are not re-read.
  - Every other command selects from the database instead of rescanning the disk.
  - A filing whose raw file changed (different SHA-256 from the manifest or the previous scan) is flagged, and its later steps are redone.
- **Markdown:** new filings are converted into `Markdown_10K/` alongside the existing files. Existing Markdown is never rewritten.
- **Converter:** reuse `convertmd.py`, extended to accept 10-KSB, 10-KSB405, 10-KT and their amendments.
  - The change goes through the converter's existing compatibility mechanism, so all existing `Markdown_10K/` outputs stay valid without reconversion.
  - The change is shown to the user before it is made.
- **Amendments:** marked from the form type and, when the manifest allows it, linked to the filing they amend (same company and period). That way the scheduled tasks do not count the same number twice.
- **Problem filings:** source-incomplete filings are carried through as "source incomplete". Filings whose format cannot be converted are listed with the reason.

### F2. Segmentation

**How boundaries are found**
- Boundaries come from document structure, not keywords. A table-of-contents entry or a sentence that mentions "Item 7" must not start a new section.
- 10-KSB's own item numbering is recognized, not forced into the 10-K numbering.
- Legacy fixed-width text uses its own structural rules (line layout, capitalized headings).

**What each section looks like**
- A table stays in one section together with its caption, unit line (for example "in millions"), headers and footnotes.
- Long sections are split into reading chunks of a configured maximum size. Each chunk carries its heading path and position. When a long table is split, its header is repeated in the model input.
- Each chunk records: filing ID, heading path, character start/end in the Markdown, token count, type.

**Lossless check:** concatenating all chunks in order reproduces the filing's Markdown exactly, checked for every filing.

### F3. Labeled sample and model selection

**Sample**
- About 12 development filings, plus an external validation set from companies outside the first 100.
- The user adds the external filings to `10K_Filings/` with their downloader.
- Together they cover each form type, raw format and era (≤2000, 2001–2008, 2009–2018, 2019+), and a range of company sizes and industries.

**Labeling:** sections are exported to spreadsheets, labeled by the user (relevant / not relevant, plus an optional category), and read back.

**Evaluation**
- Candidates are measured alone and in tier-1 + tier-2 combinations, with candidate thresholds. The report gives:
  - recall of relevant sections, separately for development and external filings;
  - share of tokens removed;
  - speed;
  - GPU memory use;
  - error rate;
  - projected time for the full target corpus.
- The chosen models, versions/quantizations, prompt, thresholds and settings are recorded in IMPLEMENTATION_PLAN.md and in every result.

### F4. Two-tier filtering

**Tier 1: fast scoring**
- Every chunk of every filing is scored against the category definitions. No chunk is skipped by keyword or Item.
- Two thresholds, both tuned on the labeled sample, decide what tier 1 settles alone:
  - "clearly relevant" → KEEP;
  - "clearly irrelevant" → DROP.

  The DROP threshold must be conservative enough that the recall target is still met.
- Everything between the thresholds goes to tier 2.

**Tier 2: instruction model**
- Each request contains content from exactly one filing: the chunk, its heading path, short surrounding context, and the category definitions.
- The model returns KEEP / DROP / UNCERTAIN, optional categories, and a one-sentence reason.

**When labels are overridden**
- Errors, timeouts, out-of-memory failures and unreadable answers count as UNCERTAIN, which means kept. A tier-1 failure sends the chunk to tier 2.
- If any chunk of a section is KEEP or UNCERTAIN, the whole enclosing section is kept.
- If a kept section explicitly refers to a note, table or exhibit in the same filing (for example, "see Note 7"), that target is kept too.

**Order:** filings are processed one at a time; one filing's outputs are written before the next filing starts.

### F5. Deterministic compaction (kept text only)

**Rules** (each rule is measured first; only rules that save tokens are enabled):
- unwrap purely presentational `<p>`/`<div>`/`<span>` inside table cells;
- remove empty wrappers;
- collapse whitespace outside fixed-width and preformatted text;
- convert HTML tables to pipe tables when the grid stays exact;
- drop table columns that are empty in every row;
- other conservative table simplifications only if they pass the same checks.

**Never changed:**
- the order of text;
- numbers, signs and parentheses;
- cell contents and row/column structure;
- merged-cell spans;
- footnotes and headings;
- spacing inside legacy fixed-width text.

**Validation:** per filing, the compacted text is checked against the uncompacted kept text: visible text sequence, numeric tokens with signs, and table cell grids. If anything fails, the uncompacted kept text is used instead.

**Records:** the rules applied, the checks, and tokens before and after.

### F6. Published output for scheduled tasks

**Per filing:** `<id>.clean.md`.
- **Header:** company name, CIK, ticker if known, form, fiscal period / report date, filing date, accession, amendment flag and link, source file, and conversion quality flag.
- **Body:** the kept sections in document order. Each section starts with its heading path once, in plain Markdown.
- The body does not repeat the definitions or the instructions; those belong in the scheduled task's prompt.

**Per filing sidecar:** `<id>.clean.map.json`. It maps each part of the clean file to its chunk IDs, its character ranges in the original Markdown, labels, reasons, and the deciding tier.

**Corpus index:** `clean_index.csv` / `.jsonl`. One row per filing with:
- the metadata above;
- status (ready / source incomplete / failed);
- paths;
- token counts at each step: raw, Markdown, kept, clean.

A scheduled task can work down this list one filing at a time and skip filings it has already done.

**Never zero by omission:** a filing whose clean file has no kept sections says so explicitly in its header ("no relevant sections kept"). The index marks it, so missing text is never mistaken for a zero amount.

### F7. Command-line runner
- Run one stage or the whole flow, on everything, a subset (`--limit`, company, year, form) or one filing.
- A streaming run processes each filing through convert → segment → filter → compact → publish. CPU work for upcoming filings may run ahead; model calls handle one filing at a time.
- **Resume:**
  - a filing is skipped only when its inputs, code version and settings (models, prompt, thresholds, compaction rules) match a finished result;
  - changing a later step reruns only that step and the steps after it;
  - an interrupted filing is redone from the start, without partial or duplicate results.
- Each stage writes a per-filing status record and a corpus summary (CSV + JSON) with o200k_base token counts.

### F8. Monitor (local Flask page, read-only)
- **Overview:**
  - per stage, filings done / running / failed / pending / source-incomplete;
  - total tokens at each step (raw → Markdown → kept → clean) and the overall saving;
  - free disk space with a warning threshold.
- **Current run:** which filing and chunk are being processed, speed, estimated time remaining for the selection and for the full target, last update time.
- **Filing list:** filter by company, year, form, status and stage.
- **Filing detail:**
  - the status of each stage and any errors;
  - chunks with their tier-1 score, label, deciding tier and reason;
  - token counts at each step.

  Clicking a chunk shows its original text.
- **Problem lists:** conversion failures and unknown formats, UNCERTAIN-by-error chunks, compaction fallbacks, and filings with nothing kept.
- The page refreshes automatically while a run is active and still works when nothing is running.

## 3. Not in this tool

The following belong elsewhere or are out of scope:
- **Downloading filings:** done by the user's separate code.
- **Final extraction of amounts, periods and evidence:** done by Codex or Claude scheduled tasks reading the clean files.
- **XBRL facts extraction:** possible later.
- **Any batch API, and any request mixing filings.**
- **Monitor limits:** no starting, stopping or configuring runs from the monitor; no labeling inside the monitor (labeling happens in exported spreadsheets).
- **Access and deployment:** no authentication, no multiple users, no deployment beyond the user's laptop.
- **Content not in the filing:** no OCR of images, and no fetching of material not in the downloaded files.
- **Other forms:** forms outside the 10-K family listed in §0 (for example 20-F or 40-F).
- **Removing content without a model:** removing tables of contents, page headers or repeated text by rule. Only the local models decide what to drop; compaction never removes content.

## 4. Important edge cases

**Input**
- Manifests listing missing files.
- New filings added while a run is in progress (they are picked up by the next scan).
- A raw file replaced by the downloader.
- Very large complete submissions.
- Unusual encodings.
- Main documents split into several files, or PDF-only filings.

**Forms**
- 10-KSB item numbering.
- 10-KT transition periods: a period shorter than a year, which must be recorded.
- Amendments that contain only a few Items or only exhibits, and amendments that restate a full report.
- 10-K405 and 10-KSB405.

**Structure and segmentation**
- Item references in the table of contents, page headers/footers, cross-references and ordinary prose.
- Headings that are only bold or centered text in HTML, or all-caps lines in legacy text.
- Older filings that put Items 7/8 in an EX-13 annual report "incorporated by reference". The EX-13 text must be segmented and filtered like the main document.
- Missing, merged ("Items 7 and 7A"), out-of-order or repeated Items.
- Tables split across pages, "continued" tables, very wide tables, and nested HTML tables kept as raw HTML.
- Very large filings (the largest in the sample is about 2.25 million tokens) and very small ones.

**Compaction**
- Tables whose cells contain several paragraphs.
- `$` and `%` in their own cells.
- Footnote markers.
- Merged cells.
- Fixed-width tables, which must not be touched.

**Model and run failures**
- Malformed model output, GPU out of memory, model server not running, and a crash in the middle of a filing.
- Rerunning after a change to a model, prompt, threshold, segmentation rule or compaction rule.
- The disk running low during a long run.

**Corpus quirks**
- Incomplete-source filings, and filings flagged `REVIEW_REQUIRED` by conversion. These are still processed, with the flag carried into the clean file's header.

## 5. Assumptions

**Machine and users**
- The tool runs only on the user's Windows laptop (RTX 5000 Ada, about 16 GB GPU memory, about 64 GB RAM; 566 GB free on C: on 2026-10-12) and is used only by the user.
- The monitor listens on 127.0.0.1 only.
- All filing data stays on the laptop; local models run on the laptop. The only outbound traffic is downloading model weights and the model runtime, with the user's approval.

**Input**
- All filings are in `10K_Filings/`, in the downloader's `filings.csv` layout, and new ones keep being added.
- The tool never modifies input files, nor the existing `Markdown_10K/` outputs.

**Throughput**
- At the target size, a generative model reading every chunk would take an estimated 1–3 months of GPU time. Hence the two tiers.
- The real full-corpus time is measured in a pilot before the full run.

**Model selection**
- The labeled sample is about 12 development filings plus the external validation set, all labeled by the user.
- The version 1 target is recall of at least 95% of user-labeled relevant sections on held-out filings, including the external ones (target to be confirmed).
- Downloading weights or installing a runtime needs the user's approval first.

**Interpretation of the definitions**
- Organizational capital does not mean all of SG&A: only content about the activities listed in the table above.
- Asset balances, amortization and impairment of intangibles count as relevant and are kept. Telling them apart from current-year spending is left to the scheduled tasks.

**Token counts** are o200k_base text-token counts. They are not billing measurements, and identical model answers on compacted text are not guaranteed.

## 6. Acceptance checklist

**Input and running**
- [ ] After new companies or filings are added to `10K_Filings/`, `scan` registers exactly the new ones, and a run processes only them. A missing file listed in a manifest fails only that filing.
- [ ] Listing filings for a command does not rescan the whole corpus on disk.
- [ ] One command takes a single raw filing all the way to its clean file, map and index row.
- [ ] A second run skips finished filings. After a change to a model, prompt, threshold or compaction rule, only the affected steps are redone.
- [ ] If a run is killed in the middle of a filing and restarted, that filing is redone. There are no partial or duplicate rows.
- [ ] Input files and `Markdown_10K/` have the same SHA-256 hashes before and after a run.

**Conversion**
- [ ] 10-KSB, 10-KT and amendment filings convert, and existing `Markdown_10K/` outputs are reused, not reconverted.
- [ ] Amendments are marked and, when possible, linked to the filing they amend.

**Segmentation**
- [ ] For every processed filing, the chunks joined in order equal the Markdown file exactly. The corpus summary reports 0 mismatches.
- [ ] No table is separated from its caption, unit line, header or footnotes.
- [ ] On user-checked sample filings (development and external; each raw format, each era, and 10-KSB), table-of-contents entries and prose mentions do not create Item boundaries, and the Item boundaries match the user's check.

**Filtering**
- [ ] Every chunk has a tier-1 score or a recorded tier-1 failure, and exactly one final label. Failures count as UNCERTAIN.
- [ ] Every request log entry contains exactly one filing ID, and no request contains text from another filing.
- [ ] Cross-referenced notes from kept sections are kept, with the reason recorded.
- [ ] The chosen models and thresholds meet the recall target on held-out filings, including the external ones.

**Compaction and output**
- [ ] For every published filing, the clean file's text, numbers, signs and table cells match the kept text. Any filing that failed a check uses the uncompacted kept text, and the fallback is recorded.
- [ ] Every clean file has the metadata header. Its map file traces every part back to character ranges in the original Markdown.
- [ ] The corpus index lists every filing with status, paths and token counts at each step. Filings with nothing kept are explicitly marked.
- [ ] The corpus report shows weighted token savings at each step (Markdown → kept, kept → clean, Markdown → clean, raw → clean) and per-filing figures.

**Pilot**
- [ ] A pilot of several hundred filings from companies outside the first 100 completes. It records measured speed, disk use per filing, failure rates, token savings, and a full-corpus time and disk estimate.

**Monitor**
- [ ] During a run, the monitor shows the current filing and chunk, and progress numbers that update.
- [ ] The monitor shows per-stage counts, token totals and free disk space when no run is active.
- [ ] From the monitor, the user can open any filing, see its chunks with scores, labels and reasons, and read a chunk's original text.
- [ ] Conversion failures, compaction fallbacks, error-UNCERTAIN chunks and nothing-kept filings are listed.

**Tests**
- [ ] `pytest -q` passes without a GPU, a downloaded model or network access, including an end-to-end test on small fixtures.
