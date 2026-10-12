# SPEC.md — 10-K Intangible-Investment Section Pipeline (version 1)

## 1. Core functionality and main workflow

The tool processes SEC 10-K filings one at a time. For each filing it finds the parts that may describe intangible investment, so that a later extraction step only has to read those parts. Nothing is deleted: every decision is recorded, and the original text stays traceable.

Main workflow:

1. **Convert.** Raw filing (HTML, inline XBRL, SGML complete submission, or legacy fixed-width text) → one Markdown file. The existing `convertmd.py` already does this for the current corpus.
2. **Segment.** Markdown → an ordered list of sections that follows the filing's own structure: Part → Item → subsection → financial note or table. Each section records where it came from.
3. **Choose a local model (done once, and again only when needed).** Candidate local models classify the sections of a labeled sample of filings. The tool reports how well each candidate does, and the user picks one.
4. **Classify.** The chosen local model reads every section of every filing and labels it KEEP, DROP or UNCERTAIN. For each filing the tool writes:
   - the labels;
   - a "kept text" file containing the original text of every KEEP and UNCERTAIN section, in document order.
5. **Monitor.** A local web page shows what has been processed, what is running, and what failed.

Intangible-investment categories (from `Intangible_investment.docx`):

| Category | What counts |
|---|---|
| Research & Development | Creating new products, technologies and production processes; improving technology, design and production processes |
| Advertising / marketing-related intangibles | Advertising campaigns, market research, brand development, trademarks that build customer recognition and demand |
| Organizational capital (SG&A) | Management systems, business-process improvement, how the firm organizes production and management, data processing, employee training, firm-specific skills |
| Intellectual property | Patents, copyrights, trademarks, licenses; creating or controlling valuable knowledge and rights |

The definitions document says these categories are expected in Items 1, 6, 7, 8, 14 and 15, including tables and the notes to the financial statements. Version 1 uses that list only for reporting and checks. It is **never** used to choose which sections the model reads.

## 2. Essential features

### F1. Conversion stage
- Reuse `convertmd.py` unchanged in behavior.
- Existing `Markdown_10K/` outputs stay valid, and reorganizing the code must not force a full reconversion.
- The two incomplete-source placeholders are carried through as "source incomplete". They are not processed further and not counted as successes.

### F2. Segmentation stage

**How boundaries are found**
- Boundaries come from document structure, not keywords. A table-of-contents entry or a sentence that mentions "Item 7" must not start a new section.
- Legacy fixed-width text has no HTML headings, so it needs its own structural rules (for example, line layout and capitalized headings).

**What each section looks like**
- A table stays in one section together with its caption, unit line (for example "in millions"), headers and footnotes.
- Long sections are split into reading chunks of a configured maximum size. Each chunk carries its heading path and position. When a long table is split, its header is repeated.
- Each section/chunk records:
  - filing ID;
  - heading path (Part / Item / subsection / note);
  - character start and end in the Markdown file;
  - token count;
  - type (prose / table / mixed).

**Lossless check**
- Concatenating all sections in order reproduces the filing's Markdown exactly. The tool checks this for every filing.

### F3. Labeled sample and model selection
- The tool picks a sample of filings that covers each raw format, each era (≤2000, 2001–2008, 2009–2018, 2019+) and several industries.
- It exports their sections to a spreadsheet for the user to label (relevant / not relevant, plus optional category).
- It reads the labels back and runs each candidate model on the sample. Per candidate it reports:
  - recall of relevant sections;
  - share of tokens dropped;
  - speed;
  - GPU memory use;
  - error rate.
- The chosen model, its version/quantization, the prompt and the generation settings are recorded in IMPLEMENTATION_PLAN.md and in every result.

### F4. Classification stage

**Model input and output**
- Every section of every filing goes to the model; none is skipped by keyword or Item.
- Each request contains content from exactly one filing: the section, its heading path, short surrounding context, and the category definitions.
- The model returns a label (KEEP / DROP / UNCERTAIN), optional categories, and a one-sentence reason.

**When labels are overridden**
- Errors, timeouts, out-of-memory failures and unreadable answers become UNCERTAIN, with the error recorded.
- If any chunk of a section is KEEP or UNCERTAIN, the whole enclosing section is kept.
- If a kept section explicitly refers to a note, table or exhibit in the same filing (for example, "see Note 7"), that target is kept too, and the reason is recorded.

**Order and outputs**
- Filings are processed one at a time. One filing's outputs are written before the next filing starts.
- Per filing outputs:
  - the label file (one row per section, with provenance, model info and reason);
  - the kept-text Markdown (original text only, with section markers for provenance);
  - a short summary: sections and tokens kept / dropped / uncertain, failures, and kept tokens per Item.

### F5. Command-line runner
- Run one stage or all stages, on the whole corpus, a subset (`--limit`, company, year) or one filing.
- **Resume:** skip a filing only when its inputs, code version and settings (including model and prompt) match a finished result. Changing the prompt or model reruns classification only.
- An interrupted filing is redone from the start, without leaving partial or duplicate results.
- Each stage writes a per-filing status record and a corpus summary (CSV + JSON), with o200k_base token counts.

