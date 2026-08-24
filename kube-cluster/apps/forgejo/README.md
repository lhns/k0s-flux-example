# forgejo

[Forgejo](https://forgejo.org) — git forge at `forgejo.example.com` (traefik ingress, wildcard TLS).
Image `codeberg.org/forgejo/forgejo:16.0.2`. Runs **alongside** [`../gitea`](../gitea/README.md)
until the repositories move across; see *The migration from Gitea* below for why they cannot simply
share a database.

## Shape

- **Server:** one Deployment, `replicas: 1`, `strategy: Recreate`. Runs as root so s6 can drop to
  `git` (uid/gid **1000**) via `USER_UID`/`USER_GID`. **No `fsGroup`** — the s6 setup script
  already chowns `/data/gitea` and `/data/git` on every start, and `fsGroup` would make the kubelet
  redo that work on the HDD tier before the container even starts.
- **DB:** the shared CNPG cluster (`postgres-rw.postgres.svc.cluster.local:5432`,
  `sslmode=disable`), own database + own role, created **empty**. Forgejo runs its xorm migrations
  against it on first boot.
- **Files:** two dynamically-provisioned PVCs, one per Ceph tier.
  `/data/git` (repositories + LFS) is `cephfs-appdata` — the **HDD** filesystem, where Gitea's
  27 G already lives. `/data/gitea` (`app.ini`, avatars, attachments, indexers) is `cephfs`, the
  SSD tier. The split needs no config at all: the root image keeps Gitea's layout, so
  `[repository] ROOT` is already `/data/git/repositories` and `APP_DATA_PATH` is `/data/gitea`.
- **Auth:** Forgejo's own, with an Authelia OIDC login source added out of band (below). Unlike
  Gitea, this one needed a **new** Authelia client — and one that requests `groups`.
- **SSH:** disabled, matching gitea. No Service, no VIP, no port for it to land on.
- **Mail:** the shared `msmtpd` relay. `forgejo` is listed in `apps/msmtpd/vnet.yaml`.
- **kube-vnet:** `net.traefik.traefik: ingress` + `net.postgres.postgres: egress` +
  `net.msmtpd.msmtpd: egress`. No `vnet.yaml` — all three are vnets owned by other namespaces.

## `FORGEJO__`, and the one rule about it

Forgejo's env-config prefix regex is literally `^(FORGEJO|GITEA)__`, so the Gitea names still parse
— deliberate compatibility, not an accident. **Do not mix the two prefixes for the same key**;
precedence between them is undefined. This repo uses `FORGEJO__` exclusively. `USER_UID`/`USER_GID`
and `GITEA_CUSTOM` keep their historical names and are unaffected.

The env reaches `app.ini` through `environment-to-ini`, which the s6 setup script runs
**unconditionally** — after it writes the template, and on every subsequent start. So env always
wins over whatever is on the volume, including on the very first boot. That is the mechanism the
next section depends on.

## No install wizard, and what that costs

Without `[security] INSTALL_LOCK`, Forgejo serves its setup wizard on the first request, and
whoever reaches the URL first configures — and owns — the instance. `*.example.com` is a wildcard onto
the public edge, so that window is open the moment the pod is ready. `INSTALL_LOCK=true` is
therefore not optional here.

But the wizard is also what would normally **generate the instance's crypto material**. Locking it
means nothing does, so all four are pinned in `secrets.yaml` (SOPS) and injected by `secretKeyRef`:

| key | why it must be pinned rather than generated |
| --- | --- |
| `SECRET_KEY` | the AES key for encrypted DB columns — 2FA secrets, mirror credentials, and the Authelia client secret in `login_source` |
| `INTERNAL_TOKEN` | signs internal API calls (git hooks talk back over it) |
| `JWT_SECRET` | OAuth2 access tokens |
| `LFS_JWT_SECRET` | LFS authentication |

Left to itself Forgejo would mint these and write them into `app.ini` **on the PVC** — at which
point losing that volume silently invalidates every session and LFS token, and makes the
`login_source` row undecryptable. `SECRET_KEY` in particular must be right in the *first* commit:
changing it later does not fail loudly, SSO simply stops working and 2FA users are locked out.

`secrets.yaml` also carries `OIDC_CLIENT_SECRET`, which **no container reads** — it is the
plaintext half of the Authelia client secret, kept next to its own instance for the one-time
`add-oauth` command below and for any later rotation.

## One-time setup

All of these run as `su git`: the pod is root so s6 can drop privileges, but the CLI refuses to run
as root. (There is an override env — still spelled `GITEA_I_AM_BEING_UNSAFE_RUNNING_AS_ROOT` — and
it should not be used.) There is **no init-script hook** in this image to do any of it declaratively;
[forgejo#1035](https://codeberg.org/forgejo/forgejo/issues/1035) is open and cites Kubernetes as
the motivating case.

**There is exactly one step, and no admin account to create.** With
`oauth2_client.ENABLE_AUTO_REGISTRATION` and `ACCOUNT_LINKING=auto` set in `resources.yaml`, the
first person to log in through Authelia is created automatically and promoted to admin by the
`admin` group claim. Creating a local admin first is not just unnecessary, it is actively
counterproductive — see *Why SSO stops at a "link account" page* below.

```sh
# The Authelia login source. --name authelia is load-bearing: it fixes the
# callback to /user/oauth2/authelia/callback, which is what routing.yaml's
# login redirect and the Authelia client's redirect_uris both assume.
kubectl -n forgejo exec deploy/forgejo -- su git -c \
  "forgejo admin auth add-oauth --name authelia --provider openidConnect \
     --key forgejo --secret '<OIDC_CLIENT_SECRET>' \
     --auto-discover-url https://auth.example.com/.well-known/openid-configuration \
     --scopes 'openid email profile groups' \
     --group-claim-name groups --admin-group admin"
```

**Boolean flags must be bare.** [forgejo#7938](https://codeberg.org/forgejo/forgejo/issues/7938)
was closed *not a bug*: writing `--group-team-map-removal true` shifts argument parsing and
surfaces as the entirely misleading `auth source is not activated`. Omit boolean flags you do not
want rather than passing them `false`.

`--group-claim-name groups --admin-group admin` is why the Authelia `forgejo` client requests the
`groups` scope and sets `claims_policy: 'default'`, where the older `gitea` client does neither.
It means admin rights follow the lldap `admin` group instead of being granted by hand.

**The `forgejo` group has to be created in lldap by hand.** Nothing in this repo declares lldap
groups — they live in the CNPG `lldap` database — so the Authelia policy above denies everyone
outside `admin` until that group exists.

### Why SSO stops at a "link account" page

If a login lands on a page offering *"register a new account / link to an existing one"* with a
username and password box and no Authelia button anywhere, **the integration already worked**. That
page is `/user/link_account`, reached only *after* Authelia has authenticated you and redirected
back. Forgejo is asking what to do with the identity it just received.

Two separate defaults produce it, and both are set in `resources.yaml`:

- **`oauth2_client.ENABLE_AUTO_REGISTRATION`** (default `false`) — an unknown external identity is
  prompted to register rather than created. Note this is *not* the same knob as
  `service.ALLOW_ONLY_EXTERNAL_REGISTRATION`, which governs who may register, not whether it
  happens without a form.
- **`oauth2_client.ACCOUNT_LINKING`** (default `login`) — if a local account already has that
  email, Forgejo demands that account's *local password* before attaching the identity. The
  default is conservative on purpose: auto-linking on an email claim means anyone who can make an
  IdP assert an address inherits the matching account. `auto` is safe here only because Authelia
  is the sole identity source and no other login source exists on this instance.

The trap is that pre-creating a local admin (the obvious first move when `INSTALL_LOCK` means no
wizard ran) *causes* the second case — and if that account's password was randomised, there is then
no way to complete the link at all. Set both options, own no local accounts, and let the first SSO
login create the admin.

## The migration from Gitea

**There is no database path.** Forgejo's compatibility statement is explicit: future versions do
not support upgrades from Gitea **v1.23 or above**, and the supported ceiling is Gitea v1.22 →
Forgejo v10. Ours is Gitea **1.27.1**, well past it. Anything at the schema level would be manual
surgery on a best-effort basis, which is why this is a wholly separate instance — own database, own
role, own storage — and the repositories come across over the API instead.

Measured against the live Gitea database, that is a much smaller job than "175 repositories"
suggests:

| | count | how it comes across |
| --- | --- | --- |
| repositories | 175 | — |
| ↳ **mirrors** | **161** (160 pull, 1 push) | recreated as pull mirrors pointing at their **original upstreams**, so they keep mirroring |
| ↳ real repositories | **14** | in-app migration over the Gitea API |
| issues / PRs / comments | **0 / 0 / 0** | nothing to move |
| releases | 9019, but **9006 belong to mirrors** | mirror releases re-sync themselves; 13 are in real repos |
| webhooks | 0 | — |
| users / orgs | 4 / 0 | recreated by SSO on first login |

So the API route — the only supported one — is entirely sufficient, and no risky schema surgery is
warranted to preserve data that does not exist. **Mirrors must be re-pointed at their original
upstreams, not at Gitea**, or Forgejo would mirror a mirror and the chain breaks the moment Gitea
is retired.

### Status: 151 of 175 across; 24 private mirrors still need a credential

`~/gitea2forgejo/` on the jumphost holds the whole thing — `classify.sh`, `migrate.sh`, `plan.tsv`
(175 rows, one per repo, pre-reviewed), `dead.list`, `state/` (per-repo `done` / `failed`), and both
API tokens at mode 600. It is idempotent and resumable: `./migrate.sh dead|real|mirror` retries only
what has not landed. **Gitea is never written to**, so it remains a complete rollback.

Completed 2026-08-11:

| class | result |
| --- | --- |
| dead (upstream gone, Gitea is the only copy) | **5 / 5** |
| real (ordinary repositories) | **14 / 14** |
| mirrors | **132 / 156** |
| **total in Forgejo** | **175 repos** — matching Gitea's 175 — 151 populated, 10.0 GB |

**Every mirror that landed has completed a first successful sync (132/132).** That was the one
silent failure mode worth checking: a mirror created with a *revoked* embedded token looks
identical in the UI to a working one and only ever reveals itself by never updating. None are in
that state, so the reused per-repo tokens are all still valid.

### The 24 that failed, and what they need

One bounded cause, not transient. Gitea has **190 on-disk repos but only 149 carry an inline
credential** in `.git/config`; the other 41 have none, and the *private* ones among those cannot be
cloned anonymously. All 24 fail identically:

```
Authentication failed: Clone: exit status 128
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

```
DaftTech.croom  DaftTech.hhg-ganztag  DaftTech.iostra  DaftTech.stackstore
DaftTech.webpage-2016  DockBits  ESP8266-Projects  FL-Studio-Projects  FreeAuth
Industria  LabyrinthDigger  PipeLayer  PocketContraption  ProcessMonitor
PushyEditor  Ratty  RechnungsManagement  Th3Falc0n.daftPAInventory
Th3Falc0n.Down  Th3Falc0n.mesg-client  Th3Falc0n.mesg-it  Th3Falc0n.pegasus
Th3Falc0n.Universe  WebDMX
```

They exist in Forgejo as **empty shells**, which is not damage: `migrate.sh` deletes an empty repo
before retrying it, precisely so a failed attempt cannot masquerade as a completed one. A re-run
after supplying a credential therefore starts clean.

Two ways to finish them:

1. **One GitHub PAT with `repo` scope**, used as a fallback in `credential_for()` when no embedded
   credential exists, then `./migrate.sh mirror`. They stay real mirrors of their upstreams.
2. **Clone them from Gitea** (as the `dead` class does). Content is preserved, but they stop
   mirroring upstream and become static copies — which matters when Gitea is retired.

### Also outstanding

- **Rotate the Forgejo admin token** (`write:repository`, `write:user`). `migrate.sh` passes it on
  `curl`'s **argv**, so it is readable in `ps` by any user on the jumphost — the script kept it out
  of its own log but never out of the process table, and a fragment leaked into an assistant
  transcript on 2026-08-11. Patch the script to pass the header via `curl --config <file>` or stdin
  before running it again.
- The **push** mirror on `lhns/mc-dependency-provider` → GitHub (8 h), which `POST /repos/migrate`
  does not carry and needs a separate `POST /repos/{owner}/{repo}/push_mirrors` now that the repo
  exists.
- Once the above land: stop Gitea's own mirrors, or retire Gitea. While both run, both pull from
  the same upstreams — harmless, but pointless.

### Four things learned the hard way, worth not rediscovering

- **`[git.timeout] MIGRATE` defaults to 600 s** and killed the first attempts at 661 s. Now 3600 s,
  set in `resources.yaml` as `FORGEJO__git_0X2E_timeout__MIGRATE` — note `_0X2E_`, which is how
  `environment-to-ini` encodes a dot *inside a section name*.
- **`curl`'s own `-m` is a second, independent ceiling.** At 1800 it cut every clone over 30
  minutes; raising the server limit alone does nothing. Now 14400.
- **`http=000` does not mean failure.** The connection drops client-side while Forgejo keeps going;
  `citra` recorded `FAIL` and was fully populated. Only the repository's own state —
  absent / empty / populated — is evidence, so the script now *polls* after a `000` rather than
  ruling on it. Ruling on it risks deleting a migration that is still running.
- **Throughput was the whole story on the first attempt.** It ran at 13 KB/s while Ceph was
  degraded and every clone timed out; at ~2.3 MB/s on a healthy pool the same script completed
  175 repos without a single non-credential failure. Nothing about the script needed changing —
  only the storage underneath it.

## Notes

- **v16 is the quarterly line**, superseded roughly every three months, so staying supported needs
  a deliberate major bump. v15 is the LTS (patched to 2027-07) if that trade ever looks wrong.
- **`REVERSE_PROXY_TRUSTED_PROXIES` is set explicitly** because v16 removed the container image's
  implicit `*`. Nothing here breaks without it — we do not use
  `ENABLE_REVERSE_PROXY_AUTHENTICATION` — but every client address in the logs and in rate-limiting
  would otherwise be a Traefik pod.
- **`DEFAULT_ACTIONS_URL` differs from Gitea's**, which defaults to `github.com`. Forgejo's is
  `data.forgejo.org`, and it is what `uses: actions/checkout@v4` resolves against.
- **`[mailer] FROM` is mandatory** on a fresh install. `apps/gitea` has no `FROM` only because it
  inherited one from the `app.ini` it migrated; without it, mail silently does not send.
- **Registration stays enabled**, which looks wrong and is not: an SSO login by an unknown user
  *is* a registration. `ALLOW_ONLY_EXTERNAL_REGISTRATION` is what makes Authelia the only path, and
  the button is hidden because there is no local form worth showing.
- **No local-login escape hatch**, same as gitea: `forgejo-login-redirect` sends `/user/login` to
  Authelia. If Authelia is down, recover with
  `kubectl -n forgejo exec deploy/forgejo -- su git -c 'forgejo admin user ...'`, or drop the
  middleware from the route.
- **`forgejo.kube.example.com` 301s to `forgejo.example.com`**, matching every other app here. It served
  the app during setup — the staging URL SSO, mail and the runner were verified on — and was
  flipped once the instance went live.
- Ceph is `HEALTH_ERR` with the `appdata` pool at ~92 %. Standing this up writes almost nothing;
  the moment that matters is the migration, when ~27 G lands — which is exactly where it stalled.
  See *Status* above.
