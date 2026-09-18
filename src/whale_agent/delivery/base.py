"""Shared delivery result type and the digest -> HTML converter.

The digest is generated as plain text with a known line structure (a title line, tier
headings in caps, numbered rows, indented continuation lines). Rather than pull in a
markdown library and a templating engine for that, this module escapes the text and
applies the handful of transforms an email actually needs: headings, row grouping, and
clickable source links. The plain-text body is always sent alongside as the multipart
alternative, so the email is readable even if the HTML is stripped.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_URL_IN_BRACKETS = re.compile(r"\[(https?://[^\]\s]+)\]")

_CSS = """
body { margin:0; padding:0; background:#f6f7f9; }
.wrap { max-width:680px; margin:0 auto; padding:24px 16px;
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
        font-size:15px; line-height:1.5; color:#1b1f24; }
.card { background:#ffffff; border:1px solid #e3e6ea; border-radius:10px; padding:20px 22px; }
h1 { font-size:18px; margin:0 0 4px; letter-spacing:.02em; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:#5c6773;
     margin:22px 0 8px; border-top:1px solid #e3e6ea; padding-top:14px; }
.lede { color:#3d444d; margin:0 0 6px; }
.coverage { color:#8a6d1f; background:#fff8e1; border-radius:6px; padding:6px 10px;
            font-size:13px; margin:10px 0 0; }
.row { margin:14px 0; }
.row .head { font-weight:600; }
.row .why { color:#3d444d; }
.row .note { color:#7a3e2f; font-size:13px; }
.row a { color:#2d6cdf; }
.footer { color:#6b7480; font-size:12px; margin-top:22px; border-top:1px solid #e3e6ea;
          padding-top:14px; }
@media (prefers-color-scheme: dark) {
  body { background:#14171a; }
  .wrap { color:#e6e9ec; }
  .card { background:#1c2024; border-color:#2c3238; }
  h2 { color:#9aa4b0; border-color:#2c3238; }
  .lede, .row .why { color:#c2c9d1; }
  .coverage { background:#2b2612; color:#e0c877; }
  .row .note { color:#e0a596; }
  .footer { color:#8b95a1; border-color:#2c3238; }
}
"""


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one send. `ok=False` is informational, never fatal."""

    channel: str
    ok: bool
    detail: str = ""

    def __bool__(self) -> bool:  # lets callers write `if result:`
        return self.ok


def _linkify(escaped: str) -> str:
    """Turn a bracketed source URL into an anchor. Runs after escaping, so the URL is
    already safe to embed."""
    return _URL_IN_BRACKETS.sub(lambda m: f'<a href="{m.group(1)}">source</a>', escaped)


def _themed_css(theme) -> str:
    """The fallback stylesheet with the theme's card, page, text, accent and font."""
    if theme is None:
        return _CSS
    return (
        _CSS.replace("background:#f6f7f9", f"background:{theme.page}")
        .replace("background:#ffffff", f"background:{theme.background}")
        .replace("color:#1b1f24", f"color:{theme.text}")
        .replace("color:#2d6cdf", f"color:{theme.accent}")
        .replace(
            "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif",
            theme.font_stack,
        )
    )


def digest_to_html(digest: str, subject: str | None = None, theme=None) -> str:
    """Render the plain-text digest as a standalone HTML email body.

    `theme` is an optional `delivery.theme.Theme`; it changes styles only, never text."""
    lines = digest.split("\n")
    title = html.escape(lines[0]) if lines else "Whale digest"
    body: list[str] = []
    open_row = False

    def close_row() -> None:
        nonlocal open_row
        if open_row:
            body.append("</div>")
            open_row = False

    for raw in lines[1:]:
        stripped = raw.strip()
        if not stripped:
            continue
        escaped = _linkify(html.escape(stripped))
        if stripped.startswith("TIER "):
            close_row()
            body.append(f"<h2>{escaped}</h2>")
        elif re.match(r"^\d+\.\s", stripped):
            close_row()
            body.append(f'<div class="row"><div class="head">{escaped}</div>')
            open_row = True
        elif stripped.startswith("Why:"):
            body.append(f'<div class="why">{escaped}</div>')
        elif stripped.startswith("Skeptic's note:") and open_row:
            body.append(f'<div class="note">{escaped}</div>')
        elif stripped.startswith("Coverage note:"):
            close_row()
            body.append(f'<p class="coverage">{escaped}</p>')
        elif stripped.startswith("Skeptic's note:"):
            close_row()
            body.append(f'<p class="footer">{escaped}</p>')
        else:
            close_row()
            body.append(f'<p class="lede">{escaped}</p>')
    close_row()

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(subject or 'Whale digest')}</title>"
        f"<style>{_themed_css(theme)}</style></head><body><div class='wrap'><div class='card'>"
        f"<h1>{title}</h1>" + "".join(body) + "</div></div></body></html>"
    )


def digest_subject(digest: str, on: str | None = None) -> str:
    """Subject line: the digest's own first line, which already carries the date."""
    first = digest.split("\n", 1)[0].strip()
    return first or f"Whale digest {on or ''}".strip()
