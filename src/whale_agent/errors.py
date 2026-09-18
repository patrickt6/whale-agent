"""Shared exception types.

`NotConfiguredError` is the load-bearing one: an adapter or provider whose API key is
absent raises it, and `jobs/pipeline.py` treats it as "this source is off today" rather
than a crash. That is what makes 's "one broken adapter must not block the
digest" true in practice, and it is also how FMP/Quiver stay optional until the operator buys
them.

Every exception below carries `category` and `retryable` class attributes rather than
leaving a caller to guess from the exception's class name or its message. The categories
are the ones a source-collection loop actually has to act on differently:

* `credential`  a key is missing or rejected. Retrying now changes nothing; it needs a
  human to set a secret.
* `transient`   a timeout, a connection reset, a 5xx. Retrying later is likely to work.
* `upstream`    the vendor answered but the answer says something is wrong on their end
  (a 4xx that is not ours, a documented outage). Not necessarily retryable.
* `validation`  we got a payload and it failed our own shape/range checks.
* `parse`       the payload was not the format we expected at all (bad JSON, wrong
  schema) -- distinct from `validation` because a parse failure means we cannot even
  read the fields well enough to validate them.

`SourceFailure` is the structured record a collection loop should build when a source
raises one of these, instead of immediately flattening the failure into a sentence. The
anti-pattern this exists to kill: a source that raised `SourceUnavailableError` and a
source that legitimately found nothing today must not both collapse into "returned zero
events, no note attached" -- one of them is missing data because something broke, the
other is missing data because nothing happened, and a reader of the digest is owed the
difference. Building a `SourceFailure` for the first case and nothing at all for the
second is what keeps them distinguishable; `describe()` renders the reader-facing
sentence afterwards, as a view onto the structure rather than as the structure itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCategory(str, Enum):
    """Why a source failed, in the shape a caller needs to decide what to do next."""

    TRANSIENT = "transient"
    VALIDATION = "validation"
    CREDENTIAL = "credential"
    UPSTREAM = "upstream"
    PARSE = "parse"


class WhaleAgentError(Exception):
    """Base class for every error this package raises deliberately.

    `category` and `retryable` are class attributes rather than constructor arguments:
    the category is a property of *what kind of error this is*, not of one particular
    occurrence, so it belongs on the class the way a docstring does. A caller that wants
    per-instance detail reads the exception's message; a caller that wants to decide
    whether to retry reads `.retryable` without needing to know the category taxonomy at
    all.
    """

    category: ErrorCategory = ErrorCategory.UPSTREAM
    retryable: bool = False


class NotConfiguredError(WhaleAgentError):
    """A required credential or setting is missing.

    Callers should degrade gracefully: skip the source, note it in the digest's
    coverage line, and continue.
    """

    category = ErrorCategory.CREDENTIAL
    retryable = False


class SourceUnavailableError(WhaleAgentError):
    """An external source failed after retries (HTTP error, timeout, bad payload)."""

    category = ErrorCategory.TRANSIENT
    retryable = True


class ProvenanceError(WhaleAgentError):
    """Generated prose contained a number not traceable to the structured events."""

    category = ErrorCategory.VALIDATION
    retryable = False


class SourceParseError(WhaleAgentError):
    """A source answered, but its payload was not the shape an adapter can read at all.

    Distinct from `ProvenanceError`, which is about our own generated text, and from a
    validation failure on a value we did understand -- this is for when the response
    could not be parsed into fields in the first place (malformed JSON, an unexpected
    top-level type, a schema version we do not handle).
    """

    category = ErrorCategory.PARSE
    retryable = False


# Reader-facing phrasing per category, held stable because these strings already appear
# verbatim in shipped digests via `jobs/pipeline.py`'s own (pre-existing) coverage-note
# construction. `SourceFailure.describe()` below reproduces them from structure so a
# caller that adopts `SourceFailure` does not have to change what the reader sees.
_CATEGORY_PHRASING: dict[ErrorCategory, str] = {
    ErrorCategory.CREDENTIAL: "not included (not configured)",
    ErrorCategory.TRANSIENT: "unavailable today",
    ErrorCategory.UPSTREAM: "unavailable today",
    ErrorCategory.VALIDATION: "unavailable today",
    ErrorCategory.PARSE: "unavailable today",
}


@dataclass(frozen=True)
class SourceFailure:
    """One source's failure to report, structured rather than pre-flattened to a string.

    Building one of these is the alternative to the anti-pattern described in the module
    docstring: a source that fails is a `SourceFailure`, and a source that succeeds with
    zero rows is not one at all. The distinction lives in whether the object exists, not
    in a sentence a reader has to parse to recover it.
    """

    source: str
    category: ErrorCategory
    retryable: bool
    detail: str = ""

    @classmethod
    def from_exception(cls, source: str, exc: WhaleAgentError) -> SourceFailure:
        """Build a `SourceFailure` from whichever `WhaleAgentError` a source raised.

        Reads `category`/`retryable` off the exception's class rather than requiring the
        caller to classify it again at the call site, which is the whole point of
        putting those attributes on the exception in the first place.
        """
        return cls(
            source=source,
            category=exc.category,
            retryable=exc.retryable,
            detail=str(exc),
        )

    def describe(self) -> str:
        """The reader-facing sentence, rendered from the structure rather than stored as it.

        Kept close to the existing wording (`"{source} not included (not configured)"`,
        `"{source} unavailable today"`) so a caller migrating to this type does not churn
        the coverage line a reader has already seen a hundred times.
        """
        phrase = _CATEGORY_PHRASING.get(self.category, "unavailable today")
        return f"{self.source} {phrase}"
