# 0058 — The Grafana Operator runs Grafana; components own their dashboards and alert rules as CRs

- Status: Accepted; supersedes [0053](0053-grafana-dashboards-provisioned-from-git.md)
- Date: 2026-10-21
- Current answer: the Grafana Operator, namespace-scoped to `grafana`, deploys Grafana from a
  `Grafana` CR in `apps/grafana`, on the same Postgres database and PVC. Dashboards and alert rule
  groups are `GrafanaDashboard`/`GrafanaAlertRuleGroup` CRs in namespace `grafana`, defined by the
  component that produces their metrics, in its `grafana.yaml`
  ([0060](0060-grafana-objects-in-the-component.md)). Existing folders are referenced by uid; contact points stay
  file-provisioned in `apps/grafana`; the notification policy tree stays in the UI.

## Context

File provisioning keeps every dashboard and rule in `apps/grafana`, far from the component whose
metrics they read, and one broken rule file stops Grafana from starting. Removed rules need
hand-written `deleteRules`. Nothing reports whether an object reached Grafana.

## Decision

- **The operator deploys Grafana** (managed mode) rather than managing an external instance: one
  definition of the instance, config rollouts done by the operator. `spec.deployment` merges onto
  the operator's defaults and carries today's env, labels, PVC and Recreate.
- **The `Grafana` CR lives in the `app-grafana` Kustomization**, not in the component's `grafana.yaml`: the
  takeover (CR created, old Deployment pruned) has to be one apply. So `app-grafana` needs the CRDs
  to apply and depends on the operator. The operator's Roles are manifests (the one in `grafana` in
  `apps/grafana`), so it does not depend back; until `app-grafana` applies, it restarts.
- **One Grafana pod at a time**: a ResourceQuota of one pod in `grafana`, plus `Recreate`. Flux
  applies new objects before it prunes old ones, so without it two Grafanas would share the
  database.
- **The operator logs in with its projected ServiceAccount token** through Grafana's `[auth.jwt]`
  (cluster JWKS, `iss`, `sub` and audience pinned), with org Admin. No admin password or API token
  is stored.
- **Operator scope**: watches `grafana` only, Roles only, no cluster-wide Secret access. All
  Grafana CRs live in `grafana`; no `allowCrossNamespaceImport`.
- **Ownership**: a CR is named, and a rule group's `spec.name` prefixed, with the owner's Flux
  name; every CR selects the instance by `grafana.example.com/instance: main`.
- **Folders**: existing ones by `folderUID`, never by a `GrafanaFolder`, whose finalizer deletes the
  folder with everything in it. New folders get a `GrafanaFolder` with a fixed uid and
  `kustomize.toolkit.fluxcd.io/prune: disabled`.
- **Contact points stay file-provisioned**: they hold no secret, and moving one means deleting a
  receiver that routes use. No policy, route or contact-point CRs.
- **Rules move with their uids**: provenance `file` cannot become `api`, so each batch is deleted
  by `deleteRules`, the entries removed, then recreated by CRs, in three PRs.
- Carried over from 0053: the `datasource` variable, and metrics enabled in the same PR as the
  dashboard.

## Consequences

- Alert state and `for` timers reset once per moved rule.
- UI saves of an operator dashboard are reverted at the next resync (10m); saving a copy still works.
- A dashboard or rule change no longer restarts Grafana; a change to `apps/grafana` still does.
- A CR deleted while Grafana is down stays in Grafana as an orphan.
- Uninstalling the operator: delete the `Grafana` CR first (no finalizer); content stays in Grafana.
- `check-grafana.py` enforces the naming, folder, instance and `deleteRules` rules.

## References

- Plan and evidence: grafana-operator v5.25.0 and Grafana 13.2.2 source; [apps/grafana](../../kube-cluster/apps/grafana/README.md#operator),
  [infra/grafana-operator](../../kube-cluster/infra/grafana-operator/README.md).
