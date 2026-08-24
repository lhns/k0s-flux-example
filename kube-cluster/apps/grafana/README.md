# grafana

Grafana (`ghcr.io/lhns/grafana-babel`) at **`grafana.kube.example.com`**, ported from a
Docker Swarm stack. Grafana does its own auth (OIDC via Authelia), so **no Authelia
forwardAuth** in front of it.

- `resources.yaml` — the `grafana` namespace, a `cephfs` PVC for `/var/lib/grafana`
  (plugins + renders, not the DB), Deployment, Service, Ingress. All config is via
  `GF_*` env (DB, OIDC, SMTP, panels) — ported from the old `grafana.ini`.
- `database.yaml` — a dedicated CNPG **`DatabaseRole`** + **`Database`** (`grafana`)
  in the shared postgres cluster, as **standalone CRDs** (no edit to
  `infra/postgres/cluster.yaml`). `reclaimPolicy: retain` so removing the manifest
  never drops the role/DB.
- `db-role-secret.yaml` — the role password (SOPS, `postgres` ns) for the
  DatabaseRole. Annotated so **Reflector** mirrors it into the `grafana` namespace
  for `GF_DATABASE_PASSWORD` — one source in git, no duplicate SOPS file.
- `grafana-config-secret.yaml` — the OIDC client-secret + SMTP password (SOPS,
  `grafana` ns) for `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET` / `GF_SMTP_PASSWORD`.

Grafana's state lives in **Postgres** (`GF_DATABASE_*` → `postgres-rw`, dedicated
`grafana` role/db) rather than SQLite. The pod joins the `postgres` vnet (egress) to
reach the primary and the `traefik` vnet (ingress) to be served.

**Auth:** `GF_AUTH_GENERIC_OAUTH_*` points at the external Authelia OIDC endpoints
(`auth.example.com`); `auto_login` + `use_pkce` on. Group→role mapping: `admin`/
`grafana_admin` → Admin, `grafana_editor` → Editor, else Viewer.

The `/var/lib/grafana` volume and the `grafana` Postgres DB were migrated from the
Swarm stack (dashboards, users, plugins, renders).

`dependsOn: infra-postgres` (needs the cluster + the CNPG Database/DatabaseRole CRDs).
