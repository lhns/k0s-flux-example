# 0018 — k0s upgrades go through Autopilot, driven by the version in `k0sctl.yaml`

- Status: Accepted
- Date: 2026-07-10, amended 2026-09-02 and 2026-10-04
- Current answer: `spec.k0s.version` in `<cluster>/k0sctl.yaml` is the single source of the k0s
  version. The root generator reads it and emits a `k0s-upgrade` Kustomization whose Job, named
  after the version, recreates the Autopilot `Plan`; the cluster then upgrades itself,
  controllers first. A k0s bump is a reviewed PR, never automerged.

## Context

k0s was upgraded by running `k0sctl apply` from a workstation. Autopilot was already enabled in
the cluster; what was missing was a GitOps trigger.

## Decision

- `templates/k0s-upgrade.yaml` reads the version with `.Files.Get` and passes it to the
  Kustomization by `postBuild.substitute`. The Job lives in its own reconciler, isolated from the
  generator release.
- The Plan is created at runtime by the Job, never stored in git; a same-version run is a no-op.
- **The version is in the Job name**, so a bump prunes the old Job and creates a new one. That is
  the trigger.
- Opt-in per cluster through `values.k0sUpgrade`; a cluster without it renders nothing.
- Renovate tracks the version with a custom manager, labels it `k0s` and never automerges it,
  since a merge rolls every node.
- **`force: true`** (2026-09-02), so a change to the Job's image does not wedge the
  Kustomization on an immutable field.
- **Shared base** (2026-10-04): the Job, RBAC and namespace are a shared base
  (`shared/k0s-upgrade`); each cluster sets the image tag.

## Alternatives considered (2026-07-09)

- `k0sctl apply` from a workstation: kept as the fallback; the goal is a PR-gated rollout
  with no laptop in the loop.
- k0sctl inside the cluster: it needs every node's root SSH key in the cluster, and would evict
  itself when draining its own node.
- Autopilot's channel-based `UpdateConfig`: not pinned and not reviewed.
- Writing Autopilot's annotations directly: the Plan controller is the hard part, and doing it
  naively can restart every controller at once and lose etcd quorum.
- Cluster API or k0smotron: the declarative answer with real status, but a re-platform.
- One Job name with `force`: version-in-name was preferred as cleaner; `force` came back later
  only for image changes.

## Consequences

- Drains during an upgrade are where single-replica workloads and the Postgres primary hurt
  ([0029](0029-availability-single-replicas-and-ha-surge.md), [0014](0014-one-shared-cloudnative-pg-cluster.md)).
- Changes to `k0sctl.yaml` other than the version still need a manual `k0sctl apply`.
- No closed loop: git cannot tell whether the node roll succeeded; that lives in Autopilot's
  status.

## References

- `6bbeb404`, `614a6017`, `53bdcd5c`/`4c89dcce` (downgrade and upgrade round trip),
  `ff7408bf`, `e935ef88`.
- Conversation 2026-07-09 and 2026-07-10 (design).
