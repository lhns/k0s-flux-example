# authelia

Authelia — SSO portal, **forwardAuth** middleware, and **OIDC provider** for the cluster — migrated
from Docker Swarm into the cluster. Public at `auth.example.com`. Everything SSO-protected depends on this.

## Shape
- **Config** is a plaintext ConfigMap (`configuration.yml` via `configMapGenerator`) — the only inline
  secret (the Gmail SMTP password) was moved out to the `msmtpd` relay, and the OIDC HMAC/JWKS are
  `{{ secret "/secrets/oidc/..." }}` template refs (`X_AUTHELIA_CONFIG_FILTERS=template`). Edits roll
  the pod (config hash + Stakater Reloader). Only the hosts were changed from the Swarm config:
  LDAP → `lldap.lldap.svc`, storage → `postgres-rw.postgres.svc`, session store → valkey (same ns), SMTP →
  `msmtpd.msmtpd.svc:2500`. All 4 session-cookie domains kept.
- **Secrets** (`secret.yaml`, SOPS) mounted at `/secrets` (JWT/SESSION/STORAGE_ENCRYPTION_KEY/
  LDAP_PASSWORD + oidc HMAC/JWKS), migrated from the old deployment **unread**. The DB password comes
  from the reflected CNPG `authelia-db` secret (mounted at `/secrets/STORAGE_PASSWORD`).
- **Storage**: the `authelia` database in the shared CNPG cluster, migrated 1:1 from the old Swarm
  Postgres (`pg_dump`); stays decryptable via the preserved `STORAGE_ENCRYPTION_KEY`.
- **Sessions**: **valkey + sentinel**, 3 replicas, hand-rolled (`valkey.yaml`) — Authelia restarts
  never drop logins. Authelia is a Sentinel *failover client*: it asks a sentinel which node is
  master and writes there, so a sentinel with a stale view sends it to a replica and every write
  fails `READONLY`. Each sentinel rebuilds its config and rediscovers the master on every start
  (`start-sentinel.sh`) precisely so that view cannot get stuck; the script says why.
- **Auth backend**: lldap (`net.lldap.lldap: egress`). **Mail**: msmtpd relay (`net.msmtpd.msmtpd:
  egress`). **valkey**: the `valkey` and `valkey-internal` vnets (intra-ns), bound by
  `valkey-clients` and `valkey-peers`.
- **Rate limits**: Authelia keys its buckets on the source IP alone, and LAN clients reach
  `auth.example.com` hairpinned through the router, so all OIDC clients land in one bucket — one
  client looping on a rejected token refresh rate-limits every other client's logins. The
  `openid_connect_token` buckets in `configuration.yml` are sized for that shared bucket. Genuine
  per-client isolation would need split-horizon DNS on the router (`auth.example.com` → the Traefik VIP
  `10.20.176.210`), which is outside this repo.
- **user-info**: the `/info` page (group→app access matrix), behind the authelia middleware.
- **forwardAuth**: the reflected `authelia` / `authelia-basicauth` Middlewares (`routing.yaml`) protect
  every app, at the in-cluster `authelia.authelia.svc:9091` (a single Traefik→Authelia hop).
