#!/usr/bin/env bash
# Render the root generator chart and print it normalised (comment and blank lines
# stripped), so a golden file compares SEMANTICS rather than the template's own
# prose. Comments inside a `patch: |` block are string content, not YAML comments,
# and are deliberately kept.
#
#   render.sh fixtures          -> tests/fixtures/apps, fixed values, deterministic
#   render.sh fixtures-generate -> the same fixtures through the root release's generate path
#   render.sh real [CLUSTER]    -> live values with `generate` dropped: the Kustomizations,
#                                  which is what each per-component release emits
#   render.sh real-generate [CLUSTER]
#                               -> live values verbatim: the per-component generators the
#                                  root release emits
#
# The `real*` values are READ from <CLUSTER>'s root HelmRelease in flux-system/ (default
# kube-cluster), so they cannot drift from what is deployed. They exist to catch render errors and schema
# violations on real input; the fixtures are what pin behaviour.
set -euo pipefail
cd "$(dirname "$0")/.."

# python3 on CI, python in git-bash on Windows.
py=python3; command -v python3 >/dev/null 2>&1 || py=python

# spec.values of the live HelmRelease as JSON, minus the top-level keys named in $@.
# Dropping generate keeps its pruneDisabled, which the Kustomizations read at the top.
live_values() {
  "$py" - "$cluster" "$@" <<'PY'
import json, sys
sys.path.insert(0, "scripts")
import clusters
cluster = sys.argv.pop(1)
path, rel = clusters.root_release(cluster)
if rel is None:
    sys.exit(f"{cluster}/flux-system: no generator root HelmRelease (values.groups)")
values = rel["spec"]["values"]
for key in sys.argv[1:]:
    dropped = values.pop(key, None) or {}
    if key == "generate":
        if dropped.get("pruneDisabled"):
            values["pruneDisabled"] = dropped["pruneDisabled"]
json.dump(values, sys.stdout)
PY
}

# controlled and dependent have prune off.
fixture_cn='"pruneDisabled":["app-controlled","app-dependent"]'
cluster="${2:-kube-cluster}"

case "${1:-}" in
  fixtures)
    values='{"groups":[{"base":"tests/fixtures/apps","prefix":"app"}],'"$fixture_cn"'}'
    ;;
  fixtures-generate)
    values='{"groups":[{"base":"tests/fixtures/apps","prefix":"app"}],"generate":{'"$fixture_cn"'},"alerts":{"providerRef":"matrix","cluster":"fixture"}}'
    ;;
  real)          values="$(live_values generate)" ;;
  real-generate) values="$(live_values)" ;;
  *) echo "usage: $0 {fixtures|fixtures-generate|real|real-generate} [CLUSTER]" >&2; exit 2 ;;
esac

helm template generators . -f - <<<"$values" \
  | grep -vE '^[[:space:]]*#' \
  | grep -vE '^[[:space:]]*$'
