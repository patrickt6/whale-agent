#!/usr/bin/env bash
# Does the core feature work? Ingest the bundled sample, normalize to USD, score
# against the threshold, and require a row in the daily table.
#
# A passing test suite does not answer this. The suite can be green while the digest
# renders an empty table, which is the failure that actually matters to a reader. So
# this asserts on the rendered output and exits non-zero when the row is missing.
#
# No network, no API keys, no email.
set -uo pipefail

PY="${PYTHON:-python3}"
OUT="$("$PY" -m whale_agent.jobs.digest_daily --demo 2>/dev/null)"
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo "FAIL: the digest exited $STATUS"
    exit 1
fi

# A scored row is "<something> $<amount>M|B in <issuer>" under a tier heading. Matching
# the money shape rather than a fixed issuer keeps this honest if the sample changes.
if ! grep -qE '\$[0-9][0-9,.]*[MB]' <<<"$OUT"; then
    echo "FAIL: no USD figure in the daily table"
    echo "$OUT"
    exit 1
fi

if ! grep -qE '^TIER [0-9]' <<<"$OUT"; then
    echo "FAIL: the table rendered no tier section"
    echo "$OUT"
    exit 1
fi

echo "$OUT"
echo
echo "PASS: sample ingest produced a thresholded USD row in the daily table"
