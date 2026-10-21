# 0060 — Grafana CRs live in the component itself; reserved directories are for apply ordering

- Status: Accepted; supersedes [0059](0059-grafana-objects-dir.md)
- Date: 2026-10-21, amended 2026-10-21
- Current answer: a component's Grafana Operator CRs ([0058](0058-grafana-operator-manages-grafana-and-its-content.md))
  are in the component: a `grafana.yaml` in its `kustomization.yaml` `resources:`, the dashboard
  JSON in a `dashboards/` folder read by a `configMapGenerator` there. The generator does not know
  about them. A reserved generator subdirectory exists only to be applied before the component.
  Components have no `dependsOn` on the operator; only `app-grafana` has one.

## Context

[0059](0059-grafana-objects-dir.md) made `grafana-objects/` a fourth reserved directory with its own
Kustomization, ExternalArtifact and cluster-level generator value. The other three
(`namespaces/`, see [0045](0045-namespaces-dir-never-pruned.md); `substitution-secrets/`;
`artifacts/`) have a reason the component cannot supply: something must exist before the
component applies. Grafana objects have none, so the generator carried special cases for what a
`resources:` entry does.

## Decision

- Reserved subdirectories are for apply ordering only. Anything else is part of the component.
- The CRs (namespace `grafana`) go in `grafana.yaml`. The dashboard ConfigMaps come from a
  `configMapGenerator` with `disableNameSuffixHash: true` and the operator's label, so their names
  are stable and the operator's informer sees them.
- Every CR and every dashboard ConfigMap carries `kustomize.toolkit.fluxcd.io/substitute: disabled`
  on the object itself: dashboard JSON and rule templates are full of `${...}`, which the
  component's `postBuild` would substitute, and a `commonAnnotations` would reach the whole
  component.
- The component depends on `infra-grafana-operator` for the CRDs. The operator therefore depends
  on nothing in the cluster: its Roles are manifests (`rbac.create: false`), because the chart's
  Role in namespace `grafana` made it wait for that namespace, which waits for `infra-postgres`.
- `scripts/check-grafana.py` enforces the object rules.
- Amended 2026-10-21: components do not depend on `infra-grafana-operator`. A missing CRD only
  delays their first apply on a fresh cluster (Flux retries); a `dependsOn` stalled their deploys
  whenever the operator was not Ready. Only `app-grafana` depends on it.

## Consequences

- The generator, its values and the golden tests have no Grafana-specific part, apart from the
  `Grafana` health check on the Kustomization that owns the CR.
- A Grafana apply error fails the component's Kustomization and, through `dependsOn`, what waits
  for it, where 0059 isolated it. The operator's `*Synchronized` conditions are no longer health
  checked.
- The operator's Roles are not updated by a chart bump; compare them with `helm template`.
- Until `app-grafana` applies its Role, the operator cannot watch `grafana` and restarts.

## References

- [README, Grafana objects](../../README.md#grafana-objects), `templates/kustomizations.yaml`.
