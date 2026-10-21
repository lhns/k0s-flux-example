# outline

[Outline](https://www.getoutline.com/) team wiki.

## Shape
- **Web:** `outlinewiki/outline` on :3000, fronted at `outline.example.com`.
- **DB:** dedicated `outline` role + database in the shared CNPG cluster
  (`postgres-rw.postgres.svc.cluster.local:5432`, `PGSSLMODE=disable`).
- **Cache:** in-namespace ephemeral `outline-redis` (`redis:*-alpine`, emptyDir).
- **Files:** `FILE_STORAGE=local` on a `ceph-rbd` PVC at `/var/lib/outline/data`. Switch to `cephfs`/S3 if HA / start-first is ever wanted.
- **Auth:** OIDC against Authelia — the `outline` client is already registered in `apps/authelia`
  (`configuration.yml`). NOT behind Authelia forwardAuth (Outline owns login). `SECRET_KEY`/`UTILS_SECRET`
  encrypt data at rest and are reused verbatim from the old deployment.
- **kube-vnet edges:** `net.traefik.traefik: ingress`, `net.postgres.postgres: egress`,
  `net.outline-redis: egress` (redis carries `net.outline-redis: ingress`).

Outline must restore into an **empty** CNPG DB before its first boot, or its own schema creation
collides with the dump.
