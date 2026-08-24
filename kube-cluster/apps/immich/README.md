# immich

Immich (`ghcr.io/immich-app/immich-server`, self-hosted photo/video management), migrated from Docker
Swarm. Public at `immich.example.com` (immich's own login + OIDC via Authelia). This is the repo's largest
app: a huge mounted media library plus a dedicated Postgres.

## Shape
- **Machine-learning stays on Swarm.** No in-cluster ML pod — `immich-server` calls the Swarm ML
  directly: `IMMICH_MACHINE_LEARNING_URL=http://10.20.2.10:3003`.
- **Media/library is MOUNTED, not copied.** The Swarm dirs live on CephFS (`appdata`/`fastappdata`/
  `userdata` on the same Ceph the cluster uses), surfaced as **static CephFS PVs** (jellyfin/navidrome
  pattern) — zero new storage for the (huge) library:
  | Mount | fs | rootPath | mode |
  |---|---|---|---|
  | `/usr/src/app/upload` | appdata | `/docker/immich/upload` | RW (immich's library) |
  | `/mnt/userdata` | userdata | `/` | RO (external library) |
  | `/sftpgo` | appdata | `/docker/sftpgo/data/data` | RO (external library) |
  Runs as **root, no fsGroup** (fsGroup would recursively chown the whole library on every start).
- **Dedicated Postgres.** The shared CNPG (plain PG18) can't host immich's `vectorchord`/`pgvecto.rs`
  extensions, so immich runs its **own** `ghcr.io/immich-app/postgres:14-vectorchord…` on a new
  **ceph-rbd** PVC (`immich-db`, 15Gi) — the sole standalone DB in the repo. The DB (~1.8G) is copied
  in via `pg_dump`/restore. `PGDATA` is a subdir so a fresh RBD volume's `lost+found` doesn't block
  initdb.
- **Redis/valkey** (`valkey:8`) — ephemeral emptyDir (job queue/cache; matches Swarm's no-volume).
- **kube-vnet**: `net.traefik.traefik: ingress` (frontend :2283) + intra-namespace vnets for
  postgres/redis. ML + OIDC are external egress.
- **Auth/OIDC**: immich's own login; the Authelia OIDC client + immich's provider settings already
  exist (settings live in the DB) — no Authelia change, no forwardAuth middleware.

## One-time migration (coordinated cutover)
1. Deploy with `immich-server` at `replicas: 0` (namespace, static PVs, `immich-postgres` +
   `immich-redis` come up).
2. **DB**: `pg_dump` the Swarm Postgres (`10.20.2.10:5434`, db/user `immich`) → restore into
   `immich-postgres` (same PG14+vector image both sides → clean restore).
3. **Stop the Swarm immich** (server + workers) — single writer on the upload library + DB.
4. Flip `immich-server` to `replicas: 1` (immich v3 runs its migrations, connects DB + redis + the
   Swarm ML).
5. Point `immich.example.com` (+ `.kube`) DNS at the Traefik LB `10.20.2.15`.

Note: the ML dependency means the Swarm host must keep running the ML service; migrate it later.
