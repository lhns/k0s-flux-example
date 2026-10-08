# 0039 — One generator release per component, because a Helm release is capped at 1 MiB

- Status: Accepted; its chart source is now [0047](0047-externalartifacts-per-component.md)
- Date: 2026-09-17
- Current answer: the root `generators` release does not render Kustomizations. For each
  component it emits a `gen-<group>-<name>` HelmRelease that renders the same chart against a
  source holding only that component, and emits its Kustomizations. The root release sets
  `generate`, the per-component ones do not, which stops the recursion.
  `scripts/check-release-size.py` fails the lint above 70% of the cap.

## Context

Helm stores each release as `gzip(JSON(release))` in a Secret, which embeds the whole chart, and
the chart was the repository. The apiserver caps a Secret at 1 MiB and no flag raises it. A
release over the cap stops applying silently: every generated Kustomization freezes at its
previous content and nothing alerts.

## Decision

- **Filter what the chart can read** ([0040](0040-filtered-gitrepository-per-component.md)), and
  **split the release per component**, so each release carries one component's files.
- The handover runs with prune off on every Kustomization
  ([0012](0012-prune-off-around-risky-changes.md)): the new releases recreate the Kustomizations
  under the same names and adopt their objects.
- Release history kept to three revisions.
- **A guard**: the size check models the release from the files its source admits.

## Alternatives considered

- `.helmignore`: no negation, so it cannot express an include-list.
- A chart rooted per group: a chart cannot read outside its own root, so it would not see the
  components.
- One release per group: every group would package the identical chart.
- `spec.chart.spec.ignore`: no such field in Flux. Subcharts: stored in the same release
  Secret. Another Helm storage driver: helm-controller exposes none, and a ConfigMap has the same
  cap.
- A chart root inside each component directory: rejected in favour of one filtered source per
  component and a value that makes the root render them.

## Consequences

- A release per component instead of one; each component's chart artifact is small and
  reconciles on its own.
- Anything the templates read must be admitted to the component's source, or the render quietly
  loses it.

## References

- `c88aa67e`, `91e2eaa8`, `d38741d9`, `1607f629`, `b5340870`, `f31960d3`.
- Conversation 2026-09-17 (design, migration plan).
- [docs/generator-chart-size.md](../generator-chart-size.md).
