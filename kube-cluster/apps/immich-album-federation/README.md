# immich-album-federation

Headless sync worker (`ghcr.io/lhns/immich-album-federation`) that reconciles albums between Immich
peers on an interval. Migrated from Docker Swarm.

## Shape
- **No ingress, no Service** — it is a background worker, not a server. It only makes outbound
  connections: to the CNPG database (sync state) and to each peer's HTTP API (`immich.example.com`).
- **Database**: a dedicated `immich-album-federation` role + database in the shared CNPG cluster
  (`infra/postgres`) instead of the Swarm-bundled Postgres. The Swarm DB is **not** migrated — there
  was no prior sync state — so the db starts empty and the worker migrates its own schema in on first
  run. `database.yaml` = CRDs; `db-role-secret.yaml` = SOPS/Reflector password.
- **Config** (`config-secret.yaml`): the peer list + API keys live in a SOPS-encrypted Secret mounted
  read-only at `/config/config.yaml` (`IMMICH_SYNC_CONFIG`). The DB password comes from the reflected
  db secret via env; everything else is plain env.
- **kube-vnet**: only `kube-vnet/net.postgres.postgres: egress` (reach the CNPG primary). No traefik
  label — nothing connects *to* the worker; its HTTPS to the peers is external egress, unrestricted
  under the ingress-only baseline, so no VirtualNetwork CR is needed.
- Single replica, `strategy: Recreate` — a singleton reconciler must never run two copies at once.

## Notes
- `DRY_RUN` previews actions without writing; kept `false` (the tested Swarm config). Flip to `true`
  for one cycle if a reconcile ever looks wrong.
- `IMMICH_SYNC_REARM` (commented) re-arms the worker after a circuit-breaker quarantine — paste the
  one-shot key from the logs; it is consumed on use.
