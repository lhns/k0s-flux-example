#!/usr/bin/env bash
# Validate the repo. Two halves: the repo-wide checks below, which cover every component of
# every cluster (any */flux-system/kustomization.yaml) unconditionally, and the opt-in
# per-component tests that scripts/run-tests.sh discovers (convention: kube-cluster/README.md).
# Runs in CI (.github/workflows/lint.yml) and locally — needs yamllint, kustomize and
# kubeconform on PATH.
#
# The checks run concurrently: CI has a one-minute budget, and run one after another they take
# several. Each prints in the order below once it is done; any failure fails the script.
set -euo pipefail
cd "$(dirname "$0")/.."

# python3 on CI, python in git-bash on Windows.
py=python3; command -v python3 >/dev/null 2>&1 || py=python
# Fixed, not nproc: on the CI runner nproc reports the node, not the pod
# (kube-cluster/apps/github-runner/README.md). check-artifacts.py reads it too.
export JOBS="${JOBS:-8}"

tmp="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$tmp"' EXIT
mapfile -t clusters < <("$py" scripts/clusters.py)

check_yamllint() {
  # Sharded, since it is single-threaded; --list-files applies .yamllint.yaml's ignores as
  # `yamllint .` does.
  yamllint --list-files . | tr -d '\r' | tr '\n' '\0' | xargs -0 -r -P "$JOBS" -n 50 yamllint
}

check_golden() {
  # The generator decides every component's dependsOn and substitution sources,
  # so a template regression is a cluster-wide one. tests/fixtures/ exercises it
  # on inputs the real tree does not have; the golden pins the OUTPUT.
  # Intentional change -> bash scripts/update-golden.sh, and review that diff.
  for mode in fixtures fixtures-generate; do
    if ! diff -u "tests/golden/$mode.yaml" <(bash scripts/render.sh "$mode"); then
      echo "== generator output moved (see diff above) ==" >&2
      echo "   intentional? bash scripts/update-golden.sh" >&2
      return 1
    fi
  done
  echo "golden OK"
  bash tests/generator-guards.sh
}

check_release_size() {
  # Helm embeds the whole chart in the release Secret and the apiserver caps it at 1 MiB.
  # It fails SILENTLY at the cap: the release stops applying and every generated
  # Kustomization freezes. See docs/generator-chart-size.md.
  "$py" scripts/check-release-size.py
}

check_substitution() {
  KUSTOMIZE_BUILDS="$builds" "$py" scripts/check-substitution.py
}

check_migration() {
  # The gate is what stands between a risky change and a pruned component; it only runs live by hand.
  "$py" tests/check-migration.py
}

check_source_pins() {
  # A mismatch leaves the composition Pending forever; an absent one lets it build and roll
  # out the previous revision's content. See kube-cluster/infra/oci-composer/README.md.
  "$py" scripts/check-source-pins.py
}

check_fetch_digests() {
  # Renovate bumps a fetch url, not its digest, and a mismatch is terminal in the composer.
  # Downloads each new url+digest pair once; .cache/fetch-digests remembers the verified ones.
  "$py" scripts/check-fetch-digests.py
}

check_arc_dockerfile() {
  "$py" scripts/check-arc-dockerfile.py
}

check_synapse_pymods() {
  # The modules in /pymods are resolved against the base image's interpreter, and two
  # different renovate managers see the two pins as unrelated deps. A split bump is an
  # ImportError at synapse startup pointing nowhere near the cause.
  local from_tag img_tag
  from_tag="$(grep -ohE 'FROM matrixdotorg/synapse:[^@[:space:]]+' kube-cluster/apps/matrix/artifacts/*.yaml | sed 's/.*://' | sort -u)"
  img_tag="$(grep -oE 'image: matrixdotorg/synapse:[^@[:space:]]+' kube-cluster/apps/matrix/resources.yaml | sed 's/.*://' | sort -u)"
  if [ -z "$from_tag" ] || [ -z "$img_tag" ]; then
    echo "no matrixdotorg/synapse pin found in artifacts/ or resources.yaml" >&2
    return 1
  fi
  if [ "$from_tag" != "$img_tag" ]; then
    echo "== synapse tag drift ==" >&2
    echo "  artifacts/ FROM: $from_tag" >&2
    echo "  resources.yaml:  $img_tag" >&2
    return 1
  fi
  echo "synapse $from_tag OK"
}

