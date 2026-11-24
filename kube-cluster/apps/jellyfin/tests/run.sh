#!/usr/bin/env bash
# jellyfin component tests: its Python scripts against offline fixtures (test_*.py).
# Run from the repo root by scripts/run-tests.sh, with the component directory as $1.
set -euo pipefail
app="${1:-kube-cluster/apps/jellyfin}"
py=python3; command -v python3 >/dev/null 2>&1 || py=python

"$py" -m unittest discover -s "$app/tests" -p 'test_*.py' -v
