#!/usr/bin/env bash
# The renders templates/generate.yaml must refuse. A guard that stops failing would let the
# root release emit two owners for one name, or copy another cluster's files into an artifact.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
expect_fail() {  # <name> <message substring> <values json>
  local out
  if out="$(helm template generators . -f - <<<"$3" 2>&1)"; then
    echo "  FAIL  $1: rendered" >&2; fail=1
  elif ! grep -qF -- "$2" <<<"$out"; then
    echo "  FAIL  $1: failed for another reason: $(tail -1 <<<"$out")" >&2; fail=1
  else
    echo "  ok    $1"
  fi
}

expect_fail "two groups generating one name" "two components generate this name" \
  '{"groups":[{"base":"tests/fixtures/apps","prefix":"app"},{"base":"tests/fixtures/apps","prefix":"app"}],"generate":{}}'
expect_fail "a base outside shared/" "is not a directory under shared/" \
  '{"groups":[{"base":"tests/fixtures-invalid","prefix":"app"}],"generate":{}}'
exit "$fail"
