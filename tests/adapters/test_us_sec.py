# -- the window is the bound, not a row count ---------------------------------
#
# `filings.head(limit)` threw away everything past the newest `limit` filings even
# though the date window had already narrowed the set correctly. Measured over
# 2026-07-28..08-04 EDGAR published 4,218 Form 4 filings; at limit=50 per run the
# adapter reached under 1% of them, which is why the volume gate reported this source
# and sec_13dg as below expected volume.


class _FakeFiling:
    def __init__(self, n):
        self.n = n

    def xml(self):
        return f"<x>{self.n}</x>"


class _FakeFilings:
    """Stands in for edgartools' result set: iterable, with a head()."""

    def __init__(self, count):
        self._items = [_FakeFiling(i) for i in range(count)]
        self.head_calls = []

    def __iter__(self):
        return iter(self._items)

    def head(self, n):
        self.head_calls.append(n)
        return self._items[:n]


def test_no_limit_reads_the_whole_window():
    from whale_agent.ingestion.us_sec import take_filings

    filings = _FakeFilings(4218)
    assert len(list(take_filings(filings, None))) == 4218
    assert filings.head_calls == [], "head() must not be used when there is no cap"


def test_zero_limit_also_means_the_whole_window():
    from whale_agent.ingestion.us_sec import take_filings

    filings = _FakeFilings(120)
    assert len(list(take_filings(filings, 0))) == 120


def test_an_explicit_limit_is_still_honoured():
    """Backfill and tests pass a cap deliberately; that must keep working."""
    from whale_agent.ingestion.us_sec import take_filings

    filings = _FakeFilings(4218)
    assert len(list(take_filings(filings, 50))) == 50
    assert filings.head_calls == [50]
