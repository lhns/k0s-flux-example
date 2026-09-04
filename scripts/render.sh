#!/usr/bin/env bash
# Render the root generator chart and print it normalised (comment and blank lines
# stripped), so a golden file compares SEMANTICS rather than the template's own
# prose. Comments inside a `patch: |` block are string content, not YAML comments,
# and are deliberately kept.
#
#   render.sh fixtures  -> tests/fixtures/apps, fixed values, deterministic
#   render.sh real      -> the live kube-cluster tree
#
# `real` mirrors kube-cluster/flux-system/generators.yaml. It is not read from
# there: pulling spec.values out needs a YAML parser this script deliberately does
# not depend on. It exists to catch render errors and schema violations on real
# input; the fixtures are what pin behaviour.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  fixtures)
    args=(--set-json 'groups=[{"base":"tests/fixtures/apps","prefix":"app"}]'
          --set ociComposer.publicHost=registry.test:5000)
    ;;
  real)
    args=(--set-json 'groups=[{"base":"kube-cluster/apps","prefix":"app"},{"base":"kube-cluster/infra","prefix":"infra"}]'
          --set ociComposer.publicHost=oci-composer.internal:30500
          --set k0sUpgrade.k0sctlPath=kube-cluster/k0sctl.yaml
          --set k0sUpgrade.path=kube-cluster/k0s-upgrade)
    ;;
  *) echo "usage: $0 {fixtures|real}" >&2; exit 2 ;;
esac

helm template generators . "${args[@]}" \
  | grep -vE '^[[:space:]]*#' \
  | grep -vE '^[[:space:]]*$'
