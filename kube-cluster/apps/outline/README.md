# outline

[Outline](https://www.getoutline.com/) team wiki, migrated from Docker Swarm.

## Shape
- **Web:** `outlinewiki/outline:1.9.2` on :3000, fronted at `outline.example.com`.
- **DB:** dedicated `outline` role + database in the shared CNPG cluster
  (`postgres-rw.postgres.svc.cluster.local:5432`, `PGSSLMODE=disable`).
- **Cache:** in-namespace ephemeral `outline-redis` (`redis:7-alpine`, emptyDir).
- **Files:** `FILE_STORAGE=local` on a `ceph-rbd` PVC at `/var/lib/outline/data` (started empty — the
  source dir had no attachments). Switch to `cephfs`/S3 if HA / start-first is ever wanted.
- **Auth:** OIDC against Authelia — the `outline` client is already registered in `apps/authelia`
  (`configuration.yml`). NOT behind Authelia forwardAuth (Outline owns login). `SECRET_KEY`/`UTILS_SECRET`
  encrypt data at rest and are reused verbatim from the old deployment.
- **kube-vnet edges:** `net.traefik.traefik: ingress`, `net.postgres.postgres: egress`,
  `net.outline-redis: egress` (redis carries `net.outline-redis: ingress`).

## One-time migration
Outline must restore into an **empty** CNPG DB *before* it first boots, or its own schema creation collides
with the dump. Hence it ships at `replicas: 0`.

1. Deploy (this dir) with `outline` at `replicas: 0`; wait for the CNPG `Database outline` and the reflected
   `outline-db` secret to exist.
2. Dump the old Swarm Postgres (`10.20.2.10:5436`) and restore into CNPG (same major, PG 18):
   ```
   PGPASSWORD=<old> pg_dump  -Fc --no-owner --no-acl -h 10.20.2.10 -p 5436 -U outline -d outline -f /tmp/o.dump
   PGPASSWORD=<new> pg_restore    --no-owner --no-acl -h postgres-rw.postgres.svc.cluster.local -U outline -d outline /tmp/o.dump
   ```
   (run as a one-shot Job on the CNPG image, labelled `kube-vnet/net.postgres.postgres: egress`).
3. Flip `outline` to `replicas: 1`; it applies any forward migrations and starts.
4. Point `outline.example.com` DNS → Traefik VIP `10.20.2.15`, verify OIDC login + content, then remove the Swarm stack.
