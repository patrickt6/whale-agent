"""The quarantine log: every figure the pipeline refused to publish, and why.

A blocked send is loud. A quarantined row is silent, and that is the problem this
module exists to solve. The checks in `enrichment/plausibility.py` were already
excluding bad rows before any of this was written, and the exclusions were visible only
as a count in a log line that nobody reads. That is enough to keep a false figure out of
one digest and not nearly enough to notice that a vendor changed its schema last
Tuesday, which is the thing you actually want to know.

So: near-misses are recorded, not just refusals to send.

The policy this implements, decided rather than inferred:

* **Drop the offending row, send the rest.** A quarantined figure is excluded from the
  digest, and the digest still ships. Holding an entire digest because one vendor row is
  malformed trades a small known loss for a large one.
* **Never a silent drop.** The digest carries a coverage note saying a row was withheld
  (`coverage_note` below), so the reader knows the picture is incomplete rather than
  quiet. A silent drop is indistinguishable from a source outage, which is the same
  reasoning that makes `plausibility.screen` mark rather than delete.
* **Quarantine is not "clamp and continue."** A clamped figure is still a false claim,
  only a less obvious one. There is no code path here that repairs a value.

`stage` is the enforcement point that caught it, and it is worth reading. A row caught
at `ingest` means the typed coercion did its job. The same row caught at `egress` means
every check upstream of the send missed it, and that is a defect in the pipeline, not
just in the vendor feed. `alerting_reasons` treats those two very differently.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from whale_agent.monitoring.jsonl_log import JsonlLog, monitoring_dir, utc_now_iso

log = logging.getLogger(__name__)

QUARANTINE_LOG_FILENAME = "quarantine.jsonl"

# Enforcement points, in pipeline order. Free-form strings are accepted so a new
# chokepoint does not need a change here, but these are the expected values.
STAGE_INGEST = "ingest"
STAGE_VALUATION = "valuation"
STAGE_PLAUSIBILITY = "plausibility"
STAGE_EGRESS = "egress"

# Not an enforcement point. A row never lands here because it was blocked -- it lands
# here because the layer-2 model verifier (`summarization/verifier.py`) raised a concern
# about a digest that already cleared every hard gate. `alerting_reasons` deliberately
# does not treat this stage as an egress block: an advisory finding is a prompt to look,
# not evidence that a check failed.
STAGE_ADVISORY = "advisory"

# More than this many quarantines in the watchdog window is a vendor story, not a
# handful of bad rows. Deliberately low: the normal rate is a small number of rows a
# week, so a jump to double digits is the schema-change signal the module exists for.
QUARANTINE_SPIKE_THRESHOLD = 10


@dataclass(frozen=True)
class QuarantineRecord:
    """One figure that was refused, with enough context to diagnose it later.

    `inputs` carries the raw fields that produced the figure. It is the difference
    between "a row was rejected" and "FMP put 40,000,000 in the price field again", and
    it is the reason the record is written at the point of rejection rather than
    reconstructed afterwards, when the vendor payload is long out of scope.
    """

    stage: str
    reason: str
    at: str = field(default_factory=utc_now_iso)
    event_id: str | None = None
    issuer_name: str | None = None
    ticker: str | None = None
    source: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        who = self.issuer_name or self.ticker or self.event_id or "unidentified row"
        return f"{self.stage}: {who}: {self.reason}"


@dataclass(frozen=True)
class QuarantineSummary:
    """What the quarantine log holds over some window."""

    total: int
    by_stage: dict[str, int]
    by_source: dict[str, int]
    by_reason: dict[str, int]
    window_hours: float

    def describe(self) -> str:
        if not self.total:
            return f"no quarantined figures in the last {self.window_hours:.0f}h"
        stages = ", ".join(f"{k}={v}" for k, v in sorted(self.by_stage.items()))
        return (
            f"{self.total} quarantined figure(s) in the last {self.window_hours:.0f}h "
            f"({stages})"
        )


class QuarantineLog:
    """Append-only record of refused figures."""

    def __init__(self, path: str | None = None) -> None:
        self._log = JsonlLog(path or (monitoring_dir() / QUARANTINE_LOG_FILENAME))

    @property
    def path(self) -> str:
        return str(self._log.path)

    def record(
        self,
        stage: str,
        reason: str,
        *,
        event_id: str | None = None,
        issuer_name: str | None = None,
        ticker: str | None = None,
        source: str | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> QuarantineRecord:
        """Record one quarantine. Never raises; the caller has already blocked the row."""
        rec = QuarantineRecord(
            stage=stage,
            reason=reason,
            event_id=event_id,
            issuer_name=issuer_name,
            ticker=ticker,
            source=source,
            inputs=dict(inputs or {}),
        )
        log.warning("QUARANTINED %s", rec.describe())
        self._log.append(asdict(rec))
        return rec

    def record_event(
        self, stage: str, event: Any, reasons: list[str] | tuple[str, ...]
    ) -> list[QuarantineRecord]:
        """Record every reason one event was refused, pulling its identity off the event.

        Typed loosely on purpose. This is called from the valuation and screening paths
        with a `NormalizedEvent`, and importing that model here would tie a monitoring
        module to the event schema for the sake of five attribute reads.
        """
        inputs = {
            key: getattr(event, key, None)
            for key in (
                "share_count",
                "price_used",
                "usd_value",
                "implied_usd_value",
                "shares_outstanding",
                "fx_rate_used",
                "native_currency",
            )
        }
        return [
            self.record(
                stage,
                reason,
                event_id=getattr(event, "event_id", None),
                issuer_name=getattr(event, "issuer_name", None),
                ticker=getattr(event, "ticker", None),
                source=getattr(event, "source", None),
                inputs=inputs,
            )
            for reason in reasons
        ]

    def records_since(self, cutoff: datetime) -> list[QuarantineRecord]:
        out: list[QuarantineRecord] = []
        for raw in self._log.read_since(cutoff):
            try:
                out.append(
                    QuarantineRecord(
                        stage=str(raw.get("stage") or "unknown"),
                        reason=str(raw.get("reason") or "unrecorded"),
                        at=str(raw.get("at") or ""),
                        event_id=raw.get("event_id"),
                        issuer_name=raw.get("issuer_name"),
                        ticker=raw.get("ticker"),
                        source=raw.get("source"),
                        inputs=raw.get("inputs") or {},
                    )
                )
            except (TypeError, ValueError):
                log.warning("Skipping malformed quarantine record")
        return out

    def summary(
        self, *, hours: float = 24.0, now: datetime | None = None
    ) -> QuarantineSummary:
        now = now or datetime.now(UTC)
        records = self.records_since(now - timedelta(hours=hours))
        return QuarantineSummary(
            total=len(records),
            by_stage=dict(Counter(r.stage for r in records)),
            by_source=dict(Counter(r.source or "unknown" for r in records)),
            by_reason=dict(Counter(_reason_shape(r.reason) for r in records)),
            window_hours=hours,
        )


_default_log: QuarantineLog | None = None


def default_log() -> QuarantineLog:
    """The process-wide quarantine log. Resolved lazily so tests can redirect it."""
    global _default_log
    if _default_log is None:
        _default_log = QuarantineLog()
    return _default_log


def set_default_log(log_instance: QuarantineLog | None) -> None:
    """Point the module-level log somewhere else. For tests and for the CLI."""
    global _default_log
    _default_log = log_instance


def record_quarantine(stage: str, reason: str, **kwargs: Any) -> QuarantineRecord:
    """Convenience entry point for callers that do not hold a log instance."""
    return default_log().record(stage, reason, **kwargs)


def _reason_shape(reason: str) -> str:
    """Collapse a reason to its shape so counts group.

    Reasons embed the offending figure, so counting them verbatim gives one bucket per
    row and tells you nothing. Truncating to the leading phrase is crude and it groups
    the cases that matter.
    """
    for marker in (" of ", " is ", " gives ", " exceeds "):
        idx = reason.find(marker)
        if idx > 0:
            return reason[:idx].strip()
    return reason[:48].strip()


def coverage_note(records: list[QuarantineRecord] | int) -> str | None:
    """The reader-facing sentence for rows dropped from a digest.

    Never a silent drop: if the digest is missing rows, it says so. The note states the
    count and not the figures, because the withheld numbers are exactly the ones we have
    decided are not trustworthy enough to print.
    """
    count = records if isinstance(records, int) else len(records)
    if count <= 0:
        return None
    noun = "filing" if count == 1 else "filings"
    return (
        f"{count} {noun} withheld from this digest: the disclosed figures failed a "
        "plausibility check and are being reviewed rather than published."
    )


def alerting_reasons(summary: QuarantineSummary) -> list[str]:
    """Problems in the quarantine record that deserve an operator alert.

    Two distinct conditions, and they mean different things:

    * A **spike** in quarantines is usually the vendor: a schema change, a unit change, a
      feed serving totals where it used to serve prices.
    * **Any** quarantine at the egress stage is us: the row reached the send function,
      which means every check upstream of it failed to catch the row. One is enough.

    Advisory findings (`STAGE_ADVISORY`) are excluded from the spike count on purpose.
    They are the layer-2 model's opinion on digests that already passed every hard gate,
    not evidence that a check failed, so counting them toward "suspect a vendor schema
    change" would train the operator to ignore the alert the first time the model is
    merely being its cautious self.
    """
    problems: list[str] = []
    egress = summary.by_stage.get(STAGE_EGRESS, 0)
    if egress:
        problems.append(
            f"EGRESS BLOCK  {egress} figure(s) were caught at the send gate, meaning "
            "every upstream check missed them. Investigate the pipeline, not just the feed."
        )
    blocking_total = summary.total - summary.by_stage.get(STAGE_ADVISORY, 0)
    if blocking_total > QUARANTINE_SPIKE_THRESHOLD:
        worst = max(summary.by_source.items(), key=lambda kv: kv[1], default=("unknown", 0))
        problems.append(
            f"QUARANTINE    {blocking_total} figures quarantined in the last "
            f"{summary.window_hours:.0f}h (threshold {QUARANTINE_SPIKE_THRESHOLD}); "
            f"heaviest source: {worst[0]} with {worst[1]}. Suspect a vendor schema change."
        )
    return problems
