# grafana

Grafana (`ghcr.io/lhns/grafana-babel`) at **`grafana.example.com`**, ported from a
Docker Swarm stack. Grafana does its own auth (OIDC via Authelia), so **no Authelia
forwardAuth** in front of it.

- `grafana.yaml` — the `Grafana` CR: the Grafana Operator ([infra/grafana-operator](../../infra/grafana-operator/README.md))
  renders the Deployment, Service `grafana-service` and `grafana-ini` from it. See [Operator](#operator).
- `resources.yaml` — a `cephfs` PVC for `/var/lib/grafana` (plugins + renders, not the DB), the
  Ingress and the `grafana.kube.example.com` redirect, both to `grafana-service`.
- `database.yaml` — a dedicated CNPG **`DatabaseRole`** + **`Database`** (`grafana`)
  in the shared postgres cluster, as **standalone CRDs** (no edit to
  `infra/postgres/cluster.yaml`). `reclaimPolicy: retain` so removing the manifest
  never drops the role/DB.
- `db-role-secret.yaml` — the role password (SOPS, `postgres` ns) for the
  DatabaseRole. Annotated so **Reflector** mirrors it into the `grafana` namespace
  for `GF_DATABASE_PASSWORD` — one source in git, no duplicate SOPS file.
- `grafana-config-secret.yaml` — the OIDC client-secret + SMTP password (SOPS,
  `grafana` ns) for `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET` / `GF_SMTP_PASSWORD`.

**Dashboards** are operator `GrafanaDashboard`s owned by the component that produces the metrics, in
its own `grafana.yaml` ([ADR 0060](../../../docs/adr/0060-grafana-objects-in-the-component.md)); none is file-provisioned
any more. *Postgres*: [infra/postgres](../../infra/postgres/README.md#dashboards); *Ceph*:
[external/ceph](../../external/ceph/README.md#dashboards); *Cluster*:
[infra/opentelemetry-monitoring](../../infra/opentelemetry-monitoring/README.md#dashboards) and
[apps/anubis](../../apps/anubis/README.md#dashboard); *CI*: [apps/arc-controller](../../apps/arc-controller/README.md).
The operator overwrites a dashboard at its next resync, so a save in the UI is reverted; a copy made with
*Save as* lives in the database and stays. To keep a change, edit the JSON in git.

To add one, in the component that produces the metrics: put the JSON in `dashboards/`, list
it under a `configMapGenerator` in the component's `kustomization.yaml`, and add a `GrafanaDashboard` to
`grafana.yaml` (name `<flux name>-<uid>`, `folderUID` from the table under [Operator](#operator)), as
`infra/postgres` does. Copy the generator's `options` (stable name, operator label, `substitute: disabled`)
and the same annotation on each CR, and add `grafana.yaml` to `resources:`; no `dependsOn` on the operator
([README](../../../README.md#grafana-objects)). The JSON uses a `datasource` variable of type
`victoriametrics-metrics-datasource`, since the metrics have dotted names (MetricsQL).

**Alerting.**
- *Severity*: `critical` means the owner must act today: something users rely on is down, data is at
  risk, or the alerting or monitoring pipeline is blind; only critical alerts mention the owner
  ([ADR 0072](../../../docs/adr/0072-only-critical-alerts-mention.md)). Anything
  that can wait for the next look at the room is `warning`.
- *Contact points* `matrix` (a webhook to the `matrix-alerts` relay) and `matrix-and-email` (the same
  webhook plus email via the SMTP config) are file-provisioned (`alerting-contactpoints.yaml`, read-only
  in the UI). The file holds no secret, and moving one to a CR would mean deleting a receiver the
  routes use.
- The *notification policy tree* stays in the UI; rules route straight to a contact point through
  `notificationSettings`, so they need no policy.
- *Grouping*: every rule sets `notificationSettings` `group_by: [grafana_folder, alertname]`, `group_wait: 1m`,
  `group_interval: 10m`, `repeat_interval: 24h`, so one rule firing for many instances is one notification.
  Copy the four lines into new rules (the policy tree's defaults do not apply to them).
- *Flapping*: a rule on a state (a level or threshold that holds) sets `keepFiringFor: 15m`, so a condition that
  clears for a few minutes does not resolve and re-fire; an event rule (`for: 0s` on a counter's increase) sets none.
  A threshold rule whose history flapped around its line adds a recovery threshold, `unloadEvaluator` on the
  threshold condition (`traefik-5xx`: fires above 10%, recovers below 7%).
- The webhook contact points use the file-provisioned template `matrix-payload` (`alerting-contactpoints.yaml`):
  it folds a group into one pseudo-alert per status, because the `matrix-alerts` relay renders one Matrix message
  per alert. See [matrix-alerts](../matrix-alerts/README.md).
  - A rule that fails to query (`execErrState: Error`) arrives as alertname `DatasourceError`, one alert per
    rule (labels `rulename`, `datasource_uid`, `ref_id`, the rule's own labels; annotation `Error`), carrying
    its summary rendered without values, so it reads as true. The template reports "<rule> could not be
    evaluated" or "N rules could not be evaluated" with the rule names and the error once, no per-rule text.
  - A no-data alert (`noDataState: Alerting`) keeps the rule's alertname and summary
    (`grafana_state_reason: NoData`, no series labels, no values), so annotations that interpolate a value or
    label use `{{ with $values.A }}...{{ else }}fallback{{ end }}`.
  - A normal group sends `description` and the Rule link only when all its alerts share them.
  - `tests/` renders fixtures through the pinned Grafana and relay images (`bash tests/run.sh`, docker;
    `UPDATE_GOLDEN=1 python3 tests/check.py .` after an intended change). A template that is not valid JSON
    drops every alert.
- *Rules* are `GrafanaAlertRuleGroup` CRs in the `grafana.yaml` of the component that produces
  their metrics (tables and backtests in its README), and the CRs reach Grafana through its API, so the
  UI shows them as provisioned. Rules made in the UI stay.

To add a rule group, in that component's `grafana.yaml`, copy a group from
[infra/traefik](../../infra/traefik/grafana.yaml):
- name `<flux name>[-suffix]` as both `metadata.name` and `spec.name`; `folderUID` from the table under
  [Operator](#operator); `interval`; a unique rule `uid`;
- `notificationSettings.receiver`: `matrix`, or `matrix-and-email` for rules the alerting pipeline itself
  depends on;
- `execErrState: KeepLast`, so a datasource outage is one message, not one per rule: only the rules that report
  the alerting and metrics pipeline itself keep `Error` (`grafana-rule-evaluations-failing`,
  `grafana-notifications-failed`, `matrix-alerts-dropping`, `victoriametrics-component-down`,
  `kafka-exporter-members`); `noDataState: OK` unless NoData is the failure;
- backtest the query over 7 days in Explore before adding it, and record the result in the README table.

Log-based rules query the VictoriaLogs datasource (uid `cens0eitab2m8b`; `queryType: instant` with a
`stats` pipe), as in [apps/matrix](../matrix/grafana.yaml). Not added: rules for mautrix bridge connection
states and selected Kubernetes Warning events.

Rules on Grafana itself (`grafana.yaml`, group `app-grafana`, emailed too; Grafana's `/metrics` is scraped
by the otel `cluster` collector):

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `grafana-notifications-failed` | `grafana_alerting_notifications_failed_total` rose in 30m, per integration | 0 | critical |
| `grafana-rule-evaluations-failing` | over 20% of rule evaluations failed in 15m | 10m | critical |

Not backtested: Grafana was not scraped before these rules. About 4-5% of evaluations fail all the time
(the counter has no rule label and the log does not name the rules), hence the ratio.

Backtest of `execErrState` (14 days of state history): 32 ten-minute buckets saw a rule enter `Error`; with `Error` on
the five pipeline rules only, 5 of them would have sent no message and the two datasource outages would have named
2-3 rules instead of 9.

Grafana's state lives in **Postgres** (`GF_DATABASE_*` → `postgres-rw`, dedicated
`grafana` role/db) rather than SQLite. The pod joins the `postgres` vnet (egress) to
reach the primary and the `traefik` vnet (ingress) to be served.

**Auth:** `GF_AUTH_GENERIC_OAUTH_*` points at the external Authelia OIDC endpoints
(`auth.example.com`); `auto_login` + `use_pkce` on. Group→role mapping: `admin`/
`grafana_admin` → Admin, `grafana_editor` → Editor, else Viewer.

The `/var/lib/grafana` volume and the `grafana` Postgres DB were migrated from the
Swarm stack (dashboards, users, plugins, renders).

`dependsOn: infra-postgres` (the cluster and the CNPG Database/DatabaseRole CRDs), `infra-reflector`
(the role Secret) and `infra-grafana-operator` (the `Grafana` CRD).

## Operator

Grafana is a `Grafana` CR ([ADR 0058](../../../docs/adr/0058-grafana-operator-manages-grafana-and-its-content.md)).
`spec.deployment` is a strategic merge onto the operator's Deployment, so it carries only what
differs: image (`spec.version`), today's `GF_*` env (config stays env; the operator reads nothing
from it), labels, Recreate, probe, resources, the PVC in place of the default `emptyDir`, and the
alerting ConfigMap. `kustomizeconfig.yaml` gives its hash suffix to the CR, so a
content change rolls the pod at once; Secrets referenced from env roll it at the operator's next
resync (10m). `app-grafana` is Ready once the operator has logged in (the generator's health
check on `GrafanaReady`).

- **Login of the operator**: its projected ServiceAccount token (audience `operator.grafana.com`)
  against `[auth.jwt]`, which trusts the cluster's JWKS and only that subject; it gets org Admin. No
  admin password or token is stored anywhere. Users still log in through Authelia.
- **One pod**: `namespaces/resourcequota.yaml` allows one pod in `grafana`, so a new Grafana pod
  starts only once the old one is gone, whatever order Flux and the operator act in. A pod stuck
  Terminating on a lost node holds the slot until force-deleted. A helper pod needs the quota
  raised in git (Flux reverts a `kubectl` edit), and put back after.
- **Never delete the PVC or its PV** (AGENTS.md). The CR does not own it: deleting the CR removes
  the Deployment and Services, not the data.
- Upgrading Grafana: bump `spec.version` (Renovate does, through the `# renovate:` comment).

**Folders** for operator CRs (`folderUID`; the uids were generated by the providers, so they are not
in any JSON). `scripts/check-grafana.py` keeps the same list.

| Folder | uid |
| --- | --- |
| CI | `fg0li6dpvfitce` |
| Cluster | `ag1airdsjen7kc` |
| Postgres | `cg1t0rly9yuioe` |
| Ceph | `dg1tqkgkqcbnke` |

Folders from a repo `GrafanaFolder` (fixed `spec.uid`, not in that list): *Proxmox*, `proxmox`,
[external/proxmox](../../external/proxmox/README.md#dashboards).
