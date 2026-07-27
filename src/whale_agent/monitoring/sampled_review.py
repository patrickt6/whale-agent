"""Sampled review of what actually went out.

The quarantine log answers "what did we stop". This answers the harder question: of the
digests that passed every gate and reached the reader, are they right? No automated
check can answer that, because the failures worth finding here are the ones the checks
were not written for. The corpus is blunt about it: sampled review is a distinct pattern
from an approval gate and from escalation, it runs post hoc on completed outputs, and it
does not stop once the automation looks good.

Three design choices, each of which the obvious alternative gets wrong.

**Sampling is deterministic, not random.** Inclusion is a hash of the digest identity
against a rate, so a digest is either in the review queue or it is not, permanently. A
`random.random()` draw would resample on every rerun, which means the queue changes
under the reviewer and the same digest can be reviewed twice while its neighbour is
never reviewed at all.

**Sampling is stratified by segment.** Aggregate accuracy masks segment failure:
"99.4% of digests were fine" is worthless if every non-US filing in them is wrong. The
stratum key is jurisdiction plus digest kind, and each stratum gets its own guaranteed
minimum, so a segment that is 2% of volume is not sampled at 2% of the rate. That
minimum is the whole point of the module.

**Review is a queue, not a report.** `pending_reviews` returns what has not been looked
at. A record is only cleared by `mark_reviewed`, which writes the verdict back. An audit
mechanism whose output is a number nobody has to act on decays into a number nobody
reads.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from whale_agent.monitoring.jsonl_log import JsonlLog, monitoring_dir, utc_now_iso

log = logging.getLogger(__name__)

SENT_LOG_FILENAME = "sent_digests.jsonl"
REVIEW_LOG_FILENAME = "reviews.jsonl"

# Fraction of sends drawn for review. Low enough to stay a habit rather than a chore.
DEFAULT_SAMPLE_RATE = 0.10

# Every stratum yields at least this many reviews per window regardless of its share of
# volume. This is the line that stops a rare jurisdiction from going unexamined forever.
MIN_PER_STRATUM = 1

# How long a queued review may sit before the watchdog starts mentioning it. A review
# backlog is not an outage, so this is generous and it is only ever a nag.
REVIEW_STALE_DAYS = 14

VERDICTS = ("ok", "wrong", "unclear")


@dataclass(frozen=True)
class SentDigest:
    """One digest that reached a reader, recorded at the moment it was sent."""

    digest_id: str
    kind: str  # "daily" | "weekly" | "instant"
    at: str = field(default_factory=utc_now_iso)
    subject: str = ""
    event_count: int = 0
    total_usd: float | None = None
    max_usd: float | None = None
    jurisdictions: list[str] = field(default_factory=list)
    quarantined_count: int = 0
    sampled: bool = False

    @property
    def strata(self) -> list[str]:
        """The segments this digest belongs to, one per jurisdiction it covers.

        A digest is not a single segment. Reviewing it because Taiwan needs coverage is
        a legitimate reason to review it even though most of its rows are US, so it can
        be drawn on behalf of any jurisdiction it carries.
        """
        js = sorted({j for j in self.jurisdictions if j}) or ["unknown"]
        return [f"{self.kind}/{j}" for j in js]


@dataclass(frozen=True)
class Review:
    """A human's verdict on one sampled digest."""

    digest_id: str
    verdict: str  # one of VERDICTS
    reviewer: str = ""
    notes: str = ""
    at: str = field(default_factory=utc_now_iso)


