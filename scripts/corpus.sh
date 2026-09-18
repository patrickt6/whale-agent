#!/usr/bin/env bash
# The corpus is the only mutable state this system has, and it does not live in git:
# whale.db is 40MB and gitignored, and committing a fresh 40MB blob per run would pass
# GitHub's repo limit within months. It lives as an asset on a private release instead.
#
# Usage: scripts/corpus.sh {init|download|upload}
set -euo pipefail

TAG="${CORPUS_RELEASE_TAG:-corpus}"
ASSET="whale.db"

case "${1:-}" in
  init)
    # Idempotent: safe to re-run, does nothing if the release already exists.
    if gh release view "$TAG" >/dev/null 2>&1; then
      echo "release '$TAG' already exists"
    else
      gh release create "$TAG" \
        --title "Corpus" \
        --notes "Accumulating whale.db, written by the daily ingest workflow. Do not delete."
      echo "created release '$TAG'"
    fi
    ;;
  download)
    # No fallback to a fresh database, on purpose. An empty corpus does not produce an
    # obviously broken report, it produces a confident report about nothing, which is
    # the one failure this system must never ship. Aborting is the safe outcome.
    gh release download "$TAG" --pattern "$ASSET" --output "$ASSET" --clobber
    echo "downloaded $ASSET ($(du -h "$ASSET" | cut -f1))"
    ;;
  upload)
    gh release upload "$TAG" "$ASSET" --clobber
    echo "uploaded $ASSET ($(du -h "$ASSET" | cut -f1))"
    ;;
  *)
    echo "usage: $0 {init|download|upload}" >&2
    exit 2
    ;;
esac
