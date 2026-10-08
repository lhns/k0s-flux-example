# 0004 — One Flux Kustomization per component, emitted by a repo-root Helm chart

- Status: Accepted; amended by [0039](0039-one-generator-release-per-component.md) and
  [0047](0047-externalartifacts-per-component.md)
- Date: 2026-07-07, amended 2026-08-24
- Current answer: a component is a directory `<cluster>/<group>/<name>/` with a
  `kustomization.yaml`, and each gets its own Flux Kustomization. Nobody keeps a list: the Helm
  chart at the repo root (`Chart.yaml`, `templates/`, "flux-component-generator") globs
  `<base>/*/kustomization.yaml` for every group and emits the Flux objects. Adding a component
  is adding a directory.

## Context

One Kustomization per component makes each its own failure domain: a component that fails to build
or apply does not block the rest. The earlier generators
([0002](0002-first-generators-artifactgenerator-and-resourceset.md)) still needed an input list, and
keeping a Kustomization per app by hand does not scale.

## Decision

- A Helm chart's `.Files.Glob` sees the repository, so the directory tree is the list.
- One parameterised chart for every group, rendered by the `generators` HelmRelease in
  `<cluster>/flux-system/`. Groups are values (`base`, `prefix`); the Kustomization is named
  `<prefix>-<dir>`. The chart stays at the repo root because a chart cannot read outside its
  own root.
- `reconcileStrategy: Revision`, so a new directory renders without a chart-version bump.
- Generated Kustomizations prune; whether they wait is set per group. A prune toggle was left
  out as unused; it came later as `pruneDisabled` ([0012](0012-prune-off-around-risky-changes.md)).
- **No wait** (2026-08-24): the generator releases do not wait for the Kustomizations they emit,
  since Helm's wait would block all rendering behind one unhealthy component; each Kustomization
  reports its own health.
- **Precedent** (2026-07-30): the Swarm's bundled telemetry ingest became `infra/strimzi` and
  `infra/opentelemetry-gateway`, so a collector config typo cannot block Kafka's reconcile.
  Things that fail independently are separate components.

## Alternatives considered

- kustomize's `helmCharts` inflator: needs `--enable-helm`, which Flux never sets.
- kustomize exec or KRM plugins: kustomize-controller disables them by design.
- A ResourceSet input provider backed by an HTTP shim listing the directories: one more
  service, and an empty answer deletes every component.
- CI regenerating the list: removing a component should stay a deliberate change.

## Consequences

- **The Kustomization is named after the directory.** Renaming or moving a component deletes
  its Kustomization, and deleting a pruning Kustomization deletes what it applied:
  [0012](0012-prune-off-around-risky-changes.md).
- The chart embeds whatever it can read, and the repo is the chart, which runs into Helm's 1 MiB
  release cap: [0039](0039-one-generator-release-per-component.md).
- Generator behaviour is pinned by golden tests (`tests/`), since nothing else renders the
  chart against cases the live tree lacks ([0016](0016-validate-sh-one-offline-gate.md)).

## References

- `3438fa99`, `9c18200f` (per-group charts), `583447cd` (unified root chart), `7b52ca2e`
  (`kube-cluster/` layout), `6cb55ccb` (no wait), `6f997d23`, `c06f574a` (telemetry split).
- Conversation 2026-07-03 to 2026-07-07 (alternatives), 2026-07-30 (telemetry split).
- README: [How it works](../../README.md#how-it-works), [Adding a component](../../README.md#adding-a-component).
