# uptime-kuma

Uptime Kuma (`louislam/uptime-kuma`, status/uptime monitor), migrated from Docker Swarm. Public at
`uptimekuma.kube.example.com` (gated by **Authelia forwardAuth**); the old `uptimekuma.example.com` redirects
here.

## Shape
- **No external database** — Uptime Kuma keeps everything (the embedded **SQLite** `kuma.db`, config,
  uploads) in `/app/data`. That whole volume was copied once from `fastappdata:/docker/uptime-kuma`
  into a dedicated Ceph RBD volume (`uptime-kuma-data`, RWO). No CNPG, no SOPS secrets.
- **Auth**: Uptime Kuma's own login is **disabled** (`disableAuth=true` in its DB), so the external
  **Authelia forwardAuth** (the `authelia` middleware, reflected into every namespace by
  traefik-reflector) is the sole gate. No OIDC client.
- **Version**: pinned to the last **v1** release (`1.23.16`) — the migrated DB is a v1 schema, so we
  stay on v1 rather than trigger v2's one-way schema upgrade. Upgrading to v2 is a separate, deliberate
  step later.
- **kube-vnet**: only `net.traefik.traefik: ingress`. Monitor probes reach their targets over
  unrestricted external egress; a monitor pointing at an in-cluster service behind a kube-vnet would
  need that vnet opened separately.
- Single replica, `strategy: Recreate` (RWO SQLite volume).
- The Swarm `prometheus.*` scrape labels were dropped — there is no in-cluster Prometheus.

## One-time migration
1. Deploy with the Deployment at `replicas: 0` (namespace + RBD PVC come up).
2. Copy `fastappdata:/docker/uptime-kuma` into the `uptime-kuma-data` RBD PVC (includes `kuma.db`).
3. Flip the Deployment to `replicas: 1`.
4. Ensure Authelia allows `uptimekuma.kube.example.com` and DNS points it at the Traefik LB.
