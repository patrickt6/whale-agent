"""SQLite storage with idempotent upserts keyed on the stable event key.

Every adapter run is safe to re-run: writing an event with a key already present
updates the row rather than duplicating it ("idempotent ingestion").
Amendment history is retained -- superseded originals stay in the table with
amended=1 rather than being deleted.

**Reads are screened; writes are not.** An implausible row is stored exactly as the
vendor sent it, because a silent drop at write time is indistinguishable from a source
outage and the rejection count is the early warning that a vendor changed something.
But it does not come back out of `events_since()`, because the alternative -- every
reader remembering to call `screen()` -- is what put a $1.1 quadrillion figure into a
client-facing table. A caller that genuinely needs the raw rows has to type
`events_since_UNSCREENED`, and it logs who asked.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime

from whale_agent.enrichment.plausibility import screen
from whale_agent.models.event import NormalizedEvent

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    payload  TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    amended INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_disclosure ON events(disclosure_date);
CREATE TABLE IF NOT EXISTS seen_filers (
    filer_key TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);
-- One row per (filer, event) we have ever recorded. `seen_filers` answers "have we met
-- them", which is a single bit; ranking needs the shape of the relationship -- how often,
-- how recently, how big their positions usually are. Kept as its own narrow table rather
-- than derived from `events.payload` because every digest run reads it for every filer,
-- and JSON-parsing the whole event table to compute a median is not that.
CREATE TABLE IF NOT EXISTS filer_observations (
    filer_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    usd_value REAL,
    PRIMARY KEY (filer_key, event_id)
);
CREATE INDEX IF NOT EXISTS idx_filer_obs_key ON filer_observations(filer_key);
-- Per-adapter run history, one row per source. Powers the freshness watchdog: a
-- source that stops reporting is indistinguishable from a quiet day unless we record
-- that it ran at all.
CREATE TABLE IF NOT EXISTS source_runs (
    source_name TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_event_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
-- Delivery log, one row per digest sent. The dead-man's switch reads this: the absence
-- of a row is exactly the condition it exists to detect.
CREATE TABLE IF NOT EXISTS digest_runs (
    digest_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT,
    PRIMARY KEY (digest_date, kind, channel)
);
"""


def filer_key_for(event: NormalizedEvent) -> str:
    """The identity a filer is tracked under across runs.

    Defined here because storage owns the key format its tables are indexed on; anything
    that wants to look a filer up must agree with what `record_filers` wrote.
    """
    return (event.filer_id or event.filer_name).strip().lower()


