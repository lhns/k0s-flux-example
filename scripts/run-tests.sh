#!/usr/bin/env bash
# Per-component tests: run every kube-cluster/{apps,infra,rbac}/*/tests/run.sh.
# Called by scripts/validate.sh, which stays the single entry point. The convention
# (opt-in, no cluster access, tools manifest) is documented in kube-cluster/README.md.
#
# A missing tool is a FAILURE, not a silent pass: a check that quietly does not run
# reports green. ALLOW_SKIP=1 downgrades it to a reported skip on a workstation; CI
# pins it to 0 so it can never skip.
set -uo pipefail
cd "$(dirname "$0")/.."

allow_skip="${ALLOW_SKIP:-0}"

names=(); states=(); reasons=()
pass=0; fail=0; skip=0

record() { names+=("$1"); states+=("$2"); reasons+=("${3:-}"); }

for run in kube-cluster/apps/*/tests/run.sh \
           kube-cluster/infra/*/tests/run.sh \
           kube-cluster/rbac/*/tests/run.sh; do
  [ -f "$run" ] || continue
  dir="${run%/tests/run.sh}"
  name="${dir#kube-cluster/}"

  # Tools manifest: one required executable per line, optionally followed by the apt
  # package that provides it (see kube-cluster/README.md). Checked here rather than in
  # run.sh so the outcome is a reported SKIP/FAIL instead of an opaque "not found".
  missing=""
  tools="$dir/tests/tools"
  if [ -f "$tools" ]; then
    while read -r exe _rest; do
      case "$exe" in ''|'#'*) continue ;; esac
      command -v "$exe" >/dev/null 2>&1 || missing="${missing:+$missing }$exe"
    done < "$tools"
  fi

  if [ -n "$missing" ]; then
    if [ "$allow_skip" = 1 ]; then
      echo "== $name: SKIP (missing: $missing) =="
      record "$name" SKIP "missing tool(s): $missing"
      skip=$((skip + 1))
    else
      echo "== $name: FAIL (missing: $missing) ==" >&2
      echo "   install it, or re-run with ALLOW_SKIP=1 to skip on a workstation" >&2
      record "$name" FAIL "missing tool(s): $missing"
      fail=$((fail + 1))
    fi
    continue
  fi

  echo "== $name =="
  # Invoked from the repo root with the component directory as $1, so a test can find
  # both its own files and repo-wide fixtures without guessing where it was started.
  if bash "$run" "$dir"; then
    record "$name" PASS
    pass=$((pass + 1))
  else
    echo "== $name: FAILED ==" >&2
    record "$name" FAIL "tests/run.sh exited non-zero"
    fail=$((fail + 1))
  fi
  echo
done

# The summary is the point of the runner: a skip buried mid-scroll in a wall of OK is
# how a check stops running without anyone noticing. Components with no tests/ are not
# listed -- per-component tests are opt-in, and absence is the normal case.
echo "== component test summary =="
if [ "${#names[@]}" -eq 0 ]; then
  echo "  (no component tests found)"
else
  for i in "${!names[@]}"; do
    line="$(printf '  %-4s  %s' "${states[$i]}" "${names[$i]}")"
    echo "${line}${reasons[$i]:+  -- ${reasons[$i]}}"
  done
fi
echo "  ${pass} passed, ${fail} failed, ${skip} skipped"

[ "$fail" -eq 0 ] || exit 1
[ "$skip" -eq 0 ] || echo "  note: skipped tests did NOT run; CI pins ALLOW_SKIP=0 so it cannot skip."
exit 0
