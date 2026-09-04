#!/usr/bin/env bash
# Refresh tests/golden/ after an INTENTIONAL generator change. Always read the
# resulting diff -- it is the change under review.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/render.sh fixtures > tests/golden/fixtures.yaml
echo "wrote tests/golden/fixtures.yaml"
git --no-pager diff --stat -- tests/golden/ || true
