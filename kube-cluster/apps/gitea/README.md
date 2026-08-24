# gitea

[Gitea](https://about.gitea.com) — git forge at `gitea.example.com` (traefik ingress, wildcard TLS),
migrated from Docker Swarm. Image `gitea/gitea:1.27.1`.

## Shape

- **Server:** one Deployment, `replicas: 1`, `strategy: Recreate`. Runs as root so s6 can drop to
  `git` (uid/gid **1000**, the on-disk owner) via `USER_UID`/`USER_GID`. **No `fsGroup`** — it
  would recursively chown the 27 G repo tree at every start.
- **DB:** the shared CNPG cluster (`postgres-rw.postgres.svc.cluster.local:5432`, `sslmode=disable`),
  restored from the Swarm Postgres. The Swarm stack's dedicated `pgautoupgrade` container is gone.
- **Files:** `/data/git` (**27 G**, repositories + LFS) is **mounted in place** from CephFS
  `appdata:/docker/gitea/git` — never copied. `/data/gitea` (47 M: `app.ini`, avatars,
  attachments, indexers) is a **fresh cephfs PVC, copied in once** — see below for why it differs.
  `/data/ssh` is not mounted at all.
- **Auth:** Gitea's own, with an Authelia OIDC login source. Authelia already had the `gitea`
  client, the `two_factor` policy for groups `admin`+`gitea`, and a `gitea.kube.example.com` callback
  registered before this migration existed — **no Authelia change was needed**.
- **SSH:** disabled. It was already `DISABLE_SSH = true` on Swarm, so no remote breaks and no
  MetalLB VIP is spent. Asserted in env so it cannot drift back on.
- **Mail:** the shared `msmtpd` relay (`msmtpd.msmtpd.svc.cluster.local:2500`, plaintext, no auth).
  The per-stack `mwader/postfix-relay` is dropped. `gitea` is listed in `apps/msmtpd/vnet.yaml`.
- **kube-vnet:** `net.traefik.traefik: ingress` + `net.postgres.postgres: egress` +
  `net.msmtpd.msmtpd: egress`. No `vnet.yaml` — all three are vnets owned by other namespaces.

## Why `/data/gitea` is copied but `/data/git` is mounted

Gitea's entrypoint runs `environment-to-ini`, which **rewrites `app.ini` on disk at every start**
from the `GITEA__*` env. If `/data/gitea` were mounted in place, the first pod start would repoint
the *Swarm stack's own config* at CNPG — quietly destroying the fallback. At 47 M, copying costs
nothing. At 27 G, on a CephFS pool that is 91 % full and mid-`backfill_toofull`, it would not have
been an option — hence the split.

## Why env overrides and not a fresh app.ini

The existing `app.ini` carries `[security] SECRET_KEY`, `INTERNAL_TOKEN`, `[oauth2] JWT_SECRET` and
`LFS_JWT_SECRET`. `SECRET_KEY` in particular is the AES key for every encrypted column in the
database — TOTP/2FA secrets, mirror credentials, and **the Authelia client secret stored in
`login_source`**. Regenerating it does not fail loudly: Gitea starts fine, and then SSO breaks and
every 2FA user is locked out, unrecoverably.

So the file is preserved and only what actually moved is overridden: `[database] HOST/PASSWD`,
`[mailer] SMTP_ADDR/PORT`, and the SSH assertions.

## One-time migration

Swarm's Gitea was already stopped when this was built (`gitea.example.com` returned a bare Traefik
404), so this was a **cold migration**, not a cutover. Its Postgres was still running, which is all
the dump needs.

1. Commit with `replicas: 0`. New directory ⇒
   `flux reconcile helmrelease generators -n flux-system --with-source`.
2. Confirm both PVCs `Bound` and `gitea-db` reflected into the `gitea` namespace.
3. Copy the config volume, and take a backup of the original `app.ini` on the way past:
   ```sh
   # a LABELLED throwaway pod — label-less pods are denied cluster-wide by kube-vnet
   kubectl -n gitea run copy --image=busybox:1.36 --restart=Never -l app=copy \
     --overrides='{...mount sftpgo-style src RO + gitea-data PVC...}' -- \
     sh -c 'cp -a /src/. /dst/ && cp /src/conf/app.ini /dst/conf/app.ini.pre-kube'
   ```
