# freshrss

FreshRSS (`freshrss/freshrss`, self-hosted RSS aggregator) at `freshrss.example.com`, behind
**Authelia SSO** with a scoped `/api/` bypass so mobile RSS clients keep syncing.
`freshrss.kube.example.com` redirects to the canonical host. Runs alongside `miniflux` (feeds
were copied over via OPML); the two are independent.

## Shape
- **Storage**: a 5Gi `cephfs` (RWX) PVC. Even with Postgres, the data dir holds
  `config.php`, per-user config (incl. `is_admin`), favicons, caches and logs. It is
  mounted at `/var/www/FreshRSS/data` via `subPath: data`. It used to be mounted twice, the
  second time over `extensions/` — a *sibling* of `data/` in the image, so persisting only `data`
  would have wiped every installed extension on each image update. Extensions now come from a
  read-only image volume instead, so the PVC holds only state. File-only data + an external DB is
  the mealie/homebox
  shape, so `cephfs` rather than `ceph-rbd`: it allows start-first `RollingUpdate`
  (zero-downtime image bumps), lets **ha-surge** cover node drains (it refuses RWO), and
  avoids the RBD Multi-Attach stall on reschedule. The brief 2-pod overlap during a
  rollout is safe — the cron mutex is a per-pod lock under `TMP_PATH` (`/tmp`) so it does
  not span the overlap, but `UNIQUE(id_feed, guid)` on the entry table means a concurrent
  refresh can only waste a fetch, never duplicate an article.
- **Database**: a dedicated `freshrss` role + database in the shared CNPG cluster
  (`infra/postgres`). FreshRSS has **no `DB_*` env vars** — the whole config is one
  `FRESHRSS_INSTALL` arg-string for `cli/do-install.php`, which the entrypoint
  **shell-expands**, so `${DB_PASSWORD}` interpolates from the reflected secret at runtime
  and never lands in the repo. It's idempotent (exit 3 = already installed). There is no
  `sslmode` — FreshRSS connects via PDO `host:port`.
- **Auth**: `auth_type: http_auth` — Authelia authenticates at the proxy, FreshRSS trusts
  the `Remote-User` it sets, and unknown users are auto-created on login (see below).
  Authelia forwardAuth is on **both** routes. The GReader/Fever APIs send no
  cookies and use a FreshRSS API password, so Authelia's `access_control` has a
  `policy: bypass` rule for `^/api/.*$` on this host. **The middleware stays on the API
  route on purpose** — in `http_auth` mode FreshRSS trusts `Remote-User`, and forwardAuth
  makes Traefik *delete* client-supplied `Remote-*` headers and re-set them only from
  Authelia's response. Removing it would allow trivial admin impersonation. A
  `rateLimit` middleware guards the API route because Authelia's brute-force regulation
  does not apply to a bypassed path. Prefer **GReader** (`/api/greader.php`) over Fever,
  whose api_key is a plain MD5.
- **Secrets**: the reflected CNPG role password (`freshrss-db`) plus `freshrss-secrets`
  (`admin-password`, `admin-api-password`). Both are generated **hex** — the entrypoint
  `eval`s the arg-strings, so shell metacharacters in a password would break or inject.
- **Refresh**: the image's built-in cron (`CRON_MIN: "13,43"`), upstream's supported path;
  output goes to the pod's stderr, so refreshes are visible in `kubectl logs`. Requires
  `replicas: 1`.