def is_sampled(digest_id: str, rate: float = DEFAULT_SAMPLE_RATE) -> bool:
    """Deterministic inclusion: the same digest always gets the same answer.

    A hash rather than a counter, so it does not depend on how many digests came before
    and stays stable across reruns, machines, and a restored database.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha256(digest_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < rate


class ReviewLog:
    """The record of what went out and what a human made of it."""

    def __init__(self, sent_path: str | None = None, review_path: str | None = None) -> None:
        base = monitoring_dir()
        self._sent = JsonlLog(sent_path or (base / SENT_LOG_FILENAME))
        self._reviews = JsonlLog(review_path or (base / REVIEW_LOG_FILENAME))

    def record_sent(
        self,
        digest_id: str,
        kind: str,
        *,
        subject: str = "",
        event_count: int = 0,
        total_usd: float | None = None,
        max_usd: float | None = None,
        jurisdictions: list[str] | None = None,
        quarantined_count: int = 0,
        rate: float = DEFAULT_SAMPLE_RATE,
    ) -> SentDigest:
        """Record a delivered digest and decide, once and for all, whether it is sampled."""
        rec = SentDigest(
            digest_id=digest_id,
            kind=kind,
            subject=subject,
            event_count=event_count,
            total_usd=total_usd,
            max_usd=max_usd,
            jurisdictions=sorted({j for j in (jurisdictions or []) if j}),
            quarantined_count=quarantined_count,
            sampled=is_sampled(digest_id, rate),
        )
        self._sent.append(asdict(rec))
        return rec

    def record_review(
        self, digest_id: str, verdict: str, *, reviewer: str = "", notes: str = ""
    ) -> Review:
        """Write a human verdict. An unrecognised verdict is stored as "unclear".

        Coercing rather than raising is the right call for an audit record: a typo in a
        CLI argument should not lose the reviewer's notes, and "unclear" is the honest
        reading of a verdict nobody can interpret.
        """
        clean = verdict.strip().lower()
        if clean not in VERDICTS:
            log.warning("Unrecognised review verdict %r, recording as unclear", verdict)
            clean = "unclear"
        rec = Review(digest_id=digest_id, verdict=clean, reviewer=reviewer, notes=notes)
        self._reviews.append(asdict(rec))
        return rec

    def sent_since(self, cutoff: datetime) -> list[SentDigest]:
        out: list[SentDigest] = []
        for raw in self._sent.read_since(cutoff):
            digest_id = raw.get("digest_id")
            if not digest_id:
                continue
            out.append(
                SentDigest(
                    digest_id=str(digest_id),
                    kind=str(raw.get("kind") or "unknown"),
                    at=str(raw.get("at") or ""),
                    subject=str(raw.get("subject") or ""),
                    event_count=int(raw.get("event_count") or 0),
                    total_usd=raw.get("total_usd"),
                    max_usd=raw.get("max_usd"),
                    jurisdictions=list(raw.get("jurisdictions") or []),
                    quarantined_count=int(raw.get("quarantined_count") or 0),
                    sampled=bool(raw.get("sampled")),
                )
            )
        return out

    def reviewed_ids(self) -> set[str]:
        return {
            str(r.get("digest_id")) for r in self._reviews.read_all() if r.get("digest_id")
        }

    def pending_reviews(
        self,
        *,
        days: int = 30,
        now: datetime | None = None,
        min_per_stratum: int = MIN_PER_STRATUM,
    ) -> list[SentDigest]:
        """Digests that should be reviewed and have not been.

        Two ways in. A digest is in if it was drawn by the sampling rate, and a digest is
        in if its stratum would otherwise contribute nothing. The second clause is what
        makes this stratified rather than merely sampled: it is checked per segment, so
        the thin segments cannot be averaged away by the fat ones.
        """
        now = now or datetime.now(UTC)
        sent = self.sent_since(now - timedelta(days=days))
        done = self.reviewed_ids()

        selected: dict[str, SentDigest] = {d.digest_id: d for d in sent if d.sampled}

        by_stratum: dict[str, list[SentDigest]] = defaultdict(list)
        for d in sent:
            for name in d.strata:
                by_stratum[name].append(d)

        # Sorted by stratum name purely so the fill order is stable across runs; the name
        # itself is not needed inside the loop.
        for _stratum, digests in sorted(by_stratum.items()):
            covered = sum(1 for d in digests if d.digest_id in selected)
            if covered >= min_per_stratum:
                continue
            # Fill deterministically: the oldest unselected digests in the stratum, so
            # two runs over the same window produce the same queue.
            fill = [
                d
                for d in sorted(digests, key=lambda d: (d.at, d.digest_id))
                if d.digest_id not in selected
            ]
            for d in fill[: min_per_stratum - covered]:
                selected[d.digest_id] = d

        pending = [d for d in selected.values() if d.digest_id not in done]
        return sorted(pending, key=lambda d: (d.at, d.digest_id))

    def coverage(
        self, *, days: int = 30, now: datetime | None = None
    ) -> dict[str, dict[str, int]]:
        """Per-stratum sent / selected / reviewed counts.

        Reported by segment and never as a single overall number, because a single
        overall number is the exact shape of report that hides a segment failing wholesale.
        """
        now = now or datetime.now(UTC)
        sent = self.sent_since(now - timedelta(days=days))
        done = self.reviewed_ids()
        pending_ids = {d.digest_id for d in self.pending_reviews(days=days, now=now)}

        out: dict[str, dict[str, int]] = {}
        for d in sent:
            for stratum in d.strata:
                row = out.setdefault(stratum, {"sent": 0, "selected": 0, "reviewed": 0})
                row["sent"] += 1
                if d.digest_id in done:
                    row["selected"] += 1
                    row["reviewed"] += 1
                elif d.digest_id in pending_ids:
                    row["selected"] += 1
        return out


_default_review_log: ReviewLog | None = None


def default_review_log() -> ReviewLog:
    global _default_review_log
    if _default_review_log is None:
        _default_review_log = ReviewLog()
    return _default_review_log


def set_default_review_log(log_instance: ReviewLog | None) -> None:
    """Redirect the process-wide review log. For tests and for the CLI."""
    global _default_review_log
    _default_review_log = log_instance


def review_backlog_reasons(
    pending: list[SentDigest], *, now: datetime | None = None
) -> list[str]:
    """Watchdog lines for a review queue that is being ignored.

    Only overdue items are reported. A queue with three fresh items in it is the system
    working, and alerting on it would train the operator to skip the alert.
    """
    from whale_agent.monitoring.jsonl_log import parse_stamp

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=REVIEW_STALE_DAYS)
    overdue = [d for d in pending if (parse_stamp(d.at) or now) < cutoff]
    if not overdue:
        return []
    return [
        f"REVIEW QUEUE  {len(overdue)} sampled digest(s) unreviewed for more than "
        f"{REVIEW_STALE_DAYS} days, oldest {overdue[0].at}"
    ]
