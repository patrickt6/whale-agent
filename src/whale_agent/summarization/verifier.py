"""Layer-2 verifier: a fresh-context model reading the finished digest. ADVISORY ONLY.

Read this paragraph before wiring anything to this module. Nothing here is allowed to
decide that a figure is safe. The deterministic checks are the guarantee; this is a
second opinion that can raise a concern the code was not written to catch, and that is
the entire scope of its authority. Concretely:

* A `PASS` from this module means nothing on its own. It is not permission to send, and
  no function here returns a send decision. There is deliberately no `should_send`.
* It can never overturn a rejection. `advisory_notes` takes the deterministic outcome as
  an argument and, when the deterministic layer already refused, says so and discards
  any disagreement from the model. The refusal stands.
* Uncertainty resolves to `REVISE`, never `PASS`. Every failure mode of the call itself
  (no key, HTTP error, unparseable JSON, an unrecognised verdict string) also resolves
  to `REVISE`, because a verifier that returns `PASS` when it is broken is worse than no
  verifier: it manufactures confidence.

Why it exists at all, given that. Deterministic checks are rigid by construction. They
catch what someone anticipated, in the shape they anticipated it. A model reading the
rendered prose can notice that a sentence claims something the figures do not support,
or that an aggregate characterisation has no filing behind it, and neither of those is
expressible as a numeric bound. It is the same split the corpus draws: bounds and units
go to code, "does this read as misleading" goes to a model.

`run_advisory_pass` below is the wiring point: call it once per rendered digest and it
runs the verifier, turns the verdict into notes, and records those notes to the
quarantine log's advisory stage. It is still advisory all the way through -- it has no
return value a caller could read as permission and no path that raises.

Two structural rules it follows. It runs on a **fresh context** and sees only the
rendered digest, never the drafting conversation, because a reviewer holding the writer's
reasoning inherits the writer's blind spots. And the thing that wrote the prose is not
the thing that reviews it: independent review beats self-review, so this deliberately
does not reuse the prose call or its prompt.

The few-shot set below is doing real work. Vague instruction to "be conservative" does
not improve a model's precision, because the model is already miscalibrated; categorical
criteria plus worked examples do. The take-private example is the important one. Without
it the verifier learns "large number equals bad" and starts flagging legitimate near-100%
acquisitions, which is the exact false positive the deterministic layer already has a
pinned test against.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from whale_agent.config import Settings, get_settings
from whale_agent.errors import NotConfiguredError, SourceUnavailableError
from whale_agent.summarization.llm import LLMProvider, extract_json, get_provider

log = logging.getLogger(__name__)

__all__ = [
    "DigestVerdict",
    "VERIFIER_SYSTEM",
    "parse_verdict",
    "verify_digest",
    "advisory_notes",
    "verdict_payload",
    "run_advisory_pass",
]

PASS, REVISE, KILL = "PASS", "REVISE", "KILL"
_VERDICTS = (PASS, REVISE, KILL)

VERIFIER_SYSTEM = """\
You are an independent verifier. You did not write the text you are given and you have \
no stake in it shipping. Your job is to try to break it.

You default to skepticism. If you are uncertain, the verdict is REVISE. Never return \
PASS to resolve a doubt.

You are reviewing a finished market-awareness digest built from public filings. Every \
figure in it is supposed to trace to a disclosed field. Return one of three verdicts:

- KILL   a figure is impossible, or a claim is flatly contradicted by the text itself.
- REVISE something is unsourced, ambiguous, internally inconsistent, or you are unsure.
- PASS   every figure is plausible and every claim is attributable, with no doubt left.

Categorical criteria. Apply these, not a general feeling about the text:

1. A position cannot be worth more than the issuer's market capitalisation. A figure \
that exceeds it, or that approaches the size of a national economy, is KILL.
2. A share price outside roughly $0.01 to $10,000 is not a price. It is most likely a \
transaction total in the wrong field. KILL.
3. A high ownership fraction is NOT by itself a problem. Take-privates and tender \
offers really do reach 98% of a company. Flag an ownership fraction only when it \
exceeds 100% of shares outstanding, or when the dollar figure is inconsistent with the \
issuer's market capitalisation.
4. An aggregate characterisation ("institutional buying across the sector", "a wave of \
insider activity") with no underlying filings cited is unsourced. REVISE.
5. A predicted price, a recommendation, or a claim about future performance is out of \
scope for this product. REVISE.
6. Large is not the same as implausible. Multi-billion-dollar disclosures are ordinary \
in this dataset. Do not flag a figure for size alone.

Return STRICT JSON, no markdown fences, matching exactly this shape:

