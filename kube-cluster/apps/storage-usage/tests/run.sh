#!/usr/bin/env bash
# storage-usage component tests: the exporter's walk, attribution and rendering, against fakes.
# Run from the repo root by scripts/run-tests.sh, with the component directory as $1.
set -euo pipefail
app="${1:-kube-cluster/apps/storage-usage}"
py=python3; command -v python3 >/dev/null 2>&1 || py=python

"$py" -m unittest discover -s "$app/tests" -p 'test_*.py' -v