### F6. Monitor (local Flask page, read-only)
- **Overview:** per stage, the number of filings done / running / failed / pending / source-incomplete, plus total tokens.
- **Current run:** which filing and section are being processed, speed, estimated time remaining, last update time.
- **Filing list:** filter by company, year, status and stage.
- **Filing detail:** the status of each stage, errors, and a list of sections with labels and reasons. Clicking a section shows its original text.
- **Error list:** all failures and UNCERTAIN-by-error sections.
- The page refreshes automatically while a run is active and still works when nothing is running.

## 3. Not in version 1

The following are out of scope for version 1:
- Downloading filings from EDGAR. The raw corpus in `10K_Filings/` is fixed input.
- Deterministic compaction of Markdown (wrapper removal and similar).
- XBRL facts extraction.
- Final extraction of amounts by a cloud LLM.
- Any batch API, and any request mixing filings.
- Starting, stopping or configuring runs from the monitor (the monitor is view-only).
- Labeling sections inside the monitor (labeling happens in the exported spreadsheet).
- Authentication, multiple users, and deployment beyond the user's laptop.
- OCR of images, and fetching exhibits that are not in the downloaded files.

## 4. Important edge cases

**Structure and segmentation**
- Item references in the table of contents, page headers/footers, cross-references and ordinary prose.
- Headings that are only bold or centered text in HTML, or all-caps lines in legacy text.
- Older filings that put Items 7/8 in an EX-13 annual report "incorporated by reference". The EX-13 text must be segmented and classified like the main document.
- Missing, merged ("Items 7 and 7A"), out-of-order or repeated Items; 10-K405 and 10-K/A forms.
- Tables split across pages, "continued" tables, very wide tables, and nested HTML tables kept as raw HTML.
- Very large filings (the largest Markdown is about 2.25 million tokens) and very small ones.

**Model and run failures**
- Malformed model output, GPU out of memory, model server not running, and a crash in the middle of a filing.
- Rerunning after a change to the prompt, model or segmentation rules.

**Corpus quirks**
- The two incomplete-source filings, and filings flagged `REVIEW_REQUIRED` by conversion. These are still processed, with the flag carried forward.

## 5. Assumptions

**Machine and users**
- The tool runs only on the user's Windows laptop (RTX 5000 Ada, about 16 GB GPU memory, about 64 GB RAM) and is used only by the user.
- The monitor listens on 127.0.0.1 only.
- All filing data stays on the laptop. The local model runs on the laptop; no filing content is sent to an external service.

**Data**
- The corpus is the current 2,896 files (2,894 usable) for 100 companies, from the 1990s to 2026.
- `10K_Filings/` and `Markdown_10K/` are never modified.

**Model selection**
- The labeled sample is about 12 filings, labeled by the user (to be confirmed).
- Version 1 targets a recall of at least 95% of user-labeled relevant sections, measured on labeled filings not used to tune the prompt (target to be confirmed).
- Whether a model "fits" is judged on this laptop, with the runtime chosen in IMPLEMENTATION_PLAN.md. Downloading weights or installing a runtime needs the user's approval first.

**Interpretation of the definitions**
- Organizational capital does not mean all of SG&A: only content about the activities listed in the table above.
- Asset balances, amortization and impairment of intangibles count as relevant evidence and are kept. Telling them apart from current-year spending is left to the later extraction step.

## 6. Acceptance checklist

**Running the pipeline**
- [ ] One command takes a single raw filing all the way to labels and a kept-text file. The outputs can be opened and read.
- [ ] A second run of the same command skips the finished filing. After the prompt setting is changed, only classification is redone.
- [ ] If a run is killed in the middle of a filing and restarted, that filing is redone. There are no partial or duplicate rows.
- [ ] Raw files and `Markdown_10K/` have the same SHA-256 hashes before and after a full run.

**Segmentation**
- [ ] For every processed filing, the sections joined in order equal the Markdown file exactly. The corpus summary reports 0 mismatches.
- [ ] No table is separated from its caption, unit line, header or footnotes, and split tables repeat their header. This is checked on sample filings from each raw format.
- [ ] On sample filings checked by the user (at least one per raw format and era), table-of-contents entries and prose mentions do not create Item boundaries, and the Item boundaries match the user's check.

**Classification**
- [ ] Every section of every processed filing has exactly one label. Failures appear as UNCERTAIN with an error message.
- [ ] Every request log entry contains exactly one filing ID, and no request contains text from another filing.
- [ ] The kept text of every filing consists only of verbatim slices of its Markdown, in order, and includes every KEEP and UNCERTAIN section.
- [ ] Cross-referenced notes from kept sections are kept, with the reason recorded.

**Model selection**
- [ ] The report lists each candidate's recall, tokens dropped, speed, GPU memory and error rate on the labeled sample.
- [ ] The chosen model meets the recall target on held-out labeled filings.

**Corpus report**
- [ ] The report gives per-filing status, tokens before and after, the share kept, kept tokens per Item, and a list of failures.
- [ ] Filings where nothing is kept in Items 7 or 8 are listed for review.

**Monitor**
- [ ] During a run, the monitor shows the current filing and section, and progress numbers that update.
- [ ] The monitor shows per-stage counts when no run is active.
- [ ] From the monitor, the user can open any filing, see its sections with labels and reasons, and read a section's original text.

**Tests**
- [ ] `pytest -q` passes without a GPU or downloaded model, including an end-to-end test on small fixtures.