class Store:
    """Thin SQLite wrapper. Pass ":memory:" for tests."""

    def __init__(self, path: str = "whale.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- events -----------------------------------------------------------------
    def upsert(self, event: NormalizedEvent) -> None:
        payload = event.model_dump(mode="json")
        self.conn.execute(
            """
            INSERT INTO events (event_id, payload, disclosure_date, amended)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                payload = excluded.payload,
                disclosure_date = excluded.disclosure_date,
                amended = excluded.amended
            """,
            (
                event.event_id,
                json.dumps(payload),
                event.disclosure_date.isoformat(),
                int(event.amended),
            ),
        )
        self.conn.commit()

    def upsert_many(self, events: Iterable[NormalizedEvent]) -> int:
        n = 0
        for ev in events:
            self.upsert(ev)
            n += 1
        return n

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def get(self, event_id: str) -> NormalizedEvent | None:
        row = self.conn.execute(
            "SELECT payload FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return NormalizedEvent.model_validate(json.loads(row["payload"]))

    def _events_since_rows(
        self, since: date, include_amended: bool = False
    ) -> list[NormalizedEvent]:
        q = "SELECT payload FROM events WHERE disclosure_date >= ?"
        if not include_amended:
            q += " AND amended = 0"
        rows = self.conn.execute(q, (since.isoformat(),)).fetchall()
        return [NormalizedEvent.model_validate(json.loads(r["payload"])) for r in rows]

    def events_since(
        self, since: date, include_amended: bool = False
    ) -> list[NormalizedEvent]:
        """Believable events disclosed on or after `since`. Safe for anything client-facing.

        Screened, so a reader gets the guarantee without having to know it exists. The
        rejected rows are still in the table and still counted in the log line; they are
        simply not handed to something that might render them.
        """
        kept, rejected = screen(self._events_since_rows(since, include_amended))
        if rejected:
            log.warning(
                "Storage read withheld %d implausible row(s); first: %s / %s: %s",
                len(rejected),
                rejected[0][0].filer_name,
                rejected[0][0].issuer_name,
                "; ".join(rejected[0][1].reasons),
            )
        return kept

    def events_since_UNSCREENED(  # noqa: N802 - the shouting is the point
        self, since: date, include_amended: bool = False
    ) -> list[NormalizedEvent]:
        """Raw rows, including quarantined ones. Debugging and re-valuation only.

        NEVER route the result into rendering or delivery. The name is deliberately
        awkward: it has to be typed out, it cannot be reached by accident, and it says
        what it is at the call site rather than in a docstring nobody opens.
        """
        caller = sys._getframe(1)
        log.warning(
            "UNSCREENED storage read from %s:%d",
            caller.f_code.co_filename,
            caller.f_lineno,
        )
        return self._events_since_rows(since, include_amended)

    # -- first-time filer tracking ---------------------------------------------
    def seen_filer_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT filer_key FROM seen_filers").fetchall()
        return {r["filer_key"] for r in rows}

    def record_filers(self, events: Iterable[NormalizedEvent]) -> None:
        """Record that we have met these filers, and what we saw them do.

        Both writes happen here because they must not drift apart: a filer present in
        `seen_filers` but absent from `filer_observations` would read as "known but has
        never done anything", which is a state the ranking has no sensible answer for.
        """
        for ev in events:
            key = filer_key_for(ev)
            self.conn.execute(
                "INSERT OR IGNORE INTO seen_filers (filer_key, first_seen) VALUES (?, ?)",
                (key, ev.disclosure_date.isoformat()),
            )
            self.conn.execute(
                """
                INSERT INTO filer_observations
                    (filer_key, event_id, disclosure_date, usd_value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filer_key, event_id) DO UPDATE SET
                    disclosure_date = excluded.disclosure_date,
                    usd_value = excluded.usd_value
                """,
                (key, ev.event_id, ev.disclosure_date.isoformat(), ev.effective_usd),
            )
        self.conn.commit()

    # -- filer history (the raw material for the unfamiliarity terms) -----------
    def filer_observation_count(self) -> int:
        """Total filer observations on record, across all filers."""
        return self.conn.execute("SELECT COUNT(*) FROM filer_observations").fetchone()[0]

    def filer_history_span_days(self) -> int:
        """Calendar days between the oldest and newest observation, 0 if fewer than two.

        A count alone can be satisfied by one busy afternoon; "we have never seen this
        filer" only means something once the window is long enough that we would have.
        """
        row = self.conn.execute(
            "SELECT MIN(disclosure_date), MAX(disclosure_date) FROM filer_observations"
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return 0
        return max(0, (date.fromisoformat(row[1]) - date.fromisoformat(row[0])).days)

    def filer_observations(self, filer_key: str, before: date | None = None) -> list[dict]:
        """Every observation for one filer, oldest first.

        `before` excludes observations on or after a date, which is how an event is
        scored against the history that preceded it rather than against itself.
        """
        q = "SELECT disclosure_date, usd_value FROM filer_observations WHERE filer_key = ?"
        params: list[object] = [filer_key]
        if before is not None:
            q += " AND disclosure_date < ?"
            params.append(before.isoformat())
        q += " ORDER BY disclosure_date ASC"
        rows = self.conn.execute(q, params).fetchall()
        return [
            {
                "disclosure_date": date.fromisoformat(r["disclosure_date"]),
                "usd_value": r["usd_value"],
            }
            for r in rows
        ]

    # -- source freshness -------------------------------------------------------
    def record_source_run(
        self,
        source_name: str,
        *,
        ok: bool,
        event_count: int = 0,
        error: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record one adapter attempt. A failed attempt updates `last_attempt_at` only,
        so `last_success_at` keeps measuring true staleness rather than liveness."""
        stamp = (at or datetime.now(UTC)).isoformat()
        self.conn.execute(
            """
            INSERT INTO source_runs
                (source_name, last_attempt_at, last_success_at, last_event_count, last_error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = COALESCE(
                    excluded.last_success_at, source_runs.last_success_at
                ),
                last_event_count = excluded.last_event_count,
                last_error = excluded.last_error
            """,
            (source_name, stamp, stamp if ok else None, event_count, error),
        )
        self.conn.commit()

    def source_runs(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM source_runs").fetchall()
        return {r["source_name"]: dict(r) for r in rows}

    # -- digest delivery log ----------------------------------------------------
    def record_digest_run(
        self,
        digest_date: date,
        kind: str,
        channel: str,
        ok: bool,
        detail: str = "",
        at: datetime | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO digest_runs (digest_date, kind, sent_at, channel, ok, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(digest_date, kind, channel) DO UPDATE SET
                sent_at = excluded.sent_at, ok = excluded.ok, detail = excluded.detail
            """,
            (
                digest_date.isoformat(),
                kind,
                (at or datetime.now(UTC)).isoformat(),
                channel,
                int(ok),
                detail,
            ),
        )
        self.conn.commit()

    def digest_sent(self, digest_date: date, kind: str = "daily") -> bool:
        """True if any channel successfully delivered this digest."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM digest_runs WHERE digest_date = ? AND kind = ? AND ok = 1",
            (digest_date.isoformat(), kind),
        ).fetchone()
        return bool(row[0])