{
  "verdict": "PASS" | "REVISE" | "KILL",
  "unsourced_claims": ["<quoted phrase and why it cannot be traced>"],
  "implausible_figures": ["<quoted figure and why it cannot be true>"],
  "reasoning": "<two sentences>"
}

Worked examples:

<example>
<digest>Vanguard Group disclosed a $1,600,000,000,000,000 position in MetLife (MET).</digest>
<verdict>KILL</verdict>
<implausible_figures>["$1.6 quadrillion in MET: exceeds global equity market cap; the \
classic shares times price error where the vendor price field held a transaction \
total"]</implausible_figures>
<reasoning>No single position can exceed the issuer's market capitalisation, let alone \
global market cap. Absolute ceiling violation.</reasoning>
</example>

<example>
<digest>Berkshire Hathaway increased its Occidental (OXY) stake by 8.9M shares, a \
position now worth roughly $4.2B at Friday's close.</digest>
<verdict>PASS</verdict>
<implausible_figures>[]</implausible_figures>
<reasoning>About $47 per share implied, consistent with OXY's traded range. The position \
is material but well below the issuer's market cap, and both figures trace to source \
fields.</reasoning>
</example>

<example>
<digest>An investor group acquired 98.2% of Sculptor Capital, valuing the take-private \
at $720M.</digest>
<verdict>PASS</verdict>
<implausible_figures>[]</implausible_figures>
<reasoning>98.2% ownership is extreme but legitimate for an announced take-private, and \
the dollar figure is consistent with the issuer's market cap. Do NOT flag high ownership \
fractions on their own: flag fractions that exceed 100% of shares outstanding, or dollar \
values inconsistent with market cap.</reasoning>
</example>

