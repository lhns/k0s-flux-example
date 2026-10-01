# nextcloud

Nextcloud, migrated from Docker Swarm.

## Shape
- **App:** `nextcloud:*-apache` on :80, `nextcloud.example.com` (+ `.example.net` 301 redirect). Pin the
  version — Nextcloud upgrades one major at a time.
- **Storage split** (the `/var/www/html` volume was 119G, 99% of it `data/`):
  - **App** (~1.3G: core/apps/custom_apps/config) → a **ceph-rbd** PVC (`nextcloud-html`), seeded once at
    migration time (`rsync --exclude=/data` from the CephFS source). Fast block storage for PHP.
  - **`data/`** (~80G user files + 15G previews + 23G logs) → **mounted in place** off the appdata CephFS
    (static PV `nextcloud-data`), nested at `/var/www/html/data`. Not copied.
- **DB:** shared **CNPG** (role/db `nextcloud`), migrated by `pg_dump`/restore (PG 18 → 18.4).
- **Redis:** in-ns ephemeral `nextcloud-redis` (emptyDir, no auth).
- **Config:** the existing `config.php` is preserved (instanceid/secret/passwordsalt); a merged
  `config/zz-kube.config.php` (ConfigMap) repoints DB+Redis and adds `trusted_proxies`/`overwrite.cli.url`
  (DB password via `getenv(POSTGRES_PASSWORD)`, no secret on disk).

## One-time migration (done)
Nextcloud had to restore into the CNPG DB *before* it booted (`config.php` has `installed=true`, so an empty
DB errors). How it ran:
1. Deploy (this dir); wait for the CNPG `Database nextcloud` + reflected `nextcloud-db` secret + the PVs.
2. `pg_dump` the Swarm DB (`10.20.2.10:5436`, still up) → restore into CNPG (one-shot Job on the CNPG image).
3. Seed the app into rbd, then flip `nextcloud` to `replicas: 1`; it mounts `data/` in place + the
   override, connects to the restored DB, and starts.
4. Point `nextcloud.{lhns,lolhens}.de` DNS → Traefik VIP `10.20.2.15`.
5. Post-cutover: `occ maintenance:repair`; optionally prune the 23G of logs + `occ preview:generate-all`.
