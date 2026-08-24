# authelia

Authelia — SSO portal, **forwardAuth** middleware, and **OIDC provider** for the cluster — migrated
from Docker Swarm into the cluster. Public at `auth.example.com`. Everything SSO-protected depends on this.

## Shape
- **Config** is a plaintext ConfigMap (`configuration.yml` via `configMapGenerator`) — the only inline
  secret (the Gmail SMTP password) was moved out to the `msmtpd` relay, and the OIDC HMAC/JWKS are
  `{{ secret "/secrets/oidc/..." }}` template refs (`X_AUTHELIA_CONFIG_FILTERS=template`). Edits roll
  the pod (config hash + Stakater Reloader). Only the hosts were changed from the Swarm config:
  LDAP → `lldap.lldap.svc`, storage → `postgres-rw.postgres.svc`, redis → `redis` (same ns), SMTP →
  `msmtpd.msmtpd.svc:2500`. All 4 session-cookie domains kept.
- **Secrets** (`secret.yaml`, SOPS) mounted at `/secrets` (JWT/SESSION/STORAGE_ENCRYPTION_KEY/
  LDAP_PASSWORD + oidc HMAC/JWKS), migrated from the old deployment **unread**. The DB password comes
  from the reflected CNPG `authelia-db` secret (mounted at `/secrets/STORAGE_PASSWORD`).
- **Storage**: the `authelia` database in the shared CNPG cluster, migrated 1:1 from the old Swarm
  Postgres (`pg_dump`); stays decryptable via the preserved `STORAGE_ENCRYPTION_KEY`.
- **Sessions**: a single in-namespace **redis** (RBD PVC so sessions survive restarts) — Authelia
  restarts never drop logins. Fresh at cutover.
- **Auth backend**: lldap (`net.lldap.lldap: egress`). **Mail**: msmtpd relay (`net.msmtpd.msmtpd:
  egress`). **redis**: `net.redis` (intra-ns vnet).
- **user-info**: the `/info` page (group→app access matrix), behind the authelia middleware.
- **forwardAuth**: the reflected `authelia` / `authelia-basicauth` Middlewares (`routing.yaml`) protect
  every app. **At cutover** their address flips from the external `10.20.2.10:9091` → the in-cluster
  `authelia.authelia.svc:9091` (a single Traefik→Authelia hop, so the header-stripping issue that
  required the direct port on Swarm is gone).

## Cutover (do AFTER the in-cluster Authelia is verified healthy)
1. Deploy at `replicas: 0`; migrate the `authelia` db; scale to 1; verify `/api/health` + an OIDC + a
   forwardAuth flow via `--resolve` to the cluster LB.
2. Flip the reflected Middleware address to the in-cluster service (a one-line edit), and point
   `auth.example.com` / `lldap.example.com` DNS → the Traefik LB `10.20.2.15`; stop the Swarm auth stack.
   Rollback = revert the Middleware address to `10.20.2.10:9091` (Swarm stays warm until you stop it).
