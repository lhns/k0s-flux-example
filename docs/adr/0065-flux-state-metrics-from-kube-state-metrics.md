# 0065 — Flux object state comes from kube-state-metrics in custom-resource-only mode

- Status: Accepted
- Date: 2026-10-26
- Current answer: `infra/kube-state-metrics` exports `gotk_resource_info` for Flux objects and
  nothing else. OTel stays the only source for built-in object state.

## Context

Alerting on "Flux object not Ready" needs the objects' Ready condition as a metric. The OTel
collectors have no receiver that turns a custom resource's status into metrics; Flux's own
controllers export only per-controller metrics.

## Decision

- Run kube-state-metrics with `--custom-resource-state-only=true` and no built-in collectors, so
  no core object is exported twice (OTel's `k8s_cluster` receiver owns those).
- Its config is Flux's `gotk_resource_info`, trimmed to stable labels and scoped to the CRD
  versions this cluster serves. RBAC is limited to the Flux API groups.

## Consequences

- One more component to run and bump.
- The config must follow Flux CRD version bumps (Alert and Provider move to v1 in Flux 2.10); a
  removed version silently stops that kind's series.
- Anything built on it must not match on `revision` or messages, which are not exported.

Details: [README](../../kube-cluster/infra/kube-state-metrics/README.md).
