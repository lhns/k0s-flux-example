# guacamole

Apache Guacamole (clientless RDP/VNC/SSH gateway), migrated from Docker Swarm.
Public at `guacamole.kube.example.com` (auth via **OpenID SSO / Authelia**); the old
`guacamole.example.com` redirects here.

## Shape
- **guacamole** (web app) + **guacd** (proxy daemon, :4822) — two Deployments. guacd
  is stateless; guacamole reaches it over a dedicated kube-vnet channel (`vnet.yaml`).
  Both sides join via pod labels (`kube-vnet/net.guacd: ingress` on guacd, `egress`
  on guacamole — bare `net.<name>` since the vnet is in the same namespace) — no
  separate binding CRs needed since we own both pods.
- **Database**: a dedicated `guacamole` role + database in the shared CNPG cluster
  (`infra/postgres`) instead of MariaDB. `database.yaml` = CRDs; `db-role-secret.yaml`
  = SOPS/Reflector password.
- **Config is env-only** — the image bundles the JDBC-postgres and OpenID SSO
  extensions and configures both from `POSTGRESQL_*` / `OPENID_*` env vars, so there
  is **no `GUACAMOLE_HOME` volume and no extension jar** to manage (the Swarm `home/`
  is fully replaced by env). Images pinned to `1.6.0` (webapp, guacd, bundled
  extensions must match).
- **Routing**: `guacamole-prefix` addPrefix `/guacamole` (the webapp serves under the
  `/guacamole` context; addPrefix lets users hit the clean root URL) + the canonical
  IngressRoute + the `guacamole.example.com` redirect.

## One-time migration (schema + data)
1. Deploy with the guacamole Deployment at `replicas: 0` (guacd + DB come up).
2. **Load the schema**: `initdb.sh --postgresql | psql` into the empty CNPG `guacamole`
   db (one-shot Job).
3. **Migrate data**: pgloader from the old MariaDB into the pre-created schema
   (data-only; casts tinyint→bool / binary→bytea; reset the `*_id` sequences).
4. Flip the Deployment to `replicas: 1`.
5. On Authelia, point the `guacamole` OIDC client's redirect_uri at
   `https://guacamole.kube.example.com`.

## Notes
- OIDC is a public/PKCE client (no client secret). Username claim = `preferred_username`;
  migrated `guacamole_entity.name` values already use it, so permissions carry over.

## The white-screen failure mode (2026-08-06)

Guacamole served a **white page** for ~15 hours while Kubernetes reported the pod `1/1 Ready`.

**What happened.** After an overnight Postgres disruption the JDBC pool wedged with zero usable
connections — it logged `PooledDataSource -- Execution of ping query 'SELECT 1' failed` and never
recovered. Requests needing the database then **blocked forever** instead of erroring:

| request | needs DB | result |
| --- | --- | --- |
| `GET /`, `/api/languages`, `/api/patches`, all assets | no | 200 |
| `POST /api/tokens` | yes | **hung until timeout** |

So the app shell loaded and the token exchange never returned — a white page. A browser HAR showed
67 `POST /api/tokens` with status `0` and no `serverIPAddress`, which looks like the browser
blocking the request; `curl` reproduced it (`We are completely uploaded and fine` → timeout),
proving it was the server hanging, not the client.

**Restarting the pod clears it.** The pool does not self-heal.

Two changes came out of it:

- **`REMOTE_IP_VALVE_*`** — Tomcat was reporting the *traefik pod* as the client, so the bundled
  brute-force extension banned `172.18.1.155`. Five failed logins by anyone locked out everyone.

  `internalProxies` lists **both** hops in front of guacamole, because Tomcat walks
  `X-Forwarded-For` right to left and stops at the first address it does not recognise:
  `172.18.0.0/16` for the traefik pods, and `10.20.2.31/.32/.33` for the docker swarm nodes running
  the edge traefik.

  Three `/32`s, deliberately — *not* `10.20.2.0/24`, and not Tomcat's default, which covers `10/8`.
  LAN clients reach the VIP directly and arrive from `10.20.2.0/24` themselves, so anything broader
  classifies a real client as a proxy, walks past it, and falls back to the traefik pod —
  reintroducing the bug.

  **Correction, 2026-08-15.** This file used to claim external users sharing one identity was
  "edge/NAT topology, not something Guacamole can fix". That was wrong, and it cost a second
  investigation when jellyfin showed the same `10.20.2.33` for every external user. There was no
  NAT involved: the *cluster* traefik had no `forwardedHeaders.trustedIPs`, and traefik v3's
  default is not "ignore the inbound header" but "discard it and rewrite `X-Forwarded-For` with
  the peer address". So the edge's header — which carried the real client all along — was being
  thrown away one hop short. Fixed in `infra/traefik`; the swarm nodes above are the other half.

- **A DB-aware `livenessProbe`** — the readiness probe hits `GET /guacamole/`, which never touches
  the database, which is precisely why nothing noticed. Detecting this needs a POST, so the probe
  is `exec` + `curl`. **Any HTTP response counts as healthy** (403 and 429 included); only a
  timeout — `curl` exit 28 — restarts the pod.

  *Trade-off:* if Postgres is genuinely down, this restarts about every 3 minutes until it
  returns. Noisy, but the pool cannot recover on its own and a restart is the only remediation.

**Debugging note:** `gitea`-style `su`-less `kubectl exec ... sh -c 'echo > /dev/tcp/...'` does not
work here — the image's `/bin/sh` is dash, which has no `/dev/tcp`. `curl`, `wget` and `bash` are
all present; use those.