4. Restore the database **before the first boot** — Gitea runs xorm schema sync on start and would
   create an empty schema that collides with the dump:
   ```sh
   pg_dump -Fc --no-owner --no-acl -h 10.20.2.10 -p 5436 -U gitea -d gitea -f /tmp/gitea.dump
   pg_restore --no-owner --no-acl -h postgres-rw.postgres.svc.cluster.local -U gitea -d gitea /tmp/gitea.dump
   ```
   Run as a one-shot Job with a pod label and `kube-vnet/net.postgres.postgres: egress`, using the
   CNPG cluster's own image so the client major matches (both PG 18). Verify non-zero `user` and
   `repository` counts, and **`login_source >= 1`** — that row is the SECRET_KEY-encrypted Authelia
   client secret.
5. Scale to 1. **Expect a slow first start:** s6 chowns `/data` on every boot, and 27 G of repos on
   the degraded HDD tier takes minutes. That is why readiness allows ~10 minutes and there is no
   liveness probe.
6. Optionally regenerate the git hooks:
   ```sh
   kubectl -n gitea exec deploy/gitea -- su git -c 'gitea admin regenerate hooks'
   ```
   43 of 189 repository directories had no `post-receive.d/gitea` hook. That matters far less
   than it first appears: **161 of the 175 repositories are mirrors**, which sync by *fetching*
   and never receive a push, so the hook is not in their path at all. Idempotent and harmless to
   run; not a fix for anything urgent. Note `su git` — `gitea` CLI commands refuse to run as
   root, while the pod itself is root so s6 can drop privileges.
7. Verify on `https://gitea.kube.example.com` — the staging host, already an Authelia redirect URI —
   *before* touching DNS: SSO round-trip, browse a repo (proves the 27 G mount), `git clone` and
   **push** over HTTPS (exercises the hooks and `INTERNAL_TOKEN`), and a test mail
   (`kubectl -n msmtpd logs`). Then `gitea doctor check --all` (also as `su git`).
8. Point `gitea.example.com` at the cluster and re-verify. **Done** — it serves 1.27.1 from here.
9. Flip `gitea-kube` in `routing.yaml` from serving to the canonical redirect. **Done** —
   `gitea.kube.example.com` now 301s to `gitea.example.com`, matching every other migrated app.

### What the migration actually did

| | |
| --- | --- |
| dump | 249 M, restored into CNPG |
| restored | 112 tables · 4 users · **175 repositories** · `login_source` = 1 · 686 MB |
| schema | **331 → 343** — 1.27.1 applied 12 forward migrations on first boot |
| mirrors | **161 of 175** (160 pull, 1 push) — all on `https://` remotes |
| hooks | 43 dirs lacked `post-receive.d/gitea`; regenerated to 190/190. Mostly cosmetic — mirrors never receive pushes |
| on disk | 189 repository dirs vs 175 in the DB — 14 orphans that **predate** the migration (the source DB also had 175), not data loss |

## Notes

- **The database migrates forward on first boot.** The source was at schema version 331; 1.27.1
  applies any newer migrations and that is **irreversible for the restored copy**. The Swarm
  database is never written to, so the fallback survives — but it will be stale.
- **`/data/git` is shared with Swarm byte-for-byte.** The two must never run at once.
- **No local-login escape hatch.** `gitea-login-redirect` sends `/user/login` to Authelia. If
  Authelia is down, recover with `kubectl -n gitea exec deploy/gitea -- gitea admin user ...`, or
  drop the middleware from the route.
- **Mirrors are the bulk of this instance**: 161 of 175 repositories (160 pull, 1 push).
  Checked at migration time — **every one uses an `https://` remote**, so disabling SSH breaks
  none of them. Re-check if a mirror is ever added with an `ssh://` remote, since there is no
  SSH client path out of this pod's config any more.
- Leftovers to reclaim on the Swarm side once this is trusted (**user action** — the Swarm host is
  off-limits here): the stale 1.4 G `/mnt/appdata/docker/gitea/gitea`, the old
  `/mnt/fastappdata/docker/gitea/db`, and `.../gitea/ssh`.
