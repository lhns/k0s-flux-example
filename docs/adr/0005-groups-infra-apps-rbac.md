# 0005 — Three groups: `infra`, `apps`, `rbac`

- Status: Accepted
- Date: 2026-07-07, amended 2026-07-21 and 2026-09-16; sharpened by PR #652; extended by
  [0056](0056-external-group-for-systems-outside-the-cluster.md)
- Current answer: `infra/` is what the cluster and other components need to function: storage,
  ingress, databases, shared operators. `apps/` is applications, including an operator that
  serves only one app. `rbac/` is cluster-wide RBAC that no single app owns. The group decides
  the Kustomization's prefix (`infra-`, `app-`, `rbac-`) and nothing else.
- **Amended 2026-10-20:** a fourth group, `external/` (prefix `ext-`), holds components that only
  describe systems outside the cluster ([0056](0056-external-group-for-systems-outside-the-cluster.md)).

## Context

The first layout split `apps/` and `infrastructure/`. When the two generators were unified
([0004](0004-one-kustomization-per-component-from-a-root-chart.md)) the groups became values of
one chart.

## Decision and its precedents

- Postgres moved from `apps/` to `infra/` (2026-07-08): a platform data service that other
  components depend on.
- Authelia moved from `infra/` to `apps/` (2026-07-18): at the time it provided only forwardAuth
  middlewares, not a platform primitive.
- `rbac/` was added (2026-09-16): an OIDC group binding applies to every client presenting an
  id_token, kubectl included, so no single app owns it.
- A per-component Postgres lives with its app, not in `infra/` (2026-07-21): the operator and a
  cluster-wide instance are infra, an instance private to one app is part of that app.
- A Traefik redirect for one site moved out of `infra/traefik` into its own app (PR #459):
  one-off links are not inlined into shared infra.
- NATS went to `infra/` beside Kafka (2026-09-29), as a shared messaging service.
- PR #652 writes the rule down while adding the Actions Runner Controller to `apps/`: it
  may need elevated permissions to deploy, but the cluster does not need it to function.
  Bringing CRDs or an operator does not make a component infra.

## Consequences

- **Moving a component between groups renames its Kustomization**, which prunes it unless
  prune is off first: README,
  [Moving a component between groups](../../README.md#moving-a-component-between-groups).
- The group is not a dependency mechanism; ordering is `dependsOn`
  ([0011](0011-generator-settings-are-annotations.md)).
- The group carries no permission difference yet. It is expected to once components get
  per-tenant service accounts ([0046](0046-control-namespaces-always-on.md)).

## References

- `583447cd`, `39334ad1`, `1e9fc2ba`, `abd926e3`, `c118c18a`, PR #459, PR #652.
- Conversation 2026-07-21 (per-app instances), 2026-09-27 (redirect), 2026-10-05 (definition).
