# 0072 — Only critical alerts mention the owner

- Status: Accepted
- Date: 2026-11-23

## Context

Every firing warning mentioned the owner in the alert room, so the phone buzzed for conditions
that could wait, and the ones that could not were lost among them.

## Decision

- The `matrix-alerts` relay mentions `@admin:example.com` only for firing alerts with
  `severity: critical`. Warning, info and resolved messages post without a mention.
- `critical` means the owner has to act today: something users rely on is down, data is at
  risk, or alerting or monitoring is blind. Everything else is `warning`, read when the room is
  next looked at; its daily repeat is the reminder.
- Email (`matrix-and-email`) is unchanged: it covers the alerting pipeline when Matrix is down.

## Consequences

- A rule's severity decides whether anyone is woken, so a new rule picks it by the definition
  above (`apps/grafana/README.md`, "Alerting").
- The client's room setting "mentions and keywords" is what silences the rest.

## References

- `kube-cluster/apps/matrix-alerts/config.yaml` (`firing-template`), `apps/grafana/tests`.
