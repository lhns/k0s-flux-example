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
- *Contact points* `matrix` (a webhook to the `matrix-alerts` relay) and `matrix-and-email` (the same
  webhook plus email via the SMTP config) are file-provisioned (`alerting-contactpoints.yaml`, read-only
  in the UI). The file holds no secret, and moving one to a CR would mean deleting a receiver the
  routes use.
- The *notification policy tree* stays in the UI; rules route straight to a contact point through
  `notificationSettings`, so they need no policy.
- *Rules* are `GrafanaAlertRuleGroup` CRs in the `grafana.yaml` of the component that produces
  their metrics (tables and backtests in its README), and the CRs reach Grafana through its API, so the
  UI shows them as provisioned. Rules made in the UI stay.

To add a rule group, in that component's `grafana.yaml`, copy a group from
[infra/traefik](../../infra/traefik/grafana.yaml):
- name `<flux name>[-suffix]` as both `metadata.name` and `spec.name`; `folderUID` from the table under
  [Operator](#operator); `interval`; a unique rule `uid`;
- `notificationSettings.receiver`: `matrix`, or `matrix-and-email` for rules the alerting pipeline itself
  depends on;
- `execErrState: KeepLast` on per-object rules keeps their state through a datasource outage, so only
  the pipeline-health rules (`Error`) report it; `noDataState: OK` unless NoData is the failure;
- backtest the query over 7 days in Explore before adding it, and record the result in the README table.

Not added: log-based rules for mautrix bridge states and selected Kubernetes Warning events. They need the
VictoriaLogs datasource uid, which lives in Grafana's database and is not in the repo.

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
