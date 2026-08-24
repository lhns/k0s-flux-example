# bitwarden

Vaultwarden (`ghcr.io/dani-garcia/vaultwarden`, a Bitwarden-compatible server), migrated from Docker
Swarm. Public at `bitwarden.example.com` (its own vault login **plus** OpenID SSO via Authelia); the old
`bitwarden.kube.example.com` redirects here.

## Shape
- **Database**: a dedicated `vaultwarden` role + database in the shared CNPG cluster (`infra/postgres`)
  instead of the Swarm-bundled Postgres. The old DB was already Postgres 18, so it was migrated with a
  same-major `pg_dump`/restore (no schema rework). `database.yaml` = CRDs; `db-role-secret.yaml` =
  SOPS/Reflector password. `DATABASE_URL` is assembled with `$(VW_DB_PASSWORD)` + `sslmode=disable`.
- **Data**: `/data` (attachments, sends, icon cache, `config.json`, and `rsa_key` — the JWT signing
  key) lives on a **CephFS RWX** volume (`bitwarden-data`), copied once from the old Swarm volume
  `fastappdata:/docker/bitwarden` **excluding the nested `db/`** (the source Postgres data dir).
  Copying `rsa_key` keeps issued tokens/sessions valid.
- **Updates**: `strategy: RollingUpdate` with `maxUnavailable: 0` / `maxSurge: 1` = **start-first**
  (matches the Swarm `order: start-first`) — new pod up before old down, zero-downtime. RWX CephFS
  lets both mount `/data` during the brief rollover.
- **Config is env-only**. Secrets (`app-secret.yaml`, SOPS): the OIDC `sso-client-secret` and the
  `push-installation-key`; the DB password comes from the reflected CNPG role secret.
- **OIDC**: confidential Authelia client (`vaultwarden`) — **already registered** in the Authelia
  config with redirect_uris for both `bitwarden.example.com` and `bitwarden.kube.example.com`, PKCE/S256, and
  a policy requiring `group:admin`/`group:bitwarden` + 2FA. `DOMAIN` (the canonical host) drives the
  callback + generated links. Nothing to change on Authelia — just be in `group:bitwarden`/`admin`.
- **/admin** panel: no `ADMIN_TOKEN` set; the panel is protected by the reflected `authelia`
  forwardAuth middleware on a higher-priority `PathPrefix(/admin)` route (see `routing.yaml`).
- **kube-vnet**: `net.traefik.traefik: ingress` + `net.postgres.postgres: egress` (OIDC to
  auth.example.com, the push relay, and icon fetches are external egress).
- The Swarm `smtp` postfix relay is dropped (Vaultwarden had no SMTP config); wire to `msmtpd` later
  if mail is wanted.

## One-time migration
1. Deploy with the Deployment at `replicas: 0` (db/role + secrets come up; PVC binds).
2. **DB**: `pg_dump` the old Swarm Postgres (`10.20.2.10:5436`, db/user `vaultwarden`) into the CNPG
   `vaultwarden` db (as the `vaultwarden` role), `postgres-rw.postgres.svc.cluster.local:5432`,
   `sslmode=disable`. Same major (PG 18 both sides).
3. **Data**: copy `fastappdata:/docker/bitwarden/*` into the `bitwarden-data` PVC, **excluding `db/`**.
4. Flip the Deployment to `replicas: 1`.
5. Point `bitwarden.example.com` (+ `bitwarden.kube.example.com`) DNS at the Traefik LB `10.20.2.15`.
