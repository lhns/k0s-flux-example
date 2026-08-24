# firefox-sync

Self-hosted Firefox Sync server (`syncstorage-rs` via `lhns/syncstorage-rs-docker`, **postgres
backend**), migrated from Docker Swarm. Public at `firefox-sync.example.com`.

## Shape
- **Stateless server** — all state lives in **two CNPG databases**, `syncstorage_rs` (sync data) and
  `tokenserver_rs` (user→node routing), both owned by the `syncserver` role (`database.yaml`). The
  server **creates its own schema on startup** and **bootstraps the sync-1.5 node** from `SYNC_URL`
  (`RUN_MIGRATIONS` + `INIT_NODE_URL`) — no manual schema/seed step. The old MariaDB (two DBs) was
  migrated 1:1 into the matching PG databases.
- **Auth**: the sync protocol authenticates clients against **Mozilla FxA**
  (`oauth.accounts.firefox.com`, external egress) + Hawk tokens signed with `SYNC_MASTER_SECRET`. No
  Traefik auth middleware.
- **Config is env-only** (`SYNC_*`, double-underscore path vars). Secrets (`app-secret.yaml`, SOPS):
  `master-secret` (preserved from the old server) + a generated `metrics-hash-secret`; the DB password
  comes from the reflected CNPG role secret (`db-role-secret.yaml`) and is `$(VAR)`-interpolated into
  the two connection URLs.
- **Domain**: `firefox-sync.example.com` stays canonical (NOT `.kube`) — it's baked into `SYNC_URL`, the
  persisted node URL, and every client's `about:config` (`identity.sync.tokenserver.uri`). No redirect.
- **kube-vnet**: `net.traefik.traefik: ingress` + `net.postgres.postgres: egress`. Single replica.

## One-time migration
1. Deploy; the server creates both schemas + bootstraps the node.
2. `pgloader` the old MariaDB (`10.20.2.10:3307`) per-DB into the matching PG databases (data-only). The
   domain is unchanged, so the migrated node URL already matches `SYNC_URL` — no remap.
3. Firefox clients need no reconfiguration (same `firefox-sync.example.com`); ensure DNS points it at the
   Traefik LB.
