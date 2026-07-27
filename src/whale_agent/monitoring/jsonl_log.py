"""Append-only JSONL logs for things the operator has to be able to read later.

Two facilities in this package need durable, low-ceremony records that outlive a single
run: the quarantine log (what was blocked, and why) and the sent-digest log (what
actually went out, for sampled review). Both are append-only, both are read far less
often than they are written, and neither belongs in the event store: they are records
*about* the pipeline, not rows of market data. A file the operator can open, tail, and
grep is the right shape, and it keeps this work off the storage schema entirely.

Two properties are deliberate:

* **Writing never raises.** A monitoring write that takes down the run it was recording
  is worse than no monitoring. Failures are logged and swallowed. This is the narrow
  fail-open case: a bug in the recorder's own machinery, never a failed validation.
* **Reading tolerates a corrupt line.** A half-written final line after a kill signal
  must not blind the watchdog to the thousand good lines above it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """One spelling of "now", so every record sorts and compares the same way."""
    return datetime.now(UTC).isoformat()


def parse_stamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp into an aware UTC datetime, or None if it is unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def monitoring_dir() -> Path:
    """Where monitoring files live.

    `WHALE_MONITORING_DIR` wins when set. Otherwise the files sit beside the database,
    because that directory is already the one the operator backs up and the one the
    launcher `cd`s to, so the two records stay together.
    """
    override = os.environ.get("WHALE_MONITORING_DIR", "").strip()
    if override:
        return Path(override)
    db_path = os.environ.get("WHALE_DB_PATH", "whale.db").strip() or "whale.db"
    parent = Path(db_path).expanduser().parent
    return parent if str(parent) not in ("", ".") else Path(".")


class JsonlLog:
    """An append-only newline-delimited JSON file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, record: dict[str, Any]) -> bool:
        """Write one record. Returns whether it landed; never raises."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            log.warning("Could not append to %s: %s", self.path, exc)
            return False

    def read_all(self) -> list[dict[str, Any]]:
        """Every readable record, oldest first. A missing file is an empty log."""
        return list(self._iter())

    def read_since(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Records stamped at or after `cutoff`.

        Records with no usable timestamp are kept rather than dropped: an unstamped
        record is a defect worth seeing, and silently discarding it would hide it.
        Timestamps are parsed rather than string-compared, because two ISO strings in
        different offsets do not sort in time order.
        """
        out = []
        for rec in self._iter():
            at = parse_stamp(rec.get("at"))
            if at is None or at >= cutoff:
                out.append(rec)
        return out

    def _iter(self) -> Iterator[dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        log.warning("Skipping unparseable line in %s", self.path)
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Could not read %s: %s", self.path, exc)
            return