check_sops() {
  # Structure only -- CI has no age key, so a broken MAC is NOT caught here.
  # scripts/check-sops-mac.sh, on a host that holds the key, is what catches that.
  "$py" scripts/check-sops.py
}

check_clusters() {
  "$py" scripts/check-clusters.py
}

check_artifacts() {
  # A ../ reference out of the component builds here and fails only in the cluster.
  "$py" scripts/check-artifacts.py
}

# --- every component built once ------------------------------------------------------------
# $builds/<dir with / as %> per build target, and $rendered/<cluster> the whole stream
# kubeconform reads; check-substitution.py and check-coverage.py reuse them.
builds="$tmp/builds"
rendered="$tmp/rendered"
# A component that does not BUILD is a hard failure, recorded here; without it the build
# error prints, kubeconform still summarises the components that did build, and CI is green.
buildfail="$tmp/buildfail"

build_all() {
  mkdir -p "$builds" "$rendered"
  : > "$buildfail"
  local c mode
  local -a comps render_pids=()
  # Everything the generator emits for the real tree, in BOTH shapes: the per-component
  # ArtifactGenerator+HelmRelease the root release emits (real-generate), and the Kustomizations
  # each of those releases then emits (real). Nothing else schema-checks either -- they are
  # created by helm-controller, which validates nothing up front.
  for c in "${clusters[@]}"; do
    for mode in real real-generate; do
      { bash scripts/render.sh "$mode" "$c" > "$tmp/gen-$mode-$c" ||
          echo "generator-chart ($mode, $c)" >> "$buildfail"; } &
      render_pids+=($!)
    done
    mapfile -t comps < <("$py" scripts/clusters.py components "$c")
    printf '%s\n' "${comps[@]}" > "$tmp/comps-$c"
    printf '%s\n' "${comps[@]}" "$c/flux-system" $("$py" scripts/clusters.py k0s-upgrade "$c") > "$tmp/targets-$c"
  done
  # A failed build leaves <out>.failed holding kustomize's error.
  cat "$tmp"/targets-* | sort -u | tr '\n' '\0' | xargs -0 -r -P "$JOBS" -n 1 sh -c '
    out="$1/$(printf %s "$3" | tr / %)"
    if kustomize build "$3" > "$out" 2> "$out.failed"; then rm -f "$out.failed"; else echo "$3" >> "$2"; fi
  ' sh "$builds" "$buildfail"
  wait "${render_pids[@]}"

  # One awk over the file list, not a cat per file: a fork each costs seconds in Git Bash.
  for c in "${clusters[@]}"; do
    {
      while read -r d; do printf '%s\0' "$builds/${d//\//%}"; done < "$tmp/targets-$c"
      # No kustomization.yaml in these by design (kustomize-controller synthesizes one), and
      # the component build above deliberately excludes them. Plain manifests -- feed them to
      # kubeconform as-is.
      while read -r d; do
        for f in "$d"/substitution-secrets/*.yaml "$d"/artifacts/*.yaml "$d"/namespaces/*.yaml; do
          if [ -f "$f" ]; then printf '%s\0' "$f"; fi
        done
      done < "$tmp/comps-$c"
      printf '%s\0' "$tmp/gen-real-$c" "$tmp/gen-real-generate-$c"
    } | xargs -0 awk 'FNR == 1 { print "---" } 1' > "$rendered/$c"
  done
}

check_kubeconform() {
  # Schemas are cached on disk (CI restores .cache/ from the Actions cache), so once warm this
  # needs no network.
  local cache="$PWD/.cache/kubeconform"
  mkdir -p "$cache"

  # kube-vnet and oci-composer publish their CRD schemas per release. Pin each cluster's
  # schema-location to the exact version that cluster deploys (the component's own tag, so
  # it can't drift and needs no separate bump here): kube-test-cluster takes a minor first.
  # Without oci-composer's, ImageComposition/ImageBuild fall through -ignore-missing-schemas
  # and are never validated. Release X.Y.Z is git tag vX.Y.Z; a dev build is
  # 0.0.0-dev.SHA (kube-vnet) or 0.0.0-dev.gSHA (oci-composer), which carries the commit.
  # Sets the array `schemas` to the -schema-location arguments for cluster $1.
  local -a schemas
  pinned_schemas() {
    local c="$1" ver ref
    schemas=()
    ver="$(grep -oE 'tag: *"[^"]+"' "$c/infra/kube-vnet/version.yaml" 2>/dev/null | head -1 | grep -oE '"[^"]+"' | tr -d '"' || true)"
    if [ -n "$ver" ]; then
      case "$ver" in 0.0.0-dev.*) ref="${ver#0.0.0-dev.}" ;; *) ref="v${ver}" ;; esac
      schemas+=(-schema-location "https://raw.githubusercontent.com/lhns/kube-vnet/${ref}/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
    fi
    ver="$(grep -oE 'tag: *"[^"]+"' "$c/infra/oci-composer/release.yaml" 2>/dev/null | head -1 | grep -oE '"[^"]+"' | tr -d '"' || true)"
    if [ -n "$ver" ]; then
      case "$ver" in 0.0.0-dev.g*) ref="${ver#0.0.0-dev.g}" ;; *) ref="v${ver}" ;; esac
      schemas+=(-schema-location "https://raw.githubusercontent.com/lhns/kube-oci-composer/${ref}/schemas/{{.ResourceKind}}-{{.Group}}-{{.ResourceAPIVersion}}.json")
    fi
  }

  run() {
    local c
    for c in "${clusters[@]}"; do
      pinned_schemas "$c"
      "$py" scripts/strip-sops-metadata.py < "$rendered/$c" | kubeconform \
        -ignore-missing-schemas \
        -strict \
        -cache "$cache" \
        -schema-location default \
        -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
        "${schemas[@]}" \
        -summary 2>&1 | sed "s/^Summary:/Summary ($c):/" || echo "kubeconform did not run for $c"
    done
  }

  # Retry with backoff so a rate-limited schema download recovers and warms the cache. A
  # resource whose schema could not be *downloaded* stays unchecked this run, which is not
  # a lint failure; the cache fills over runs. Everything else fails: a schema violation, a
  # document kubeconform cannot parse or that lacks kind/apiVersion (reserved directories
  # reach it raw), and a run with no summary (kubeconform missing or crashed).
  local out="" invalid=0 throttled=0 hard=0 summaries=0 backoff
  for backoff in 0 10 30; do
    [ "$backoff" -gt 0 ] && { echo "retrying throttled schema downloads in ${backoff}s..." >&2; sleep "$backoff"; }
    out="$(run || true)"
    printf '%s\n' "$out" | grep -E '^Summary' || true
    summaries="$(printf '%s\n' "$out" | grep -cE '^Summary' || true)"
    invalid="$(printf '%s\n' "$out" | awk '/^Summary/ { for (i = 1; i < NF; i++) if ($i == "Invalid:") n += $(i + 1) } END { print n + 0 }')"
    throttled="$(printf '%s\n' "$out" | grep -c 'failed downloading schema' || true)"
    hard="$(printf '%s\n' "$out" | grep 'failed validation' | grep -vc 'failed downloading schema' || true)"
    [ "${throttled:-0}" = 0 ] && break
  done

  if [ -s "$buildfail" ]; then
    echo "== kustomize build failed ==" >&2
    sort -u "$buildfail" | while read -r d; do
      echo "  $d" >&2
      if [ -f "$builds/${d//\//%}.failed" ]; then sed 's/^/    /' "$builds/${d//\//%}.failed" >&2; fi
    done
    return 1
  fi

  if [ "$summaries" != "${#clusters[@]}" ]; then
    echo "== kubeconform did not run to completion (missing, or it crashed) ==" >&2
    printf '%s\n' "$out" | tail -5 >&2
    return 1
  fi
  if [ "$invalid" -gt 0 ] || [ "$hard" -gt 0 ]; then
    echo "== schema violations ==" >&2
    printf '%s\n' "$out" | grep -iE 'is invalid|failed validation' | grep -v 'failed downloading schema' >&2 || true
    echo "   (all of them at once? a stale schema cache: rm -rf .cache/kubeconform)" >&2
    return 1
  fi
  [ "$throttled" -gt 0 ] && \
    echo "note: ${throttled} resource(s) unchecked (schema download failed); cache fills next run." >&2
  echo "kubeconform OK"
}

check_coverage() {
  local c
  for c in "${clusters[@]}"; do
    "$py" scripts/check-coverage.py "$c" "$rendered/$c"
  done
}

check_grafana() {
  # The real tree from the builds above; the test runs the same rules over tests/fixtures.
  KUSTOMIZE_BUILDS="$builds" "$py" scripts/check-grafana.py
  "$py" tests/check-grafana.py
}

check_redirects() {
  # Host() ignores a port in the Host header, redirectRegex does not: see the script.
  KUSTOMIZE_BUILDS="$builds" "$py" scripts/check-redirects.py
  "$py" tests/check-redirects.py
}

check_tests() {
  # Printed last, so its summary -- the only place a skip is guaranteed to be seen -- is the
  # final thing on screen. A failure or an un-opted-out missing tool there fails this script.
  bash scripts/run-tests.sh
}

# --- run ------------------------------------------------------------------------------------
# Microseconds without a fork; whole seconds before bash 5.
now() { local t="${EPOCHREALTIME:-$SECONDS.000000}"; echo "${t//[.,]/}"; }
t0="$(now)"
declare -A pid
# The inner subshell keeps set -e for the check; the outer one records when it finished.
start() {
  ( set +e; ( set -e; "$1" ); rc=$?; now > "$tmp/$1.end"; exit "$rc" ) > "$tmp/$1.out" 2>&1 &
  pid[$1]=$!
}

for check in check_yamllint check_golden check_release_size check_migration check_source_pins \
    check_fetch_digests check_arc_dockerfile check_synapse_pymods check_sops check_clusters \
    check_artifacts check_tests; do
  start "$check"
done
build_all
start check_substitution
start check_kubeconform
start check_coverage
start check_grafana
start check_redirects

failed=()
report() {  # <function> <title>
  local rc=0 ds
  wait "${pid[$1]}" || rc=$?
  ds=$(( ($(cat "$tmp/$1.end") - t0) / 100000 ))
  echo
  echo "== $2 == (done at $((ds / 10)).$((ds % 10))s)"
  cat "$tmp/$1.out"
  [ "$rc" = 0 ] || failed+=("$2")
}
report check_yamllint "yamllint"
report check_golden "helm template (generator chart)"
report check_release_size "generator release size"
report check_substitution "substitution: no credential in a ConfigMap, no unreachable name"
report check_migration "migration gate: offline test"
report check_source_pins "oci-composer: every sourceRef pins the revision its source is pinned at"
report check_fetch_digests "oci-composer: every fetch digest matches what its url serves"
report check_arc_dockerfile "arc runner images: one Dockerfile in both ImageBuilds"
report check_synapse_pymods "matrix: synapse-pymods FROM matches the synapse image"
report check_sops "sops: every encrypted file is whole and encrypted to the cluster's key"
report check_clusters "clusters: each reads only its own directory, shared/ holds plain bases"
report check_artifacts "artifacts: every component builds from what its ExternalArtifact holds"
report check_kubeconform "kustomize build + kubeconform"
report check_coverage "coverage: every CSI StorageClass has a snapshot class, every CNPG Cluster a backup"
report check_grafana "grafana: operator objects opt out of substitution, with fixed folders and uids"
report check_redirects "redirects: alias routes answer from noop@internal and match any Host"
report check_tests "per-component tests"

if [ "${#failed[@]}" -gt 0 ]; then
  echo >&2
  echo "== FAILED ==" >&2
  printf '  %s\n' "${failed[@]}" >&2
  exit 1
fi
