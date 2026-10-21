# matrix-alerts

Relays Grafana alerts to the Matrix room "Grafana alerts" as `@grafana:example.com`,
using [metio/matrix-alertmanager-receiver](https://github.com/metio/matrix-alertmanager-receiver).
Grafana has no Matrix contact point.

Decisions: a room of its own, unencrypted (the receiver does no E2EE), like the Flux alerts room
(ADR 0052). The contact points are provisioned from `apps/grafana`; rules are `GrafanaAlertRuleGroup` CRs in
the components that produce their metrics (ADR 0058); notification policies stay in the Grafana UI. Alerts about the telemetry pipeline
itself also go by email (`matrix-and-email`), so they arrive when Matrix is down.

Flow: Grafana contact point `matrix` (webhook, Alertmanager JSON) -> `matrix-alerts:8080/alerts/grafana`
-> `matrix.example.com`. `grafana` maps to the room ID in `config.yaml`.

- `config.yaml`: ConfigMap via `configMapGenerator`; comments in it are shipped config. The receiver
  substitutes `${MATRIX_ACCESS_TOKEN}` from the env. Templates avoid `.SilenceURL` (it points at an
  Alertmanager) and link the rule via `.GeneratorURL`.
- `secret.yaml`: SOPS `matrix-alerts`, key `MATRIX_ACCESS_TOKEN`, a MAS compatibility token for
  `@grafana:example.com`.
- No Traefik route and no webhook auth: the vnet ingress restriction (only the Grafana pod carries
  `net.matrix-alerts.matrix-alerts: egress`) is the protection.

The room must stay unencrypted: the receiver does no E2EE. `@grafana:example.com` created it and must stay a member.

Firing warning and critical alerts mention `@admin:example.com`; resolved and `info` messages mention nobody.
For the phone to buzz only on firing, set the room's notifications to "mentions and keywords" in the client.

Grafana's contact point `matrix` is provisioned from git, and rules live beside the components they
watch; see `apps/grafana/README.md`.

Rotate the token in the `mas` pod, piped straight into sops on the jumphost, never displayed:
`mas-cli manage issue-compatibility-token grafana GRAFANAALERTS` (`MAS_CONFIG` supplies the config).
