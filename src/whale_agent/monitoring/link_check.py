"""Verifies the report links a weekly email is about to send actually resolve.

The reader has twice received a weekly whose "our report" links were dead: the pages
were rendered but not yet published to Cloudflare when the email went out, and the
pages existed minutes later. CI's workflow gets the ordering right (deploy, then send).
The local/manual path did not, because the safe ordering depended on a human doing two
things in the right order and remembering it. This module follows the same instinct as
`monitoring/health.py` (`thin_window`, `core_sources_stale`): fail closed and say why,
rather than deliver an email full of dead links.

It checks the ACTUAL URLS the email is about to carry, not merely that some page on the
site exists: each article link, the overview link, and the tracked-manager report link
together with every `id="f-<accession>"` fragment the email points into it. A page that
exists but is missing the anchor a link targets is the same failure wearing a different
hat -- this project has hit that variant too -- so the fragment is checked against the
fetched body, not assumed present just because the page loaded.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["ReportLink", "FetchResult", "Fetcher", "default_fetcher", "broken_links"]


@dataclass(frozen=True)
class FetchResult:
    """The outcome of fetching one URL once."""

    ok: bool
    status: int | None
    body: str
    error: str = ""


Fetcher = Callable[[str], FetchResult]


@dataclass(frozen=True)
class ReportLink:
    """One link the email is about to carry, and what it must be true for.

    `fragments` are the `id="..."` values the email's anchors (e.g. `#f-<accession>`)
    point at within this page; empty when the link targets the page as a whole.
    """

    url: str
    label: str
    fragments: tuple[str, ...] = ()


def default_fetcher(url: str, timeout: float = 10.0) -> FetchResult:
    """A real HTTP GET. Swapped out in tests -- network calls are forbidden there.

    Cloudflare returns 403 for urllib's default `Python-urllib/x.y` User-Agent even
    when the page is live and a browser or curl gets 200, so a real one is required.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "whale-agent-link-check/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8", errors="replace")
            return FetchResult(ok=200 <= status < 300, status=status, body=body)
    except urllib.error.HTTPError as exc:
        return FetchResult(ok=False, status=exc.code, body="", error=f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - any transport failure means "not reachable"
        return FetchResult(ok=False, status=None, body="", error=str(exc))


def broken_links(links: list[ReportLink], *, fetch: Fetcher = default_fetcher) -> list[str]:
    """One problem line per link that will not resolve for a reader, else an empty list.

    Each distinct URL is fetched at most once, however many links or fragments point at
    it, so a report with a dozen anchors into the same tracked-filings page costs one
    request rather than a dozen.
    """
    problems: list[str] = []
    cache: dict[str, FetchResult] = {}
    for link in links:
        if not link.url:
            continue
        result = cache.get(link.url)
        if result is None:
            result = fetch(link.url)
            cache[link.url] = result
        if not result.ok:
            detail = result.error or f"HTTP {result.status}"
            problems.append(f"{link.label}: {link.url} is not reachable ({detail})")
            continue
        for fragment in link.fragments:
            if f'id="{fragment}"' not in result.body and f"id='{fragment}'" not in result.body:
                problems.append(
                    f"{link.label}: {link.url} is reachable but has no anchor "
                    f"matching #{fragment}"
                )
    return problems
