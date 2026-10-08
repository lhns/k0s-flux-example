# 0025 — Manifest rules: explicit namespaces, and config changes that reach the pod

- Status: Accepted
- Date: 2026-07-25, amended 2026-07-30
- Current answer: every namespaced object names its namespace itself (`metadata.namespace`, or
  `namespace:` on a generator entry); a `kustomization.yaml` never has a top-level `namespace:`.
  Static ConfigMaps come from a `configMapGenerator`, so a content change renames them and rolls
  the pod; Secrets with fixed names (SOPS, CNPG, Reflector) roll their consumers through
  Reloader, opted in on the workload's own metadata.

## Context

- The top-level `namespace:` transformer force-overrides every object, including ones
  deliberately placed elsewhere, such as a database role in the operator's namespace.
- A plain ConfigMap updated in place leaves running pods on the old content; a rotated Secret
  never reaches an env var of a running container.

## Decision

- **Explicit namespaces** (2026-07-25), converted where the transformer was used; the rendered
  output was identical.
- **Generated ConfigMaps** (2026-07-30): the hash in the name changes the pod template, so the
  rollout is atomic with the change and depends on no controller.
- **Reloader for Secrets** (2026-07-30): opt-in per workload. The annotation sits on the
  workload, not the pod template, where Reloader does not read it.

## Consequences

- Substituted configs cannot use a `secretGenerator` and need `as-secret` with
  `disableNameSuffixHash` ([0038](0038-plaintext-config-substituted-secrets.md)), so they roll
  through Reloader, not through a hash.
- In a `configMapGenerator` source, comments are shipped config: editing them changes the hash
  and restarts the workload ([0037](0037-comments-record-traps.md)).

## References

- `da7e30ec`, `ac105f95`, `b0a14644`, `8b576c4d`.
- README: [Namespacing](../../README.md#namespacing).