- **Extensions**: **one composed OCI artifact each**, mounted read-only as image volumes.
  FreshRSS has no declarative installer — it only scans the directory — so each
  `extension-*.yaml` holds a pinned `GitRepository` and the `ImageComposition` that turns it into
  an artifact, and `infra/oci-composer` builds and serves them. Directory names must be
  `xExtension-<entrypoint from metadata.json>`, which is what the mount paths supply. Enable each
  one per-account under *Configuration → Extensions*.

  **One file per extension, self-contained.** Adding an extension is copying a file and changing
  four lines; removing one is deleting it. The generator finds compositions by KIND, so filenames
  are free and the count is unbounded. ImageProxy and LlmClassification come from the same
  FreshRSS/Extensions monorepo and each declares its own `GitRepository` for it — a second clone
  in source-controller, bought so that repinning one cannot move the other's hash.

  **Mounted per-subdirectory, not over the whole directory.** Four mounts at
  `extensions/xExtension-*` rather than one at `extensions/`. That leaves the image's own
  `extensions/` intact — it ships `README.md` and `index.html`, which the single mount hid — and
  a repin re-pulls only the artifact that changed.

  This replaced an initContainer that cloned three repositories on **every pod start**, copied
  directories out, deleted the clones and `chown -R`'d the result. Four things went with it: a
  network dependency at startup, versions pinned by ref but never verified by content, extensions
  written onto the *data* PVC (a cache kept as state), and ownership set by executing chown — now
  `owner: {uid: 33, gid: 33}` on each layer.

  **Read-only is safe, and was checked rather than assumed**: nothing had been written into the
  old extensions directory since the day it was populated, and extension *configuration* lives in
  `data/extensions-data/`, not here.

  **Nothing here names a tag, and nothing should.** Each volume holds a bare placeholder equal to
  its ImageComposition's name, and no composition names a tag. The root generator hashes each
  composition spec **plus the spec of every object its layers point at** — for all four
  extensions here that is the `GitRepository` in the same file — and drives both ends from that
  one value: a kustomize `images:` entry rewriting the volume reference, and a patch setting that
  composition's `publish.tags`. Repin a source and the hash moves, so artifact and pod template
  change in the same commit and a rollout follows.

  **The referenced source has to be in the hash**, and originally was not. A `sourceRef` layer
  *names* its input instead of containing it, so the pin that decides the content lives in the
  `GitRepository`, not in the composition spec. Hashing the composition alone therefore could not
  see it: on 2026-08-08 bumping `freshrss-ext-autottl` from `v0.6.2` to `v0.6.4` left the tag
  identical while the artifact changed. `publish.immutable` (on by default) caught it and the
  composition went `Failed` rather than moving a spec-hash tag onto different content — the guard
  doing exactly its job, but it should never have been reached. The two `FreshRSS/Extensions`
  extensions had bumped the same monorepo commit without their `subpath` content changing, so
  their artifacts were byte-identical and nothing tripped.

  The app carries no kustomize config for this. The composition side could instead be a
  `configurations:` fieldSpec pointing the images transformer at the CRD's `spec/publish/ref` —
  that works, but kustomize only reads such config from a file in the kustomization being built,
  so it would mean the same file copied into every app that owns a composition. The generator's
  patch replaces it.
- **kube-vnet**: `net.traefik.traefik: ingress` + `net.postgres.postgres: egress`
  (feed fetching is external egress). `TRUSTED_PROXY` is the pod CIDR — keep it narrow,
  anything inside it could spoof `Remote-User`.

## Users are created on login
`auth_type` is `http_auth`, so Authelia authenticates at the proxy and FreshRSS trusts the
`Remote-User` header it sets. `http_auth_auto_register` defaults to **true**, so **any
identity Authelia lets through is auto-created in FreshRSS on first visit** — no manual
provisioning. Who gets through is controlled entirely by Authelia's `access_control`
(currently `group:admin` or `group:freshrss`), which is the right place for it.

Admin rights come from **either** being the `default_user` **or** having `is_admin`
(`app/Models/Auth.php`: `$default_user === $currentUser || $isAdmin`). That is why the
break-glass `admin` account keeps its rights despite `is_admin => false` — it is the
`default_user` — and why a promoted SSO user needs `is_admin` instead.

Auto-register does *not* grant admin, and there is no CLI flag for it (`is_admin` lives in
`data/users/<user>/config.php`, on the PVC — not in Postgres). To promote an account
(`lhns` was promoted this way; redo it after a data-volume rebuild):

```sh
kubectl exec -n freshrss deploy/freshrss -- php ./cli/list-users.php          # find the name
kubectl exec -n freshrss deploy/freshrss -- \
  php -r '$f="./data/users/<user>/config.php"; $c=include $f;
          $c["is_admin"]=true; file_put_contents($f,"<?php\nreturn ".var_export($c,true).";\n");'
```

The bundled local `admin` account (password in `freshrss-secrets`) exists from the install
but **cannot log in while `auth_type` is `http_auth`** — there is no login form. It is a
break-glass account: set `auth_type` back to `form` (below) to use it.

## One-time setup
1. Point `freshrss.example.com` (+ `freshrss.kube.example.com`) DNS at the Traefik LB `10.20.2.15`.
2. Visit `https://freshrss.example.com` — Authelia challenges, then FreshRSS auto-creates your
   account and logs you straight in.
3. Promote that account to admin (see above).
4. Import feeds exported from miniflux (*Settings → Feeds → Export*):
   `kubectl cp feeds.opml freshrss/<pod>:/tmp/feeds.opml`
   `kubectl exec -n freshrss <pod> -- php ./cli/import-for-user.php --user <user> --filename /tmp/feeds.opml`
5. Enable the extensions per-account under *Configuration → Extensions*.

**Changing the auth method on a live install:** `FRESHRSS_INSTALL` only applies to a fresh
data volume (`do-install.php` exits 3 once installed). Use `reconfigure.php`, which only
touches the options you pass:
`kubectl exec -n freshrss deploy/freshrss -- php ./cli/reconfigure.php --auth-type form`

**Locked out?** The auth method lives on the PVC, not the DB — run the command above to get
the login form back, or edit `auth_type` in `data/config.php` directly.

**Extension talking to an in-cluster service?** FreshRSS ≥1.29 blocks private-network
access by default (SSRF hardening) and fails **silently**. Add the target to
`INTERNAL_HOST_ALLOWLIST` in `data/config.php` — relevant if LLM Classification is ever
pointed at a local model endpoint (it ships installed but unconfigured, since there is no
LLM endpoint in the cluster today).
