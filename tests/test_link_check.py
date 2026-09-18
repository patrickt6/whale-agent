"""The weekly send's reachability gate: block on dead links, not merely on "page exists".

No network calls -- every fetch is stubbed. These tests pin the ordering guard and the
refusal, not the internet.
"""

from __future__ import annotations

from whale_agent.monitoring.link_check import FetchResult, ReportLink, broken_links


def _ok(body: str = "<html>ok</html>") -> FetchResult:
    return FetchResult(ok=True, status=200, body=body)


def _dead() -> FetchResult:
    return FetchResult(ok=False, status=404, body="", error="HTTP 404")


def test_no_problems_when_every_link_resolves():
    links = [ReportLink(url="https://example.test/a", label="note a")]
    assert broken_links(links, fetch=lambda url: _ok()) == []


def test_unreachable_url_is_reported_by_url_and_label():
    links = [ReportLink(url="https://example.test/dead", label="research note: Foo")]
    problems = broken_links(links, fetch=lambda url: _dead())
    assert len(problems) == 1
    assert "research note: Foo" in problems[0]
    assert "https://example.test/dead" in problems[0]
    assert "404" in problems[0] or "HTTP 404" in problems[0]


def test_page_reachable_but_missing_anchor_is_reported():
    """A page that loads but lacks the fragment the email points at is the same
    failure wearing a different hat -- must be caught, not treated as a pass."""
    links = [
        ReportLink(
            url="https://example.test/report",
            label="tracked-manager report",
            fragments=("f-0001234567",),
        )
    ]
    problems = broken_links(links, fetch=lambda url: _ok("<html>no anchors here</html>"))
    assert len(problems) == 1
    assert "f-0001234567" in problems[0]
    assert "https://example.test/report" in problems[0]


def test_anchor_present_passes():
    links = [
        ReportLink(
            url="https://example.test/report",
            label="tracked-manager report",
            fragments=("f-0001234567",),
        )
    ]
    body = '<html><div id="f-0001234567">filing</div></html>'
    assert broken_links(links, fetch=lambda url: _ok(body)) == []


def test_single_quoted_id_attribute_also_counts():
    links = [
        ReportLink(url="https://example.test/report", label="report", fragments=("f-abc",))
    ]
    body = "<html><div id='f-abc'>filing</div></html>"
    assert broken_links(links, fetch=lambda url: _ok(body)) == []


def test_each_distinct_url_is_fetched_only_once():
    calls: list[str] = []

    def counting_fetch(url: str) -> FetchResult:
        calls.append(url)
        return _ok('<html><div id="f-a">x</div><div id="f-b">y</div></html>')

    links = [
        ReportLink(url="https://example.test/report", label="a", fragments=("f-a",)),
        ReportLink(url="https://example.test/report", label="b", fragments=("f-b",)),
    ]
    assert broken_links(links, fetch=counting_fetch) == []
    assert calls == ["https://example.test/report"]


def test_link_with_no_url_is_skipped():
    links = [ReportLink(url="", label="omitted link")]
    assert broken_links(links, fetch=lambda url: _dead()) == []
