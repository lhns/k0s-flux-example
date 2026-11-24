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
- One message per Grafana group: the relay templates per alert and has no group variable, so Grafana's
  `matrix-payload` template sends one pseudo-alert per status (`labels.count`, instance summaries in annotations
  `i0000`...) and `firing-template` lists them. Change the template and the payload together;
  `apps/grafana/tests` renders both. Without `labels.count` the title carries the number (rules that
  could not be evaluated) and `firing-template` omits "N instances". The relay builds the plain `body`
  from the HTML, so it carries Markdown (`**FIRING**`, `[Rule](url)`) but no entities or tags.
- `secret.yaml`: SOPS `matrix-alerts`, key `MATRIX_ACCESS_TOKEN`, a MAS compatibility token for
  `@grafana:example.com`.
- No Traefik route and no webhook auth: the vnet ingress restriction is the protection. Only the Grafana pod
  (`net.matrix-alerts.matrix-alerts: egress`) and the otel collectors, which scrape `/metrics` through
  `otel-scrape`, reach the port.

The room must stay unencrypted: the receiver does no E2EE. `@grafana:example.com` created it and must stay a member.

Only firing critical alerts mention `@admin:example.com` ([ADR 0072](../../../docs/adr/0072-only-critical-alerts-mention.md));
warning, `info` and resolved messages mention nobody. Set the room's notifications to "mentions and keywords"
in the client, so the phone buzzes only for those.

Grafana's contact point `matrix` is provisioned from git, and rules live beside the components they
watch; see `apps/grafana/README.md`.

## Alerts

`grafana.yaml`, rule group `app-matrix-alerts`, folder *Cluster*; emailed too (`matrix-and-email`).

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `matrix-alerts-dropping` | the relay's `invalid_payload`, `templating_failure` or `send_failure` counters rose in 30m | 0 | critical |

Not backtested: the relay was not scraped before this rule.

Rotate the token in the `mas` pod, piped straight into sops on the jumphost, never displayed:
`mas-cli manage issue-compatibility-token grafana GRAFANAALERTS` (`MAS_CONFIG` supplies the config).
