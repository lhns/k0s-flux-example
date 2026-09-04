#!/usr/bin/env bash
# Validate the repo: YAML style (yamllint) + Kubernetes schema of every built
# component (kustomize build | kubeconform). Runs in CI (.github/workflows/lint.yml)
# and locally — needs yamllint, kustomize and kubeconform on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== yamllint =="
yamllint .

echo
echo "== helm template (generator chart) =="
# The generator decides every component's dependsOn, substitution sources and image
# tags, so a template regression is a cluster-wide one. tests/fixtures/ exercises it
# on inputs the real tree does not have; the golden pins the OUTPUT.
# Intentional change -> bash scripts/update-golden.sh, and review that diff.
if ! diff -u tests/golden/fixtures.yaml <(bash scripts/render.sh fixtures); then
  echo "== generator output moved (see diff above) ==" >&2
  echo "   intentional? bash scripts/update-golden.sh" >&2
  exit 1
fi
echo "golden OK"

echo
echo "== substitution: no credential in a ConfigMap, no unreachable name =="
# python3 on CI, python in git-bash on Windows.
py=python3; command -v python3 >/dev/null 2>&1 || py=python
"$py" scripts/check-substitution.py

echo
echo "== kustomize build + kubeconform =="
# Schemas are cached on disk (the CI runner persists .cache/ between runs), so once
# warm this needs no network.
cache="$PWD/.cache/kubeconform"
mkdir -p "$cache"

# kube-vnet publishes its CRD schemas per release. Pin the schema-location to the
# exact version this repo deploys — single source of truth is the component's
# OCIRepository tag, so it can't drift and needs no separate bump here.
kvver="$(grep -oE 'tag: *"[0-9][0-9.]*"' kube-cluster/infra/kube-vnet/release.yaml | grep -oE '[0-9][0-9.]*' | head -1)"
kvschema="https://raw.githubusercontent.com/lhns/kube-vnet/v${kvver}/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

# Same idea for oci-composer's ImageComposition/ImageBuild. Without this they fall through
# -ignore-missing-schemas and are never validated at all -- a CRD schema change then surfaces as
# an admission rejection at apply time instead of a lint failure, which is how the 0.4.0 -> 0.5.0
# publish/push rename could have reached the cluster unnoticed.
#
# The pin is derived from the deployed chart tag, so it cannot drift. Two shapes: a released
# chart X.Y.Z is git tag vX.Y.Z, while a dev build 0.0.0-dev.gSHA carries the commit itself.
ocver="$(grep -oE 'tag: *"[^"]+"' kube-cluster/infra/oci-composer/release.yaml | head -1 | grep -oE '"[^"]+"' | tr -d '"')"
case "$ocver" in
  0.0.0-dev.g*) ocref="${ocver#0.0.0-dev.g}" ;;
  *)            ocref="v${ocver}" ;;
esac
ocschema="https://raw.githubusercontent.com/lhns/kube-oci-composer/${ocref}/schemas/{{.ResourceKind}}-{{.Group}}-{{.ResourceAPIVersion}}.json"

# A component that does not BUILD is a hard failure, and it has to be recorded in a file: the
# loop below is the left-hand side of a pipe, so it runs in a subshell and a variable set there
# would not survive. Without this the build error prints, kubeconform still summarises the
# components that did build, and the script exits 0 -- CI green on a broken component.
buildfail="$(mktemp)"
trap 'rm -f "$buildfail"' EXIT

run() {
  : > "$buildfail"   # truncate: run() is retried, and stale entries would fail a healthy repo
  {
  for k in kube-cluster/infra/*/kustomization.yaml \
           kube-cluster/apps/*/kustomization.yaml \
           kube-cluster/flux-system/kustomization.yaml; do
    [ -f "$k" ] || continue
    echo "  $(dirname "$k")" >&2
    echo "---"
    kustomize build "$(dirname "$k")" || echo "$(dirname "$k")" >> "$buildfail"
  done
  # No kustomization.yaml in these by design (kustomize-controller synthesizes one), and
  # the component build above deliberately excludes them. Plain Secret manifests -- feed
  # them to kubeconform as-is.
  for f in kube-cluster/apps/*/substitution-secrets/*.yaml \
           kube-cluster/infra/*/substitution-secrets/*.yaml; do
    [ -f "$f" ] || continue
    echo "---"
    cat "$f"
  done
  # The Kustomizations the generator emits for the real tree. Nothing else schema-checks
  # them -- they are created by helm-controller, which validates nothing up front.
  echo "  generator output" >&2
  echo "---"
  bash scripts/render.sh real || echo "generator-chart" >> "$buildfail"
  } | kubeconform \
    -ignore-missing-schemas \
    -cache "$cache" \
    -schema-location default \
    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
    -schema-location "$kvschema"     -schema-location "$ocschema" \
    -summary 2>&1
}

# Retry with backoff so a rate-limited schema download recovers and warms the
# cache. Failing to *download* a schema (raw.githubusercontent throttling) leaves
# a resource unchecked this run — not a lint failure; the cache fills over runs.
# Only a real schema violation (Invalid > 0) fails the build.
out=""; invalid=0; errors=0
for backoff in 0 10 30; do
  [ "$backoff" -gt 0 ] && { echo "retrying throttled schema downloads in ${backoff}s..." >&2; sleep "$backoff"; }
  out="$(run || true)"
  echo "$out" | grep -E '^Summary:' || true
  invalid="$(printf '%s' "$out" | grep -oE 'Invalid: [0-9]+' | grep -oE '[0-9]+$' || echo 0)"
  errors="$(printf '%s' "$out" | grep -oE 'Errors: [0-9]+' | grep -oE '[0-9]+$' || echo 0)"
  [ "${errors:-0}" = 0 ] && break
done

if [ -s "$buildfail" ]; then
  echo "== kustomize build failed ==" >&2
  sort -u "$buildfail" | sed 's/^/  /' >&2
  exit 1
fi

if [ "${invalid:-0}" -gt 0 ]; then
  echo "== schema violations ==" >&2
  printf '%s\n' "$out" | grep -iE 'is invalid' >&2 || true
  exit 1
fi
[ "${errors:-0}" -gt 0 ] && \
  echo "note: ${errors} resource(s) unchecked (schema download throttled); cache fills next run." >&2
echo "kubeconform OK"
