# mealie

Mealie (`ghcr.io/mealie-recipes/mealie`, recipe manager), migrated from Docker Swarm. Public at
`mealie.kube.example.com` (auth via **OpenID SSO / Authelia**); the old `mealie.example.com` redirects here.

## Shape
- **Database**: a dedicated `mealie` role + database in the shared CNPG cluster (`infra/postgres`)
  instead of the Swarm-bundled Postgres. The old DB was already Postgres, so it was migrated with a
  same-major `pg_dump`/restore (no schema rework). `database.yaml` = CRDs; `db-role-secret.yaml` =
  SOPS/Reflector password.
- **Data**: `/app/data` (recipe images, backups, and the `.secret` JWT-signing key) lives on a
  dedicated Ceph RBD volume (`mealie-data`, RWO), copied once from `fastappdata:/docker/mealie/data`.
  Copying `.secret` keeps existing session tokens valid. Mealie runs as PUID/PGID 1000, so the copied
  data is owned 1000:1000.
- **Config is env-only** — DB, options, and OIDC are `*` env vars. Secrets (`app-secret.yaml`, SOPS):
  the OIDC `client-secret` and the OpenAI `api-key`; the DB password comes from the reflected CNPG
  role secret.
- **OIDC**: confidential Authelia client (`mealie`), `AUTO_REDIRECT` + `ALLOW_PASSWORD_LOGIN=false`
  (SSO-only). `OIDC_ADMIN_GROUP=mealie_admin`, `OIDC_USER_GROUP=mealie`. `BASE_URL` (the canonical
  host) drives the callback + generated links.
- **kube-vnet**: `net.traefik.traefik: ingress` + `net.postgres.postgres: egress` (OIDC to
  auth.example.com and OpenAI are external egress). Single replica, `strategy: Recreate` (RWO data volume,
  1000Mi memory limit).

## One-time migration
1. Deploy with the Deployment at `replicas: 0` (db/role + secrets come up).
2. **DB**: `pg_dump` the old Swarm Postgres (`10.20.2.10:5436`) into the CNPG `mealie` db (as the
   `mealie` role). Same Mealie version both sides, so alembic is a no-op on start.
3. **Data**: copy `fastappdata:/docker/mealie/data` into the `mealie-data` RBD PVC (chown 1000:1000).
4. Flip the Deployment to `replicas: 1`.
5. On Authelia, point the `mealie` OIDC client's redirect_uri at the `mealie.kube.example.com` host and
   ensure the `mealie` / `mealie_admin` groups are assigned.
