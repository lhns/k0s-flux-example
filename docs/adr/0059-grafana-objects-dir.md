# 0059 — `grafana-objects/`: a component's Grafana CRs get their own Kustomization

- Status: Superseded by [0060](0060-grafana-objects-in-the-component.md)
- Date: 2026-10-21
- Current answer: no longer used. A component's Grafana Operator CRs ([0058](0058-grafana-operator-manages-grafana-and-its-content.md))
  went in the reserved subdirectory `grafana-objects/`, which has its own `kustomization.yaml`
  and ExternalArtifact and becomes the Kustomization `<name>-grafana-objects`. Nothing depends on
  it, it never substitutes, and its health comes from `healthCheckExprs` on the operator's
  `*Synchronized` conditions. It depends on the root value `grafanaObjects.dependsOn`.

## Context

Listing the CRs in the component itself would make its apply fail whenever the CRDs are missing,
and a Grafana sync failure would hold back the component and everything depending on it. The
operator's CRs have no `Ready` condition, so kstatus calls a failed sync Current.

## Decision

- A fourth reserved subdirectory, like [0045](0045-namespaces-dir-never-pruned.md), but with a
  `kustomization.yaml`: dashboards need a `configMapGenerator`. Only that file goes into the
  generator chart; the JSON goes into the directory's own artifact.
- Never a dependency of the component; its failure fires the component's Flux error alert and blocks
  nothing.
- Never `postBuild`: dashboard JSON is full of `${...}`.
- Its dependsOn comes from the cluster's root values, so a cluster without the operator has no such
  Kustomization; the generator refuses the directory there.
- Not `grafana/`: its suffix would become reserved, and `app-grafana` would resolve to the control namespace `gen-app`.

## Consequences

- The root artifact copies `*/*/grafana-objects/kustomization.yaml` only once one exists: a copy
  glob that matches nothing fails the whole root artifact.
- A failed sync shows after the Kustomization timeout; spec errors (`InvalidSpec`,
  `NoMatchingFolder`) fail at once.

## References

- [README, Reserved subdirectories](../../README.md#reserved-subdirectories), `templates/kustomizations.yaml`.
