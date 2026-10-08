# 0034 — Chart CRDs are upgraded by default

- Status: Accepted
- Date: 2026-08-09
- Current answer: the generator patches `spec.upgrade.crds: CreateReplace` into every HelmRelease
  of every component. A component overrides it with `flux-component-generator/crdPolicy`
  (`Skip`, `Create`, `CreateReplace`).

## Context

Helm never touches a chart's `crds/` directory on upgrade, and helm-controller defaults
`spec.upgrade.crds` to `Skip`. A chart's CRDs therefore freeze at the first installed version,
silently: new fields do not exist, and the API server prunes them from every resource that sets
one.

## Decision

- There is no controller-wide setting, so the generator is the one place a repo-wide default can
  live. It is a merge patch, so other `spec.upgrade` fields survive.

## Consequences

- `CreateReplace` replaces. Additive schema changes are safe; a chart dropping a CRD version
  that still has stored objects is rejected by the API server, so that failure is loud.
- Only charts with a `crds/` directory are affected; charts that template their CRDs already
  upgrade them.

## References

- `c741c642`.
- README: [Chart CRDs are upgraded](../../README.md#chart-crds-are-upgraded).
