#!/usr/bin/env bash
# The matrix-payload template (this component) and the relay's firing-template (matrix-alerts)
# are one contract; this renders every fixture through the pinned Grafana and relay images.
# Run from the repo root by scripts/run-tests.sh, with the component directory as $1.
set -euo pipefail
dir="${1:-kube-cluster/apps/grafana}"
py=python3; command -v python3 >/dev/null 2>&1 || py=python
exec "$py" "$dir/tests/check.py" "$dir"
