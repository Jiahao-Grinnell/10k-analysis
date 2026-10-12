# 10-K → Markdown

Converts each SEC 10-K in this project into its own Markdown file and counts tokens before and after conversion. Conversion runs entirely locally. It calls no LLM, does not filter sections by keyword, and uploads nothing. Original files are never modified.

The conversion, independent audit and token-report workflow is adapted from [Jiahao-Grinnell/10-k-analysis](https://github.com/Jiahao-Grinnell/10-k-analysis). This implementation is a conservative converter written with `lxml`, rather than a direct wrapper around `sec2md`. It handles the SGML complete submissions, early fixed-width text, HTML and inline XBRL in the current corpus, and preserves complex financial tables.

## Usage

Requires Python 3.12 or later. The current workspace already has the dependencies installed and the tokenizer cached, so you can run:

```powershell
& '.\.venv\Scripts\python.exe' convertmd.py --input '10K_Filings' --output 'Markdown_10K' --workers 8 --resume
```

In a new environment, install the dependencies first:

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
```

The first time `tiktoken` loads an encoding, it needs network access. After that it uses the local cache in `.cache/tiktoken`. Conversion itself downloads no attachments and needs no API key.

To test a single raw file:

```powershell
& '.\.venv\Scripts\python.exe' convertmd.py --input 'some-10-K.htm' --output 'test_outputs/single'
```

Other options:

- `--limit 10`: convert only the first 10 filings, for a trial run.
- `--workers 1`: strictly sequential local conversion; this is the default. Multiple workers only parallelize local format conversion; this is not LLM batch processing.
- `--tokenizer o200k_base --tokenizer cl100k_base`: count tokens with both encodings separately. The default is `o200k_base`.
- `--resume`: checks the SHA-256 of the raw content, the converter code, the tokenizer and the output file before reusing an existing result.
  - Version 1.1 also reuses outputs from the specified compatible 1.0 version whose text, number and table checks all passed. It keeps the hash of the code that actually did the conversion and records the current verifying version separately.
  - Outputs that failed their checks or are damaged are regenerated.

## Input and output

If `filings.csv` manifests exist, files are read according to the manifest (status `downloaded` / `already_present`), keeping the company folder structure. Without a manifest, `.htm`, `.html`, `.txt`, `.sgml` and `.sgm` files are read recursively. The format is detected from the content, not the file extension.

Outputs:

- One `.md` file per report, plus an `.audit.json` audit record with the same name.
- `conversion_report.csv`: per-filing token counts, savings and check results.
- `conversion_summary.json`: corpus-wide statistics.
- `conversion_report.md`: a readable summary report.
- `conversion_progress.jsonl`: an append-only run log. Repeated runs may add duplicate entries; the summary and CSV are authoritative for final statistics.

A conversion error does not stop the run: other files are still processed, and the error is recorded in the report. Exit codes:
- `0`: everything completed;
- `1`: at least one conversion error;
- `2`: no conversion errors, but some raw files are missing their report body.

A file that contains only the SEC header, or that was mistakenly downloaded as an exhibit such as EX-3 without the 10-K main document, produces an explicit placeholder Markdown. Placeholders do not count as successful extractions and are excluded from the savings figures.

## Preservation rules

- **Whole report kept:** the readable body of the entire report is kept, including the table of contents, risk factors, financial statements, notes and so on. No sections are removed based on intangible-investment keywords.
- **SGML submissions:** SGML `<DOCUMENT>` / `<TEXT>` records are separated first, then converted. The 10-K / 10-K405 main document and readable text exhibits are kept, including EX-13, which may contain the full annual report.
- **Excluded attachments:** technical attachments such as EX-27, EX-101 / XML and binary images are excluded and recorded in the audit. Images referenced in the body keep their reference and alt text, but are not OCR'd, and missing attachments are not downloaded.
- **HTML cleanup:** style attributes, scripts, technical headers, hidden XBRL and similar formatting overhead are removed. Visible inline XBRL numbers are kept as written: never rescaled, added or recalculated.
- **Tables:** ordinary rectangular tables become Markdown tables. Tables with merged cells, nesting or irregular shapes are kept as minimal HTML, preserving cell order and `rowspan` / `colspan`. The Markdown may therefore contain native HTML tables.
- **Legacy text:** ASCII tables and body text in old-style TXT filings are written as fenced text, preserving spacing, negative-number parentheses, symbols and footnote markers. For plain TXT filings the token count may rise slightly.
- **Encodings:** UTF-8, UTF-16, declared character sets and CP1252 / Latin-1 are each decoded explicitly; characters are never dropped with `errors=ignore`.

## Checks and limitations

**Checks.** Each retained document is checked separately:
- the cleaned original visible text matches the text of the re-rendered Markdown (ignoring whitespace);
- no numeric occurrences are missing;
- table cell order and merged-cell structure match.

If ordinary Markdown conversion fails these checks, the converter automatically falls back to a minimal HTML body and checks again.
- `CHECKED_TEXT` means these programmatic checks passed.
- Images, encoding or parsing diagnostics, absolute positioning, or failed checks are flagged `REVIEW_REQUIRED`.

**Limitations.**
- The checks do not prove identical browser rendering, and they do not replace review of the financial meaning.
- Cleanup uses the HTML tree and inline hidden styles; it does not execute JavaScript or full CSS layout.
- Image content in the raw files and externally linked material that was not downloaded are not covered by the text-completeness checks. A report that passes every text check may still need review because of missing images.

Run the tests:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' smoke_conversion.py
& '.\.venv\Scripts\python.exe' verify_outputs.py
```

The unit tests cover:
- merged and nested tables;
- numbers and signs;
- legacy text and SGML attachments;
- XBRL;
- encodings and escaping of numbered paragraphs;
- saving and resuming;
- missing report bodies;
- weighted statistics.

Real-sample tests cover the main formats and the largest files in the current corpus.

After a full conversion, `verify_outputs.py` re-checks every filing and writes `corpus_validation.json`. It checks:
- the SHA-256 of each raw file against the download manifest;
- the SHA-256 of each output file against its audit record;
- the current converter version;
- the text / number / table check results;
- the summary token totals.

## How to read the token savings

The report gives two baselines:

1. **Raw file → Markdown:** the raw file string includes HTML / SGML tags, hidden XBRL, encoded attachments and so on.
2. **Raw text of the retained documents → Markdown:** compares only the retained main report and text exhibits. This separates savings from dropping technical attachments from savings from removing formatting overhead.

The corpus-wide figure is `1 − total Markdown tokens / total baseline tokens`, not a simple average of per-file percentages. Placeholder files for missing report bodies are excluded from the comparison.

These are local measurements made with `tiktoken` on the actual strings. They are suitable for comparing the size of raw HTML text and Markdown text as model input. They are not the actual bill or internal token count after uploading a file to ChatGPT, which may parse HTML first. Prompt, output, reasoning and image tokens are not included.

Later LLM extraction can strictly follow "read one Markdown file → independent request → save the result → next file". This converter does not implement LLM extraction or browser automation.
