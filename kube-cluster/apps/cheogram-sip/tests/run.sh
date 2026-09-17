#!/usr/bin/env bash
# cheogram-sip component tests. Run from the repo root by scripts/run-tests.sh, with the
# component directory as $1. No cluster access -- see kube-cluster/README.md.
set -euo pipefail
app="${1:-kube-cluster/apps/cheogram-sip}"
py=python3; command -v python3 >/dev/null 2>&1 || py=python

# The dialplan suite loads the RENDERED routing table rather than a fixture, so the two
# cannot drift. trap on EXIT, not after the last command: `set -e` must still clean up.
routing="$(mktemp)"
trap 'rm -f "$routing"' EXIT

echo "-- sip routing chart: schema enforcement, and the invariants no schema expresses"
"$py" "$app/tests/check-routing.py" --write-lua "$routing"

echo "-- asterisk dialplan: lua 5.1 syntax + unit tests"
# extensions.lua runs under Asterisk's embedded Lua 5.1 and a syntax error there takes down
# every inbound and outbound call, so check it with the 5.1 compiler, not any lua on PATH.
luac5.1 -p "$app/extensions.lua"
# $1 is the component dir, which the suite dofile()s extensions.lua from; $2 the rendered
# table, so the suite exercises the live rules rather than a fixture that can drift.
lua5.1 "$app/tests/dialplan_test.lua" "$app" "$routing"
