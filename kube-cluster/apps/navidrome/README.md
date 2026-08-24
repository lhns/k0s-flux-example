# navidrome

[Navidrome](https://www.navidrome.org/) music server at **`navidrome.example.com`**,
ported from a Docker Swarm stack.

- `resources.yaml` — namespace, volumes, Deployment, Service.
- `routing.yaml` — the Traefik IngressRoutes + subsonic middlewares (routing needs a
  query match that native Ingress can't express, so it's CRDs, not an Ingress).

## Storage

- **`navidrome-data`** — `ceph-rbd` (block, RWO). Navidrome's SQLite DB + artwork/
  cache; block because SQLite is a single writer. Migrated once (1.5 GB) from the
  old Swarm `/docker/navidrome` dir with Swarm navidrome stopped for a consistent
  snapshot.
- **`navidrome-music`** — a **static** CephFS PV on the **`archive`** filesystem
  (`fsName: archive`, `rootPath: /media/music/library`), mounted **read-only** at
  `/music`. Same Ceph cluster as everything else, so `clusterID: ceph-cluster`.

## Auth

Navidrome does no auth of its own — the external **Authelia** does, and navidrome
trusts the `Remote-User` header it injects (`ND_EXTAUTH_USERHEADER`,
`ND_EXTAUTH_TRUSTEDSOURCES: 0.0.0.0/0` — safe only because kube-vnet ingress
isolation means nothing but Traefik can reach `:4533`). Two routers:

- **`navidrome`** (IngressRoute) — web UI + API behind the `authelia` forwardAuth
  middleware (browser SSO).
- **`navidrome-subsonic`** (IngressRoute, higher priority) — Subsonic API clients
  hitting `/rest/` that aren't the web app (`!Query(c, NavidromeUI)`). The
  `subsonic-basicauth` Traefik plugin converts their Subsonic auth params into a
  Basic header, `authelia-basicauth` validates it, then `navidrome-authcleanup`
  strips the `Authorization` header before navidrome sees it. Web-app subsonic calls
  (`c=NavidromeUI`) fall through to the session-cookie router.

Both IngressRoutes reference the Authelia middlewares in the `authelia` namespace,
which requires `providers.kubernetesCRD.allowCrossNamespace: true` and the
`subsonic-basicauth` plugin (`github.com/crazygolem/traefik-subsonic-basicauth`),
both configured in `infra/traefik`.

`dependsOn: app-authelia` (Traefik CRDs + plugin, and the Authelia middlewares).
Runs as root (`user: root` in Swarm) so both volumes are accessible.
`image: deluan/navidrome:latest` — pin to a released tag/digest if you want
reproducibility.
