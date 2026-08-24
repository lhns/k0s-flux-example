# miniflux

Miniflux (`miniflux/miniflux`, minimalist RSS reader), migrated from Docker Swarm. Public at
`miniflux.example.com` with its **built-in login**; `miniflux.example.net` and `miniflux.kube.example.com`
redirect to the canonical host.

## Shape
- **Stateless** — all state lives in Postgres, so there is **no data volume**. Just a Deployment +
  Service + a dedicated `miniflux` role/database in the shared CNPG cluster (`infra/postgres`).
  `DATABASE_URL` is assembled with `$(MINIFLUX_DB_PASSWORD)` + `sslmode=disable`; `RUN_MIGRATIONS=1`
  applies forward schema migrations on startup.
- **Auth**: Miniflux's own multi-user login — **not** behind Authelia (forwardAuth would break its
  REST API / mobile clients, which authenticate with Miniflux tokens). `CREATE_ADMIN`/`ADMIN_PASSWORD`
  from the compose are dropped — the migrated DB already has all users.
- **Config is env-only**; the only secret is the DB password (reflected CNPG role secret) — no
  `app-secret.yaml`. `BASE_URL` (the canonical host) drives links, the PWA, and the API.
- **Updates**: stateless, so `strategy: RollingUpdate` (`maxUnavailable: 0` / `maxSurge: 1`) =
  start-first, zero-downtime.
- **kube-vnet**: `net.traefik.traefik: ingress` + `net.postgres.postgres: egress` (feed fetching is
  external egress). `miniflux.example.net` gets its own cert via IONOS DNS-01 (`letsencrypt-ionos-prod`).

## One-time migration
1. Deploy with the Deployment at `replicas: 0` (db/role + secret come up).
2. **Verify** the source DB at `10.20.2.10:5436` is actually miniflux (same host port the bitwarden
   stack used), then `pg_dump` `postgresql://miniflux:<pw>@10.20.2.10:5436/miniflux` into the CNPG
   `miniflux` db (as the `miniflux` role), `postgres-rw.postgres.svc.cluster.local:5432`,
   `sslmode=disable`. Same major (PG 18 both sides).
3. Flip the Deployment to `replicas: 1`.
4. Point `miniflux.example.com` (+ `miniflux.kube.example.com`, `miniflux.example.net`) DNS at the Traefik LB
   `10.20.2.15`.
