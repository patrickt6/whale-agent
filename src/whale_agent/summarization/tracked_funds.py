"""Render the tracked-manager section of the weekly.

Deliberately separate from the rest of the weekly's rendering. The aggregation sections
describe a seven-day window; this one describes standing positions on a quarterly clock,
and the reader has to be told which is which or the two blur into a claim neither source
supports. Every line here carries its own as-of date for that reason.
"""

from __future__ import annotations

import textwrap
from datetime import date

from whale_agent.ingestion.fund_watchlist import FundSnapshot

STALE_13F_DAYS = 135  # the statutory worst case: quarter end plus the 45-day deadline


def format_usd_short(value: int) -> str:
    """Compact money, shared with the HTML renderer so the two agree exactly."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    return f"${value:,}"


def aggregate_figure_tokens(snapshots: list[FundSnapshot]) -> set[str]:
    """The portfolio totals this section prints, in the exact form it prints them.

    Handed to the egress guard as an allowlist. A 13F portfolio is legitimately larger
    than the per-disclosure ceiling, and every one of these was summed from an
    information table we parsed ourselves rather than read off a vendor field.
    """
    tokens: set[str] = set()
    for snap in snapshots:
        if snap.error is None and snap.has_13f:
            tokens.add(format_usd_short(snap.total_value_usd))
            # Only when there are option rows: a fund with none would otherwise put
            # "$0" into the allowlist, which is not a figure this section prints.
            if snap.put_name_count:
                tokens.add(format_usd_short(snap.put_notional_usd))
            if snap.call_name_count:
                tokens.add(format_usd_short(snap.call_notional_usd))
            # Nothing else belongs here. `allow_figures` exempts a token from the
            # egress ceiling across the whole email body, not just this section, so a
            # figure that is allowlisted but never printed is a standing permission for
            # it to appear anywhere, including in model-written prose. Two used to be:
            # the top three holdings, which no renderer emits, and the vendor's
            # `marketValue`, which is the option-inclusive portfolio total. For
            # Situational Awareness LP that is $13,676,657,577, the exact figure the
            # put/call split exists to stop presenting as ownership.
    return tokens


def option_note(snap: FundSnapshot) -> str:
    """One clause on the option rows of a 13F, or empty when there are none.

    Wording is constrained by what the form actually discloses. It says "notional"
    rather than a dollar amount at risk, because a 13F option `value` is the value of
    the underlying and not the premium paid: Situational Awareness LP's $8.46B of put
    notional on 2026-03-31 is not $8.46B of capital. It says "disclosed" rather than
    "bearish" or "short", because a 13F carries long option positions only and reveals
    nothing about the stock, cash or offsetting contracts on the other side. And it is a
    separate clause rather than a line item inside the portfolio figure, because merging
    the two presented that fund's largest put names as things it owned.
    """
    bits = []
    for count, notional, word in (
        (snap.put_name_count, snap.put_notional_usd, "put"),
        (snap.call_name_count, snap.call_notional_usd, "call"),
    ):
        if count:
            plural = "position" if count == 1 else "positions"
            bits.append(
                f"{count} long {word} {plural} on {format_usd_short(notional)} of underlying"
            )
    if not bits:
        return ""
    return (
        "also disclosed "
        + " and ".join(bits)
        + "; 13F reports options at the value of the underlying, not premium paid, and "
        "does not disclose what offsets them"
    )


def render_market_stakes(
    stakes: list, on: date, since: date, limit: int = 10, examined: int = 0
) -> list[str]:
    """Everyone who crossed 5% this week, watchlist or not.

    The watchlist answers "what did the managers I picked do". This answers "who took a
    big position", which is the question that does not depend on having guessed the right
    names in advance. Ranked by percentage of the class, because that is what the filing
    obliges the holder to disclose and what makes a stake notable.
    """
    if not stakes:
        return [
            "BIG STAKES DISCLOSED THIS WEEK",
            f"  Nobody crossed 5% between {since.isoformat()} and {on.isoformat()}, or the",
            "  filings could not be read.",
            "",
        ]
    # The count is stated rather than implied. This header used to say "every 13D/G
    # filed market-wide", while the sweep behind it read the newest 25 of the week's
    # 1,123 filings, which made the ranking a ranking of the last two days.
    scope = (
        f"  All {examined:,} 13D/G filings EDGAR indexed between {since.isoformat()} "
        f"and {on.isoformat()},"
        if examined
        else f"  13D/G filings between {since.isoformat()} and {on.isoformat()},"
    )
    lines = [
        "BIG STAKES DISCLOSED THIS WEEK",
        scope,
        "  ranked by size of the stake. Anyone crossing 5% must file within days.",
        "",
    ]

    # Two lists rather than one ranking. Sorted purely by percentage, the top of this
    # section is control blocks: a founder holding all of a shell company, a sponsor
    # holding its own fund, a parent holding its subsidiary. Each is a real filing and
    # none is dropped, but none of them is a manager taking a position, which is the
    # thing the section exists to surface. The split is the filing's own reporting
    # person code, so nothing here is inferred from the name.
    def by_size(rows: list) -> list:
        return sorted(rows, key=lambda s: s.detail.percent or 0, reverse=True)

    funds = by_size([s for s in stakes if s.detail.is_institutional])
    others = by_size([s for s in stakes if not s.detail.is_institutional])

    def block(rows: list, limit_: int) -> list[str]:
        out: list[str] = []
        for s in rows[:limit_]:
            who = s.holders[0] if s.holders else "undisclosed filer"
            out.append(f"  {who} -- {s.detail.describe()}")
            out.append(f"      {s.form}, filed {s.filed.isoformat()}")
            if s.url:
                out.append(f"      {s.url}")
            out.append("")
        return out

    if funds:
        lines += block(funds, limit)
    if others:
        # Kept shorter: this list is context, not the point of the section.
        lines.append("  Other filers (individuals, parents and holders the filing does")
        lines.append("  not classify as an institution)")
        lines.append("")
        lines += block(others, max(3, limit // 2))
    return lines


def render_fund_moves(snapshots: list[FundSnapshot], on: date, since: date) -> list[str]:
    """Stake filings by tracked managers in the reporting window.

    Placed above the standing positions because it is the only part of the manager
    coverage that is news. A 13F says what was held in March; a 13D/A says what changed
    on Tuesday. When nothing was filed the section still appears and says so, for the
    same reason a degraded source is named rather than omitted: silence and absence look
    identical otherwise.
    """
    moved = [s for s in snapshots if s.error is None and s.recent_filings]
    lines = [
        "WHAT THE TRACKED MANAGERS FILED THIS WEEK",
        f"  Stake disclosures between {since.isoformat()} and {on.isoformat()}. These run",
        "  on a days-long clock, unlike the quarterly positions further down.",
        "",
    ]
    if not moved:
        lines.append("  No tracked manager filed a stake disclosure in this window.")
        lines.append("")
        return lines
    for snap in moved:
        lines.append(f"  {snap.name}")
        for f in snap.recent_filings:
            age = (on - f.filed).days
            when = "today" if age == 0 else f"{age} day{'s' if age != 1 else ''} ago"
            detail = f.detail.describe() if f.detail else ""
            headline = f"{f.form} filed {f.filed.isoformat()} ({when})"
            lines.append(f"      {headline}")
            if detail:
                lines.append(f"        {detail}")
            for trade in f.trades[:4]:
                lines.append(f"        {trade.describe()}")
            if f.url:
                lines.append(f"        {f.url}")
        lines.append("")
    return lines


def render_tracked_funds(snapshots: list[FundSnapshot], on: date) -> list[str]:
    """A roster, not a report.

    The quarterly holdings used to be spelled out here -- every position, every change
    against the previous snapshot. That was the wrong emphasis: a 13F describes a book as
    it stood on a date months ago, and what was asked for is what has happened since. The
    events live in the sections above; this is the reference row saying who is watched and
    where to read their last full filing.
    """
    usable = [s for s in snapshots if s.error is None and s.has_13f]
    if not usable:
        return []
    lines = [
        "TRACKED MANAGERS",
        "  The last full 13F for each, for reference. Stock positions as of the date",
        "  shown, which is a quarter end and not this week. Option positions, which a",
        "  13F reports at the notional value of the underlying, are stated separately.",
        "",
    ]
    for snap in usable:
        as_of = snap.period_end.isoformat() if snap.period_end else "date unknown"
        scaled = " [reported in thousands]" if snap.values_scaled_from_thousands else ""
        lines.append(
            f"  {snap.name} -- {format_usd_short(snap.total_value_usd)} in stock, "
            f"{snap.position_count} positions as of {as_of}{scaled}"
        )
        note = option_note(snap)
        if note:
            # Wrapped to the width the rest of this section already uses. A single
            # 190-character line renders as a wall in a plain-text mail client.
            lines.extend(
                textwrap.wrap(
                    note, width=72, initial_indent="      ", subsequent_indent="      "
                )
            )
        if snap.latest_13f_url:
            lines.append(f"      {snap.latest_13f_url}")
    lines.append("")
    return lines


def render_top_trades(trades: list, stakes: list, on: date, our_link=None) -> list[str]:
    """The five largest trades, pooled across every tracked manager.

    Two rankings, not one merged list: a Form 4 trade has a price and a dollar value;
    a 13D/G stake has neither, only a percent of class. Merging them would mean either
    inventing a dollar figure for the stake or dropping a real one for the trade, and
    this section's whole point is that every figure in it is copied, not computed by
    guessing.

    `our_link(accession)` is optional. When given, each row also carries a link into
    our own report page at the anchor for that filing; when not, the row still carries
    the SEC link and nothing crashes or half-renders a URL.
    """
    ranked = [t for t in trades if t.percent_of_portfolio is not None]
    unrankable = [t for t in trades if t.percent_of_portfolio is None]

    lines = [
        "THE LARGEST TRADES",
        "  Pooled across every tracked manager, ranked by trade size relative to the",
        "  manager's own disclosed 13F portfolio value -- not raw dollars, so a $5M",
        "  trade that is half of one manager's book outranks a $50M trade that is 1%",
        "  of another's. One row per filing (transaction lines sharing one accession",
        "  number are summed), one row per manager. Limited to open-market purchases",
        "  and sales; grants, tax withholding, and other non-discretionary",
        "  transactions are excluded rather than labelled as a decision to buy or sell.",
        "",
    ]
    if not ranked:
        lines.append("  No open-market trade by a tracked manager this window.")
        lines.append("")
    else:
        for t in ranked:
            value = format_usd_short(int(t.dollar_value))
            lines.append(
                f"  {t.manager} {t.direction} {t.shares:,} shares of "
                f"{t.issuer} ({t.symbol}) at ${t.price:,.2f} = {value} "
                f"({t.percent_of_portfolio:.1f}% of its 13F portfolio)"
            )
            lines.append(f"      {t.form} filed {t.filed.isoformat()}")
            if t.url:
                lines.append(f"      SEC filing: {t.url}")
            report = our_link(t.accession) if our_link else ""
            if report:
                lines.append(f"      Our report: {report}")
            lines.append("")

    if unrankable:
        lines.append("OTHER LARGE TRADES, NO 13F ON FILE TO RANK AGAINST")
        lines.append("  These managers have no disclosed 13F portfolio value (or it is zero),")
        lines.append("  so their trades cannot be ranked by percent of portfolio. Ranked by")
        lines.append("  raw dollar value instead, listed separately rather than mixed in.")
        lines.append("")
        for t in unrankable:
            value = format_usd_short(int(t.dollar_value))
            lines.append(
                f"  {t.manager} {t.direction} {t.shares:,} shares of "
                f"{t.issuer} ({t.symbol}) at ${t.price:,.2f} = {value}"
            )
            lines.append(f"      {t.form} filed {t.filed.isoformat()}")
            if t.url:
                lines.append(f"      SEC filing: {t.url}")
            report = our_link(t.accession) if our_link else ""
            if report:
                lines.append(f"      Our report: {report}")
            lines.append("")

    lines.append("LARGEST NEW OR AMENDED STAKES, BY PERCENT OF CLASS")
    lines.append("  13D/13G filings carry a stake size but no price, so no dollar value is")
    lines.append("  computed for them. An amendment does not by itself say whether the stake")
    lines.append("  grew or shrank -- only that one was filed -- so it is labelled 'amended'")
    lines.append("  rather than 'added to' or 'trimmed'.")
    lines.append("")
    if not stakes:
        lines.append("  No new or amended stake by a tracked manager this window.")
        lines.append("")
    else:
        for s in stakes:
            kind = "new position" if s.is_new else "amended stake"
            shares = f", {s.shares:,} shares" if s.shares else ""
            lines.append(f"  {s.manager} -- {kind}: {s.percent:g}% of {s.issuer}{shares}")
            lines.append(f"      {s.form} filed {s.filed.isoformat()}")
            if s.url:
                lines.append(f"      SEC filing: {s.url}")
            report = our_link(s.accession) if our_link else ""
            if report:
                lines.append(f"      Our report: {report}")
            lines.append("")
    return lines


def render_resale_watch(
    registrations: list, on: date, since: date, examined: int = 0
) -> list[str]:
    """Funds that have arranged to sell, weeks before any Form 4 says so."""
    lines = [
        "ARRANGING TO SELL",
        f"  Registration statements filed between {since.isoformat()} and "
        f"{on.isoformat()} naming a tracked fund as a selling securityholder.",
        "  Registering shares is the step before selling them; no 13F, 13D/G or Form 4",
        "  carries this, and it runs weeks ahead of all of them.",
        "",
    ]
    if not registrations:
        # The count is the point of this line. Without it, a sweep that silently read
        # nothing produces the same sentence as a week where nothing was registered.
        if examined:
            lines.append(
                f"  No tracked fund was named in the {examined:,} registration "
                "statements filed this window."
            )
        else:
            lines.append("  No tracked fund was named in a registration this window.")
        lines.append("")
        return lines
    for reg in registrations:
        lines.append(f"  {reg.whale_name} -- {reg.describe()}")
        lines.append(f"      {reg.form}, filed {reg.filed.isoformat()}")
        if reg.url:
            lines.append(f"      {reg.url}")
        lines.append("")
    return lines
