from pipeline import corpus


def test_filing_ids_are_stable_unique_and_carry_metadata(small_corpus):
    first = corpus.list_filings(small_corpus.raw_dir, small_corpus.markdown_dir)
    second = corpus.list_filings(small_corpus.raw_dir, small_corpus.markdown_dir)
    assert [f.filing_id for f in first] == [f.filing_id for f in second]
    ids = {f.filing_id for f in first}
    assert ids == {"AAA_Alpha_Inc/1998_b", "AAA_Alpha_Inc/2020_a", "BBB_Beta_Corp/1993_c"}
    alpha = next(f for f in first if f.filing_id == "AAA_Alpha_Inc/2020_a")
    assert alpha.ticker == "AAA" and alpha.year == 2020
    assert alpha.markdown_path == small_corpus.markdown_dir / "AAA_Alpha_Inc" / "2020_a.md"
    assert alpha.audit_path.name == "2020_a.audit.json"


def test_select_filters(small_corpus):
    filings = corpus.list_filings(small_corpus.raw_dir, small_corpus.markdown_dir)
    assert [f.filing_id for f in corpus.select(filings, filing="aaa_alpha_inc\\2020_a")] == ["AAA_Alpha_Inc/2020_a"]
    assert len(corpus.select(filings, company="aaa")) == 2
    assert len(corpus.select(filings, company="BBB_Beta_Corp")) == 1
    assert [f.year for f in corpus.select(filings, year=1998)] == [1998]
    assert len(corpus.select(filings, limit=1)) == 1
