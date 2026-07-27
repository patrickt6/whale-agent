"""The profile filter stage: a deterministic, pre-scoring narrowing of collected events.

Deliberately separate from `scoring/score.py`. A profile changes *which* events are even
candidates for the digest -- one jurisdiction, one filer type, one event type -- not how
the survivors are weighed against each other. Keeping the two apart is what lets the
default profile guarantee byte-identical output to plain `Settings.from_env()`: every
filter list defaults to empty, `apply_filters` is then a no-op, and `test_scoring.py`
never has to know this module exists.

Runs once, in `jobs/pipeline.collect_events`, right after every source has been
collected and before dedup. That single call site means the daily digest, the instant
alerts, and the weekly report (which reads back from storage) all see the same narrowed
slice, since it is what actually gets persisted.
"""

from __future__ import annotations

from whale_agent.config import Settings
from whale_agent.enrichment.roles import role_for
from whale_agent.models.event import NormalizedEvent


def _role_matches(event: NormalizedEvent, tokens: set[str]) -> bool:
    """Whether any of `tokens` appears in the filer's role.

    Uses `role_for`, not the raw `filer_role` field, because most rows only get a role
    at enrichment time -- SEC Form 4 stores it as booleans plus a title in `raw_payload`
    and `role_for` is what turns that into text, exactly the same way the render layer
    already does. Filtering on the raw field would silently drop every Form 4 row,
    since it starts out None.

    Substring match, not equality: a real role reads as "Director, Chief Executive
    Officer", so a profile filtering on "director" or "officer" needs to match inside
    that compound string rather than requiring the whole thing verbatim.
    """
    role = (role_for(event) or "").lower()
    return any(token in role for token in tokens)


def apply_filters(events: list[NormalizedEvent], settings: Settings) -> list[NormalizedEvent]:
    """Keep only events matching every filter the active profile set. Order-preserving.

    Each filter is independently optional (an empty list means "no restriction"), and
    they compose with AND: a profile naming both `jurisdictions` and `event_types`
    requires both to match, not either.
    """
    out = events

    if settings.filter_jurisdictions:
        allowed = {j.strip().upper() for j in settings.filter_jurisdictions}
        out = [e for e in out if e.jurisdiction.value.upper() in allowed]

    if settings.filter_event_types:
        allowed_types = {t.strip().lower() for t in settings.filter_event_types}
        out = [e for e in out if e.transaction_type.value.lower() in allowed_types]

    if settings.filter_filer_roles:
        allowed_roles = {r.strip().lower() for r in settings.filter_filer_roles}
        out = [e for e in out if _role_matches(e, allowed_roles)]

    if settings.filter_exclude_roles:
        excluded_roles = {r.strip().lower() for r in settings.filter_exclude_roles}
        out = [e for e in out if not _role_matches(e, excluded_roles)]

    return out
