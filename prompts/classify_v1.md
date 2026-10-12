You screen one excerpt of one SEC 10-K filing. A later step will extract intangible-investment amounts from the text you keep, so missing relevant text is far worse than keeping irrelevant text.

{definitions}

Label the excerpt:
- KEEP: it contains, or may contain, information about any category above. This includes amounts, accounting policies, tables, footnotes or descriptions of: expenses or spending, capitalized costs, additions or acquisitions, asset balances, amortization, impairment, useful lives, licenses or royalties, and the activities themselves. Text about SG&A counts only when it may cover the activities listed for organizational capital, but if you cannot tell, do not drop it.
- UNCERTAIN: you cannot tell whether it is relevant (for example, a table without enough context, or a fragment).
- DROP: it is clearly unrelated to every category.

When unsure between KEEP and DROP, answer UNCERTAIN. Judge only the excerpt text, never outside knowledge about the company.

Answer with JSON only: "reason" (one short sentence), "label" (KEEP, DROP or UNCERTAIN), and "categories" (zero or more of: rd, advertising_marketing, organizational_capital, intellectual_property).
