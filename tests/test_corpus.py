from contextlib import closing

from conftest import registered
from pipeline import corpus, state


def test_filing_ids_are_stable_unique_and_carry_metadata(small_corpus):
    first = registered(small_corpus)
    second = registered(small_corpus)
    assert list(first) == list(second)
    assert set(first) == {"AAA_Alpha_Inc/1998_b", "AAA_Alpha_Inc/2020_a", "BBB_Beta_Corp/1993_c"}
    alpha = first["AAA_Alpha_Inc/2020_a"]
    assert alpha.ticker == "AAA" and alpha.year == 2020 and alpha.form == "10-K" and not alpha.is_amendment
    assert alpha.raw_path == small_corpus.raw_dir / "AAA_Alpha_Inc" / "2020_a.htm"
    assert alpha.markdown_path == small_corpus.markdown_dir / "AAA_Alpha_Inc" / "2020_a.md"
    assert alpha.audit_path.name == "2020_a.audit.json"
    assert len(alpha.raw_sha256) == 64  # manifest had no sha256, so the file was hashed


def test_select_filters(small_corpus):
    registered(small_corpus)
    with closing(state.connect(small_corpus.db_path)) as db:
        pick = lambda **kw: [f.filing_id for f in corpus.select(small_corpus, db, **kw)]
        assert pick(filing="aaa_alpha_inc\\2020_a") == ["AAA_Alpha_Inc/2020_a"]
        assert len(pick(company="aaa")) == 2
        assert len(pick(company="BBB_Beta_Corp")) == 1
        assert len(pick(company="1")) == 3  # CIK without leading zeros
        assert pick(year=1998) == ["AAA_Alpha_Inc/1998_b"]
        assert len(pick(form="10-k")) == 3
        assert len(pick(limit=1)) == 1
