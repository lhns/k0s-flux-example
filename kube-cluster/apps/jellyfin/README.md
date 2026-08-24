# jellyfin

Jellyfin media server — the `ghcr.io/jpvenson/jellyfin.pgsql` fork, which keeps its
library database in **PostgreSQL** instead of SQLite. Migrated from Docker Swarm.

- **Public** at `jellyfin.kube.example.com` (Jellyfin's own login; no Authelia, matching
  Swarm). TLS via the default `*.kube.example.com` wildcard, so `routing.yaml`'s
  IngressRoute just uses `tls: {}`. `jellyfin-redirect-home` ports the Swarm
  `/web` → `/web/index.html` redirect.
- **Database** — a dedicated `jellyfin` role + database in the shared CNPG cluster
  (`infra/postgres`, PG 18). `database.yaml` holds the `DatabaseRole` + `Database`
  CRDs; `db-role-secret.yaml` is the SOPS-encrypted password (in the `postgres` ns,
  Reflector-mirrored into `jellyfin`). The pod dials `postgres-rw.postgres.svc:5432`
  via the `postgres` kube-vnet channel.

## Storage
The old Swarm `/mnt/{appdata,fastappdata,archive}` paths are CephFS filesystems on
the same `ceph-cluster` (fs names `appdata` / `fastappdata` / `archive`).

| Mount | Source | Volume | Mode |
|---|---|---|---|
| `/config` | fastappdata `.../config/*` | `ceph-rbd` PVC `jellyfin-config` (copied in once) | RW |
| `/config/metadata` | appdata `.../config/metadata` | static CephFS PV (fs `appdata`) | RO |
| `/config/data/trickplay` | appdata `jellyfin/trickplay` | static CephFS PV (fs `appdata`) | RO |
| `/config/subbuzz` | appdata `jellyfin/subbuzz` | static CephFS PV (fs `appdata`) | RO |
| `/jellyfin/jellyfin-web/config.json` | fastappdata `.../config/web/config.json` | ConfigMap `jellyfin-web-config` | RO |
| `/media` | archive `/media` | static CephFS PV (fs `archive`) | RO |

`/config` is a fresh RW block volume (the small ~3.3G fastappdata subtree copied in)
so cache/writes work. The big `appdata` metadata is mounted **read-only for now**
(too large to copy) — existing artwork is served, but new metadata won't persist
until it's copied to RW later.

## One-time migration (with Swarm jellyfin stopped for a consistent snapshot)
1. Deploy with the Deployment at `replicas: 0` (CNPG creates the DB/role, Reflector
   mirrors the secret, PVCs bind).
2. **Copy** fastappdata `.../config` → the `jellyfin-config` RBD PVC (one-shot Job,
   `rsync -a --exclude web`).
3. **Import** the Postgres data: `pg_dump` the old DB at `10.20.2.10:5428` and
   `pg_restore` into the CNPG `jellyfin` database (one-shot Job).
4. Flip the Deployment to `replicas: 1`.

## Client IPs and `KnownProxies` — settings that live outside git

Jellyfin's network settings are in `/config/config/network.xml` on the `jellyfin-config` PVC,
copied from Swarm during the migration above. They are **not** in this repo and Flux will never
reconcile them, so they are recorded here or they are invisible:

```xml
<KnownProxies><string>0.0.0.0/0</string></KnownProxies>
<LocalNetworkSubnets><string>10.20.0.0/16</string></LocalNetworkSubnets>
<EnableRemoteAccess>true</EnableRemoteAccess>
```

External users used to appear as `10.20.2.33`. That was not Jellyfin's doing: the cluster traefik
had no `forwardedHeaders.trustedIPs`, so it discarded the edge's `X-Forwarded-For` and replaced it
with the peer — one of the three swarm nodes running the edge traefik. Fixed in `infra/traefik`,
and Jellyfin needed no change, because `KnownProxies: 0.0.0.0/0` makes it walk the whole chain.

**That `0.0.0.0/0` should be narrowed, and matters more now than it did.** It means Jellyfin
believes whatever is leftmost in `X-Forwarded-For`. While traefik was overwriting the header that
was unreachable; now that the chain is preserved, whether a client can inject a forged entry
depends on how the *edge* traefik is configured — which is outside this repo. Narrow it to
`172.18.0.0/16` plus `10.20.2.31/.32/.33` (Dashboard → Networking → Known proxies), and check the
edge's own `forwardedHeaders` while you are there.

## Caveats
- **appdata is read-only for now** (see above).
- **No GPU** in the cluster yet → CPU-only transcoding. The GPU `nodeSelector` /
  `resources.limits` are shipped commented-out in `resources.yaml`.
