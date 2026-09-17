#!/usr/bin/env bash
# Render the root generator chart and print it normalised (comment and blank lines
# stripped), so a golden file compares SEMANTICS rather than the template's own
# prose. Comments inside a `patch: |` block are string content, not YAML comments,
# and are deliberately kept.
#
#   render.sh fixtures      -> tests/fixtures/apps, fixed values, deterministic
#   render.sh real          -> live values with `generate` dropped: the Kustomizations,
#                              which is what each per-component release emits
#   render.sh real-generate -> live values verbatim: the per-component generators the
#                              root release emits
#
# The `real*` values are READ from kube-cluster/flux-system/generators.yaml, so they
# cannot drift from what is deployed. They exist to catch render errors and schema
# violations on real input; the fixtures are what pin behaviour.
set -euo pipefail
cd "$(dirname "$0")/.."

# python3 on CI, python in git-bash on Windows.
py=python3; command -v python3 >/dev/null 2>&1 || py=python

# spec.values of the live HelmRelease as JSON, minus the top-level keys named in $@.
live_values() {
  "$py" - "$@" <<'PY'
import json, pathlib, sys, yaml
src = pathlib.Path("kube-cluster/flux-system/generators.yaml")
rel = [d for d in yaml.safe_load_all(src.read_text(encoding="utf-8"))
       if d and d.get("kind") == "HelmRelease"]
if not rel:
    sys.exit(f"no HelmRelease in {src}")
values = rel[-1]["spec"]["values"]
for key in sys.argv[1:]:
    values.pop(key, None)
json.dump(values, sys.stdout)
PY
}

case "${1:-}" in
  fixtures)
    values='{"groups":[{"base":"tests/fixtures/apps","prefix":"app"}],
             "ociComposer":{"publicHost":"registry.test:5000"}}'
    ;;
  real)          values="$(live_values generate)" ;;
  real-generate) values="$(live_values)" ;;
  *) echo "usage: $0 {fixtures|real|real-generate}" >&2; exit 2 ;;
esac

helm template generators . -f - <<<"$values" \
  | grep -vE '^[[:space:]]*#' \
  | grep -vE '^[[:space:]]*$'