<example>
<digest>Institutional buying detected across the healthcare sector this week.</digest>
<verdict>REVISE</verdict>
<unsourced_claims>["'across the healthcare sector': no per-filing evidence given, cannot \
be traced to a source field"]</unsourced_claims>
<reasoning>An aggregate characterisation with no underlying filings cited. Not \
necessarily false, but unsourced, and this verifier defaults to skepticism.</reasoning>
</example>
"""


@dataclass(frozen=True)
class DigestVerdict:
    """The verifier's opinion. An opinion, and nothing more.

    `advisory` is not configurable. It exists so that a caller reading this object in
    isolation, months from now, cannot mistake it for a clearance.
    """

    verdict: str
    unsourced_claims: tuple[str, ...] = ()
    implausible_figures: tuple[str, ...] = ()
    reasoning: str = ""
    error: str | None = None
    advisory: bool = field(default=True, init=False)

    @property
    def is_clean(self) -> bool:
        """True only when the model raised nothing. Still not permission to send."""
        return (
            self.verdict == PASS
            and not self.unsourced_claims
            and not self.implausible_figures
            and self.error is None
        )

    def describe(self) -> str:
        bits = [f"verdict={self.verdict}"]
        if self.implausible_figures:
            bits.append(f"implausible={list(self.implausible_figures)}")
        if self.unsourced_claims:
            bits.append(f"unsourced={list(self.unsourced_claims)}")
        if self.error:
            bits.append(f"error={self.error}")
        return " ".join(bits)


def _strings(value: Any) -> tuple[str, ...]:
    """Accept only a list of non-empty strings; anything else contributes nothing."""
    if not isinstance(value, list):
        return ()
    return tuple(
        " ".join(v.split()).strip() for v in value if isinstance(v, str) and v.strip()
    )


def parse_verdict(text: str) -> DigestVerdict:
    """Turn a raw model response into a verdict. Anything unusable becomes REVISE.

    Note which way the coercion runs. An unrecognised verdict string does not fall back
    to the model's own words and it does not fall back to PASS; it becomes REVISE, so a
    model that answers "looks fine to me" cannot be read as a clearance by accident.
    """
    try:
        data = extract_json(text)
    except ValueError as exc:
        return DigestVerdict(
            REVISE, reasoning="", error=f"unparseable verifier response: {exc}"
        )

    raw = data.get("verdict")
    verdict = raw.strip().upper() if isinstance(raw, str) else ""
    error = None
    if verdict not in _VERDICTS:
        error = f"unrecognised verdict {raw!r}, defaulting to REVISE"
        verdict = REVISE

    reasoning = data.get("reasoning")
    return DigestVerdict(
        verdict=verdict,
        unsourced_claims=_strings(data.get("unsourced_claims")),
        implausible_figures=_strings(data.get("implausible_figures")),
        reasoning=" ".join(reasoning.split()) if isinstance(reasoning, str) else "",
        error=error,
    )


def verify_digest(
    rendered_text: str,
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
) -> DigestVerdict:
    """Ask a fresh-context model to review the rendered digest. Never raises.

    Every failure resolves to REVISE with the cause in `error`. That direction is the
    whole safety property of this function: a broken verifier must degrade into "someone
    should look at this", never into silent approval.

    The digest text is passed as the user message and the criteria live in the system
    prompt, which is the architect-controlled layer. It matters here because the content
    being reviewed is partly model-written: instructions that travelled in the same
    channel as the reviewed text would be steerable by it.
    """
    if not rendered_text or not rendered_text.strip():
        return DigestVerdict(REVISE, reasoning="", error="nothing to verify")

    s = settings or get_settings()
    provider = provider or get_provider(s)
    if provider is None:
        return DigestVerdict(REVISE, reasoning="", error="no verifier provider configured")

    try:
        raw = provider.complete(VERIFIER_SYSTEM, rendered_text)
    except (NotConfiguredError, SourceUnavailableError) as exc:
        return DigestVerdict(REVISE, reasoning="", error=f"verifier unavailable: {exc}")
    except Exception as exc:  # a broken advisory check must not cost delivery
        log.warning("Verifier call failed unexpectedly: %s", exc)
        return DigestVerdict(REVISE, reasoning="", error=f"verifier failed: {exc}")

    verdict = parse_verdict(raw)
    log.info("Layer-2 verifier (advisory): %s", verdict.describe())
    return verdict


def advisory_notes(verdict: DigestVerdict, deterministic_ok: bool) -> list[str]:
    """Operator-facing lines derived from the verdict. Never a decision.

    `deterministic_ok` is required rather than optional, and it is required so that this
    function cannot be called without the caller stating what the code layer concluded.
    When the deterministic layer refused, the model's opinion is reported as disagreement
    and explicitly discarded: a rejection is not reviewable by a probabilistic check, and
    the one failure mode worth engineering against here is a future caller reading a PASS
    as a reason to send anyway.
    """
    if not deterministic_ok:
        return [
            "ADVISORY      deterministic checks rejected this digest. The layer-2 "
            f"verifier said {verdict.verdict}, which does not change that and was not "
            "consulted in the decision."
        ]

    notes: list[str] = []
    if verdict.error:
        notes.append(f"ADVISORY      verifier did not run cleanly: {verdict.error}")
    for figure in verdict.implausible_figures:
        notes.append(f"ADVISORY      verifier flagged a figure: {figure}")
    for claim in verdict.unsourced_claims:
        notes.append(f"ADVISORY      verifier flagged an unsourced claim: {claim}")
    if verdict.verdict in (REVISE, KILL) and not notes:
        notes.append(
            f"ADVISORY      verifier returned {verdict.verdict} without specifics: "
            f"{verdict.reasoning or 'no reasoning given'}"
        )
    return notes


def run_advisory_pass(
    rendered_text: str,
    *,
    deterministic_ok: bool = True,
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    quarantine_log: Any = None,
) -> DigestVerdict:
    """Run the layer-2 verifier over a rendered digest and record what it finds.

    This is the wiring point the module docstring promises exists: call it once per
    rendered artifact, after the hard provenance gate has already run, and pass its
    result along with `deterministic_ok`. It never raises, never blocks -- there is no
    return value or exception here that a caller could mistake for a send/no-send
    decision -- and it degrades to a genuine no-op when no LLM provider is configured,
    which keeps it off the cost and latency path by default.

    `quarantine_log` accepts a `monitoring.quarantine.QuarantineLog` (or anything with a
    compatible `.record` method). Passing one writes every advisory note to the
    `STAGE_ADVISORY` stage of the durable quarantine record, which is what makes a
    verifier finding visible to an operator without ever being visible as a rejection to
    the reader. Passing nothing (the default) still runs the check; it just goes
    nowhere durable, which is the right default for tests and for one-off calls that
    only want the verdict.

    As of this writing, no caller in `jobs/pipeline.py` invokes this yet -- that call
    site is outside this change's scope. A reader auditing the safety claim should treat
    the verifier as built and ready, not as running in production, until pipeline.py
    calls it.
    """
    verdict = verify_digest(rendered_text, settings=settings, provider=provider)
    notes = advisory_notes(verdict, deterministic_ok)
    if quarantine_log is not None:
        for note in notes:
            try:
                quarantine_log.record("advisory", note)
            except Exception as exc:  # an advisory record must never cost the caller
                log.warning("Failed to record advisory verifier note: %s", exc)
    return verdict


def verdict_payload(verdict: DigestVerdict) -> str:
    """Serialise a verdict for the quarantine or review log."""
    return json.dumps(
        {
            "verdict": verdict.verdict,
            "unsourced_claims": list(verdict.unsourced_claims),
            "implausible_figures": list(verdict.implausible_figures),
            "reasoning": verdict.reasoning,
            "error": verdict.error,
            "advisory": True,
        }
    )
