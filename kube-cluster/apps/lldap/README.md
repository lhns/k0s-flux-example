# lldap

Light LDAP (`lldap/lldap`) directory server — the LDAP backend for Authelia and mosquitto — migrated
from Docker Swarm into its own namespace. Web UI at `lldap.example.com` (behind Authelia SSO).

## Shape
- **Env-only config** — the old `lldap_config.toml` was placeholder defaults overridden by env, so
  there's no config file. `LLDAP_LDAP_BASE_DN=dc=lhns,dc=de`; `LLDAP_JWT_SECRET` + **`LLDAP_KEY_SEED`**
  (SOPS `lldap-secrets`, from the old deployment — KEY_SEED derives the key that decrypts stored data,
  so it must be exact); `LLDAP_DATABASE_URL` → the shared CNPG cluster (`sslmode=disable`, password
  `$(VAR)`-interpolated from the reflected `lldap-db` secret).
- **Data**: the `lldap` database (users/groups/memberships) in CNPG, migrated 1:1 from the old Swarm
  Postgres via `pg_dump`. `/data` is a throwaway emptyDir (nothing persisted there with a DB backend).
- **kube-vnet**: `net.postgres.postgres: egress` (CNPG) + `net.traefik.traefik: ingress` (web UI) + a
  `lldap` VirtualNetwork (`vnet.yaml`) that Authelia + mosquitto join (egress) to reach LDAP `:3890`.
  In-cluster only — no external LDAP port.
- Single replica, `strategy: Recreate`.

## One-time migration
1. Deploy at `replicas: 0` (db/role + secrets come up).
2. `pg_dump` the `lldap` db from the old Swarm Postgres (`10.20.2.10:5437`) into the CNPG `lldap` db.
3. Flip to `replicas: 1`; verify LDAP + the web UI list the migrated users.
