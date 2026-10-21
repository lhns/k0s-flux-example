# 0052 — Flux alerts go to Matrix

- Status: Accepted
- Date: 2026-10-08
- Current answer: Flux `error` events go through a notification-controller `Provider` of type
  `matrix` to an unencrypted room, via the `flux-errors` Alert the generator renders from
  `templates/alerts.yaml`.

## Context

Flux failures were invisible: no alerting existed.

## Decision

- notification-controller `Provider` of type `matrix` posts to a room as `@flux:example.com`,
  authenticated with a MAS compatibility token. The room is unencrypted.
- The `flux-errors` Alert (error severity) is rendered by the generator
  (`templates/alerts.yaml`, opt-in via `alerts`), because an eventSource matches a namespace
  exactly with no wildcard and each component's Kustomizations live in their own `gen-*`
  namespace.
- Email was rejected: Flux has no SMTP provider, so it would need Alertmanager.

## Consequences

- The token needs manual rotation (`infra/flux-alerts/README.md`).
- The room must stay unencrypted: Flux cannot do E2EE.
- Adding a component needs no Alert change; the generator lists its namespace.
- A one-off admission-webhook `EOF` is excluded (it clears on the next retry); refused or
  timed-out webhook calls still alert. If an EOF ever persists, only the stuck Kustomization's
  `Ready` shows it.

## References

- `6eb4f62d`, `8a904c07`.
