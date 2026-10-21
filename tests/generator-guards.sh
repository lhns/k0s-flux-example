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
expect_fail "a reserved suffix in a component name" "a component name must not end in" \
  '{"groups":[{"base":"tests/fixtures-invalid/suffix","prefix":"app"}],"generate":{}}'
expect_fail "an empty group prefix" "generate needs a group prefix" \
  '{"groups":[{"base":"tests/fixtures/apps","prefix":""}],"generate":{}}'
# These three live in kustomizations.yaml and _substitution.tpl, which render only without `generate`.
expect_fail "a control namespace over 63 characters" "is longer than 63 characters" \
  '{"groups":[{"base":"tests/fixtures-invalid/longname","prefix":"app"}]}'
expect_fail "a substitution source in flux-system" "is in flux-system, but its component lives in" \
  '{"groups":[{"base":"tests/fixtures-invalid/flux-system-secret","prefix":"app"}]}'
expect_fail "an artifact writing its ref into flux-system" "writes its ref into flux-system" \
  '{"groups":[{"base":"tests/fixtures-invalid/flux-system-ref","prefix":"app"}]}'
# README "Control namespaces and risky changes" relies on this: a typo must not leave prune on.
expect_fail "pruneDisabled naming nothing generated" "is not a generated Kustomization" \
  '{"groups":[{"base":"tests/fixtures/apps","prefix":"app"}],"generate":{"pruneDisabled":["app-nope"]}}'
exit "$fail"
