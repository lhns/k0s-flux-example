# homebox

Homebox (`ghcr.io/sysadminsmedia/homebox`, home inventory manager), migrated from Docker Swarm.
Public at `homebox.kube.example.com` (auth via **OpenID SSO / Authelia**); the old `homebox.example.com`
redirects here.

## Shape
- **Database**: a dedicated `homebox` role + database in the shared CNPG cluster (`infra/postgres`)
  instead of the Swarm-bundled Postgres. The old DB was already Postgres, so it was migrated with a
  same-major `pg_dump`/restore (no schema rework). `database.yaml` = CRDs; `db-role-secret.yaml` =
  SOPS/Reflector password.
- **Data**: `/data` (uploaded attachments/images) lives on a dedicated Ceph RBD volume
  (`homebox-data`, RWO), copied once from the old Swarm volume `fastappdata:/docker/homebox/data`.
- **Config is env-only** — DB, options, and OIDC are all `HBOX_*` env vars. Secrets
  (`app-secret.yaml`, SOPS): the API-key `pepper` and the OIDC `client-secret`; the DB password comes
  from the reflected CNPG role secret. Everything else is plain env.
- **OIDC**: confidential Authelia client (`homebox`), `AUTO_REDIRECT` + `ALLOW_LOCAL_LOGIN=false` so
  it's SSO-only. `HBOX_OPTIONS_HOSTNAME` drives the callback URL, so it must match the canonical host.
- **kube-vnet**: `net.traefik.traefik: ingress` + `net.postgres.postgres: egress` (OIDC to
  auth.example.com is external egress). Single replica, `strategy: Recreate` (RWO data volume).

## One-time migration
1. Deploy with the Deployment at `replicas: 0` (db/role + secrets come up).
2. **DB**: `pg_dump` the old Swarm Postgres (`10.20.2.10:5437`) into the CNPG `homebox` db (as the
   `homebox` role). Same homebox version both sides, so no re-migration on start.
3. **Data**: copy `fastappdata:/docker/homebox/data` into the `homebox-data` RBD PVC.
4. Flip the Deployment to `replicas: 1`.
5. On Authelia, point the `homebox` OIDC client's redirect_uri at the `homebox.kube.example.com` host.
