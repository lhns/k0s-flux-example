#!/usr/bin/env bash
# arr component tests: the sonarr-queue operator tool (tools/, not deployed). Run from the
# repo root by scripts/run-tests.sh, with the component directory as $1. No cluster access.
set -euo pipefail
app="${1:-kube-cluster/apps/arr}"
py=python3; command -v python3 >/dev/null 2>&1 || py=python

"$py" -m unittest discover -s "$app/tests" -p 'test_*.py' -v
