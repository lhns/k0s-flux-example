# 0052 — Flux alerts go to Matrix

- Status: Accepted
- Date: 2026-10-08, amended 2026-10-26
- Current answer: Flux `error` events for the alerting pipeline's components and the
  flux-system sources go through a notification-controller `Provider` of type `matrix` to an
  unencrypted room, via the `flux-errors` Alert the generator renders from
  `templates/alerts.yaml`. All other Flux objects alert through Grafana (0065).

## Context

Flux failures were invisible: no alerting existed.

## Decision

- notification-controller `Provider` of type `matrix` posts to a room as `@flux:example.com`,
  authenticated with a MAS compatibility token. The room is unencrypted.
- The `flux-errors` Alert (error severity) is rendered by the generator
  (`templates/alerts.yaml`, opt-in via `alerts`), because an eventSource matches a namespace
  exactly with no wildcard and each component's Kustomizations live in their own `gen-*`
  namespace.
- **Narrowed** (2026-10-26): the Alert covers only `alerts.components` (the components Grafana
  alerting depends on, which cannot report their own failure) and the flux-system sources. All
  other Flux objects alert through Grafana `flux-not-ready` (ADR 0065), which repeats daily and
  sends a resolved message.
- Email was rejected: Flux has no SMTP provider, so it would need Alertmanager.

## Consequences

- The token needs manual rotation (`infra/flux-alerts/README.md`).
- The room must stay unencrypted: Flux cannot do E2EE.
- A component outside `alerts.components` needs no Alert change; a typo in the list fails the render.
- A one-off admission-webhook `EOF` is excluded (it clears on the next retry); refused or
  timed-out webhook calls still alert. If an EOF ever persists, only the stuck Kustomization's
  `Ready` shows it.

## References

- `6eb4f62d`, `8a904c07`.
