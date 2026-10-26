# Prompt: extract intangible investment by year from one company's merged 10-K file

You are a careful financial research assistant. The attached Markdown file contains ALL of one
company's annual reports (10-K, 10-K405, 10-KSB, 10-KT, or amendments), one after another, in fiscal-year
order. Each filing starts with a header line like:

`# FILING 7 of 32: fiscal year 2000 | form 10-K | period end 2000-09-30 | filed 2000-12-14 | accession ...`

Your job: for every filing, find how much the company spent or held in each of the four
intangible-investment categories below, and return the results as CSV.

## The four categories (research definitions)

| Column | Category | What it covers |
|---|---|---|
| `rd` | Research & Development (R&D) | Creates new products, technologies, and production processes; improvement in technologies, design, and production processes. |
| `advertising_marketing` | Advertising, marketing-related intangibles | Advertising campaigns, market research, brand development, trademarks to build customer recognition and demand, increase sales. |
| `sga` | Selling, General & Administrative (SG&A), i.e. management & organizational capital | Management systems, business-process improvements, how the firm organizes production, management, data processing, employee training programs, and organizational capabilities that build workers' human capital and firm-specific skills. |
| `intellectual_property` | Intellectual property | Patents, copyrights, trademarks, licenses; creates or controls economically valuable knowledge and rights. |

What value to record for each:
- `rd`: the R&D expense for the fiscal year (including in-process R&D only if the filing reports it as part of R&D; otherwise mention it in the notes).
- `advertising_marketing`: advertising / marketing / promotion expense for the fiscal year, usually stated in the accounting-policies note or in MD&A.
- `sga`: total selling, general and administrative expense for the fiscal year as reported on the income statement. If the company reports selling and G&A separately, add them and say so in the notes.
- `intellectual_property`: the net carrying amount at fiscal year end of identifiable intangible assets that are intellectual property (patents, copyrights, trademarks/trade names, licenses, developed technology). Exclude goodwill. If only a total "intangible assets" figure is given and it clearly consists of these, use it and say so.

## How to read: read every filing in full

1. Work through the filings **one at a time, from FILING 1 to the last one**. Do not skip any filing,
   and do not stop early.
2. For each filing, **read the whole filing from start to end**: the business description (Item 1),
   selected financial data (Item 6), MD&A (Item 7), the financial statements and **every note to the
   financial statements** (Item 8), Items 14/15, and any attached annual report (Exhibit 13). Amounts
   are often only in a note, a table, or a footnote under a table.
3. **Do not use keyword search or keyword matching to find the numbers.** Companies use many different
   names (for example "product development", "engineering", "technology and development",
   "promotion", "marketing and selling", "brand building", "acquired technology"). Judge by the
   *meaning* of the text and tables against the definitions above, not by whether a word appears.
4. Use the number for **that filing's own fiscal year** (the current-year column), not the
   prior-year comparison columns. If a later filing restates an earlier year, keep the number from the
   original filing and mention the restatement in the notes.
5. Read the units carefully (thousands, millions, billions, currency). Convert every value to
   **US dollars in millions**. Keep the sign as reported.
6. **A missing disclosure is NOT zero.** If a filing does not disclose the amount, leave the cell
   empty. Only write `0` when the filing explicitly reports zero.
7. Never estimate, interpolate between years, or copy a number from a neighbouring year.
8. If two filings have the same fiscal year (for example an original and an amendment), use the most
   complete one and say so in the notes.

Before writing the output, check yourself: the number of rows in Table 1 must equal the number of
filings in the file, and every non-empty cell must have a matching row in Table 2.

## Output: exactly two CSV code blocks, nothing else

**Table 1 — values** (one row per filing, in fiscal-year order):

```csv
fiscal_year,period_end,form,accession,rd,advertising_marketing,sga,intellectual_property,currency_unit,notes
```

- Values are numbers only (no `$`, no commas, no text), in `currency_unit` = `USD millions`.
- `notes`: short remarks, e.g. "SG&A = selling 120 + G&A 80", "advertising not disclosed",
  "R&D restated in FY2003 filing". Put text that contains commas in double quotes.

**Table 2 — evidence** (one row per non-empty value in Table 1):

```csv
fiscal_year,category,value_usd_millions,value_as_reported,unit_as_reported,location,quote
```

- `value_as_reported`: the number exactly as printed (e.g. `1,234.5`), `unit_as_reported` e.g. `thousands`.
- `location`: where you found it (e.g. "Item 8, Consolidated Statements of Operations" or
  "Item 8, Note 1 Summary of Significant Accounting Policies, Advertising Costs").
- `quote`: the exact sentence or table row from the filing, at most 40 words, in double quotes.
