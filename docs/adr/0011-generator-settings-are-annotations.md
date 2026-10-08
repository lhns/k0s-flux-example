# 0011 — Generator settings are annotations on the component's own `kustomization.yaml`

- Status: Accepted
- Date: 2026-07-08, amended 2026-07-10, 2026-08-09, 2026-09-27 and 2026-10-02
- Current answer: a component configures its generated Kustomization with annotations under
  `flux-component-generator/` on its own `kustomization.yaml`: `dependsOn`, `appInstance`,
  `crdPolicy`, `suspend`, `bases`. Kustomize ignores `metadata` there, so they never reach the
  cluster. Opting into a reserved subdirectory is having it, with no annotation at all.

## Context

The first per-component setting was ordering (Traefik waits for MetalLB). Keeping it in a central
list would bring back the list [0004](0004-one-kustomization-per-component-from-a-root-chart.md)
removed.

## Decision

- An inert annotation, read by the chart with a default when absent.
- Added over time: `dependsOn` (2026-07-08); `appInstance` (2026-07-10, the
  `app.kubernetes.io/instance` label the generator stamps on everything, so each component shows
  as one app in Headlamp); `crdPolicy` ([0034](0034-chart-crds-upgraded-by-default.md));
  `suspend` (2026-09-27, parks the component Kustomization while its `gen-` release stays live to
  lift it again); `bases` ([0048](0048-several-clusters-one-repo.md)).
- **Prefix** (2026-10-02, PR #532): keys are named after the pattern, `flux-component-generator/`. A
  renamed key is matched under both prefixes during the move, because a ConfigMap built without the
  patch prunes its Secret. "Component" and "group" stay as the terms: the generator does not order
  groups, so "layer" would claim more than it does. The chart stays in this repo for now
  (conversation 2026-10-01).

## Alternatives considered (2026-07-03 to 2026-07-08)

- `dependsOn` everywhere, apps after infra: Flux converges by retrying anyway, and a blanket
  dependency couples failure domains, so one unhealthy infra component would stall every app update.
  A synthetic "infra ready" gate was declined for the same reason.
- A central dependency map: each component declares its own.
- A structured field in `kustomization.yaml` or a side file: kustomize rejects unknown fields and
  `metadata` is closed, so only an annotation can carry it.

## Consequences

- `suspend` stops only the Kustomization; HelmReleases the component defines keep reconciling,
  so `flux suspend hr` those too (AGENTS.md).
- Since control namespaces, `dependsOn` names resolve to `gen-<owner>`, and a component name
  ending in `-namespaces`, `-artifacts` or `-substitution-secrets` is refused
  ([0046](0046-control-namespaces-always-on.md)).

## References

- `8caa95b1`, `37fc1bb2`, `8293983e`, PR #447, PR #532, #533, #534.
- Conversation 2026-07-03 and 2026-07-08 (dependencies), 2026-10-01 (naming).
- README: [Annotations](../../README.md#annotations).
