# matrix

The Matrix homeserver (`example.com` / `matrix.example.com`) — migrated from Docker Swarm (`10.20.2.10`).

## Components

- **synapse** — the homeserver. Media + signing key on a CephFS static PV, mounted in place.
  `homeserver.yaml` is untouched; overlays deep-merge over it (see below).
- **synapse-redis** — replication pub/sub (valkey, ephemeral).
- **mas** — matrix-authentication-service (`mas.example.com`, intercepts login/logout/refresh).
- **lk-jwt** + **livekit** — Element Call RTC. Signaling via Traefik; media on VIP `10.20.2.19`
  (TCP 7881, UDP 50100-50120).
- **well-known** — nginx serving `m.server = matrix.example.com:443`.
- **element-web**, **synapse-admin** — static SPAs (ConfigMap config).
- **baibot** — AI bot; SQLite session/crypto store on ceph-rbd.
- **Bridges** — whatsapp, telegram, signal, instagram, discord (all on CNPG). discord runs a forked
  image carrying a backfill throttle + retry patch; the rest are upstream.

### Synapse config is layered

Synapse merges every `--config-path` **last-value-wins per top-level key**:

| path | source | overrides |
| --- | --- | --- |
| `/data/homeserver.yaml` | CephFS PV, **not in git** | base |
| `/secrets/database.yaml` | `secret-synapse.yaml` (SOPS) | `database` |
| `/config/synapse-turn.yaml` | ConfigMap | `turn_*` |
| `/config-appservices/…` | `synapse-appservices.yaml` | `app_service_config_files` |
| `/media-s3/media-s3.yaml` | `secret-media-s3.yaml` (SOPS) | `media_storage_providers` |

**All five appservice registrations live in git** (`secret-registrations.yaml`, mounted at
`/registrations`). Replacement is whole-key, not element-wise, so `synapse-appservices.yaml` must
always list *every* bridge — a partial list silently unregisters the rest. `homeserver.yaml` still
holds its original three-entry list; it is **dead config**, kept only as the rollback.

TURN comes from [`../coturn`](../coturn/README.md). `synapse-turn.yaml` is deliberately not SOPS:
the secret is read from a mounted file via `turn_shared_secret_path`. Setting both
`turn_shared_secret` and `turn_shared_secret_path` is a startup error.

## Media in S3

Uploads go to **`s3.example.com`, bucket `synapse`** as well as local disk (Aug 2026). Driver is cost:
the media store had reached 32.1 GiB with no retention ever configured.

`store_local` / `store_remote` **true**, `store_synchronous` **false**. **Nothing deletes media.**

**Async on purpose.** Synapse writes to local NVMe before the provider runs, so the file is already
durable; blocking the client on an external S3 round trip gates the fast leg on the slow one. The
cost is upstream's `# TODO: Handle errors` — a failed background upload is logged and forgotten,
so **the sweep is the retry path and is not optional**.

### The sweep (`synapse-media-sweep` CronJob)

Daily 04:23, **suspended** until first enabled deliberately. One job covers three cases, because it
`HEAD`s each object and does not care why a file is on disk without being in S3: historical media
the provider never saw, failed async uploads, and already-uploaded files awaiting reclaim.

- `SWEEP_AGE=30d` — files not **accessed** in 30 days (not "created before"), so hot media stays local.
- `SWEEP_DELETE` empty — set to `--delete` to reclaim disk. Deletes only after a *verified* upload.
- `concurrencyPolicy: Forbid` + `activeDeadlineSeconds` — two runs would race on `cache.db`; Forbid
  alone has no timeout, so a hung run would block all future ones.
- `cache.db` is SQLite → its PVC is **ceph-rbd**, per the single-writer-SQLite rule.
- Credentials are read out of `secret-media-s3.yaml` at runtime; DB access reuses
  `secret-synapse.yaml` via `--homeserver-config-path`, whose `database.name`/`args` shape it wants.

### Getting the module into synapse

`matrixdotorg/synapse` has no boto3. An **initContainer** pip-installs the provider into `/pymods`
with `PYTHONPATH=/pymods`. Three non-obvious details:

- **Persistent PVC, not emptyDir**, skipped when the version marker matches — otherwise every
  restart needs PyPI, and a blip during a node drain becomes a homeserver outage.
- **`--no-deps`** for the provider, boto3 separately. `pip --target` ignores site-packages and will
  try to build **psycopg2 from source**; there is no compiler in the image. psycopg2/Twisted/PyYAML
  are declared deps but only the CLI uses them and all three are already present.
- **Runs as root, chowns `/pymods` to 991.** The PVC is provisioned root-owned. Pod-level `fsGroup`
  would work but applies to every volume — including a recursive chown of the media store.

Any change here **restarts synapse and blocks startup for the length of the pip install** (~3 min).
Replacing it with an ImageComposition would fix that; it is not oci-composer today because that
means hand-resolving eight transitive wheels and `unpack: zip` does not exist. `unpack: deb` would
be wrong regardless — this image imports from `/usr/local/lib/python3.13/site-packages`.

### Path-style addressing

`s3.example.com` serves path-style, but boto3 defaults to virtual-host (`synapse.s3.example.com`) for
DNS-compatible bucket names, which resolves nowhere. The provider builds its own botocore
`Config()` passing only the two checksum options, never `s3.addressing_style` — so it can only come
from the shared config file: **`synapse-aws-config.ini`** + `AWS_CONFIG_FILE=/aws/config`. Delete
that and media breaks with DNS errors that look nothing like a config problem.

Verify the provider actually parsed (silent otherwise):

```sh
kubectl -n matrix exec deploy/synapse -c synapse -- python -c "
import sys; sys.argv=['x'] + '--config-path /data/homeserver.yaml --config-path /secrets/database.yaml --config-path /config/synapse-turn.yaml --config-path /config-appservices/synapse-appservices.yaml --config-path /media-s3/media-s3.yaml'.split()
from synapse.config.homeserver import HomeServerConfig
print(len(HomeServerConfig.load_config('', sys.argv[1:]).media.media_storage_providers))"   # -> 1
```

## telegram-bridge

Down 19 Nov 2025 → 16 Aug 2026. **No token was lost:** `config.yaml` asked for MSC4190 device
management while the registration Synapse loaded was the 2022 format that never granted it.
Restored from the Swarm `telegram-old` triple — config, 588-byte MSC4190 registration, and
`data.db` **plus `-wal`** (2.7 MB uncheckpointed; copying the `.db` alone rolls back silently).

Then ported SQLite → CNPG the same day. It had to start on SQLite (no dump existed, and the legacy
`mautrix.util.async_db` keeps per-dialect migration sets); the port became possible only once the
Go rewrite migrated the DB in place, since bridgev2 generates both dialects from one migration set.
Not upstream-supported — mautrix says "log in again", which would duplicate 400+ contacts.

### Porting SQLite → Postgres

Rollback: the SQLite file is still on the PVC; revert the `database` block in `config.yaml` and drop
the `kube-vnet/net.postgres.postgres` label. `config.yaml.pre-pg.bak` is the exact prior file.

Generate the target schema by running **the same image** against the empty DB, not by hand-writing
DDL. An empty DB has no `user_login`, so that pod cannot touch Telegram. Two traps:

- **Blackholing the homeserver builds only 17 of 38 tables.** `crypto_*` and `telegram_*` migrate
  after the Matrix connection succeeds, so the pod must reach Synapse for real.
- **It then wedges on its own MSC2659 ping**, which needs `telegram-bridge:29317` to have an
  endpoint. Briefly run the real bridge. Do *not* point the Service at the throwaway: it would ACK
  real transactions against an empty DB and Synapse never redelivers.

Both sides must report the same four section versions (main 29, matrix_state 11, crypto 21,
telegram 9 @ v0.2608.0). The copy is mechanical — identical columns, no BLOBs — needing only
`0`/`1`→`boolean` and text→`jsonb`, in FK-topological order (`session_replication_role` is
unavailable; `matrix` is not superuser), with `portal` ordered by `parent_id IS NULL` first because
it self-references. 15165 rows / 38 tables.

**Reset the sequences.** `message.rowid` is an IDENTITY column; copying explicit rowids leaves the
sequence at its old value and *every* insert then fails on `message_pkey`. The bridge sends each
message to Matrix, fails to record it, and so re-sends it forever — every message appears twice,
and Matrix-side sends echo back as your ghost. `setval('message_rowid_seq', max(rowid))`. Row-count
and portal-mapping checks do not catch this; sequence state is invisible to both.

### Config lives on the PVC, not in git

| setting | value | why |
| --- | --- | --- |
| `database.type` / `.uri` | `postgres` + CNPG | the Aug 2026 port |
| `permissions` | dropped `'*': relaybot` | any Matrix user anywhere could otherwise pull the bridge into a room — which is how it once posted a DB error into a room full of security people |
| `public_portals`, `relaybot.authless_portals` | `false` | same reason |
| `homeserver.address` | `http://synapse:8008` | internal, no Traefik hairpin |
| `backfill.enabled` | `true` | was `false` |
| `backfill.max_catchup_messages` / `max_initial_messages` | `5000` / `5000` | see below |
| `sync.update_limit` | `-1` | **was `0`** — see below |
| `takeout.forward_backfill` | `true` | only valid after the export request is approved |

**`sync.update_limit: 0` silently disables all backfill**, whatever `backfill.enabled` says. Gap
detection is in `syncNormalDialog` (`pkg/connector/chatsync.go`), reachable only from
`for updateLimit < 0 || updateLimit > 0` — which `0` skips. `-1` is no-limit. `create_limit: 30` is
fine: consulted only inside `if portal.MXID == ""`.

**Takeout cannot be enabled unilaterally.** It calls `account.initTakeoutSession`; Telegram answers
`TAKEOUT_INIT_DELAY (86400)` until the account holder approves the export in a client. The bridge
blocks on that call and every portal queues behind it (`Portal event channel is still full`).

**The two backfill limits are not interchangeable** (`portalbackfill.go`): `lastMessage != nil` →
catchup, else initial. A *recreated* portal is an empty room and takes the initial path. Note this
only helps chats that actually have more history — rebuilding three portals on that theory returned
identical counts, because those chats only had ~190 messages.

**The dialog-sync latch.** `DialogSyncComplete` serialises under the misleading key
`takeout_portal_crawl_done` (nothing to do with takeout). While set, startup logs
`Dialogs already synced` and does no enumeration at all — no ChatResync, no backfill, and after a
long gap no live messages either, while looking perfectly healthy. Only `!tg sync-chats` clears it
(the one caller passing `restart=true`).

**Message timestamps were lost in the Python→Go migration** (fixed Aug 2026). `timestamp = 0` on
91% of rows; bridgev2 orders by `timestamp DESC, id DESC` and `id` is **TEXT**, so `"99"` beat
`"4767"` and the bridge re-backfilled from a 2021 message on every restart. Timestamp massaging
gave those re-sends the newest `stream_ordering`, so a room complete to Aug 2025 rendered as ending
in Sept 2021. Repaired from Synapse: `timestamp = origin_server_ts * 1_000_000` matched on `mxid`
(Synapse stores ms, the bridge ns), 100% coverage. Only one chat mis-sorted, by luck — the first
chat to cross 9999→10000 would have hit it too.

**SQLite corruption** (pre-port, still relevant if the rollback is used). `integrity_check` reported
a freelist error inherited from the Swarm DB; the pre-revival backup is *worse* (invalid page
number, rowid out of order), so restoring it is not a recovery option. `VACUUM INTO` cleared it with
no row-count change. Copy the DB out with the bridge **stopped**, and delete `-wal`/`-shm` when
swapping a file in or SQLite replays a stale WAL over it.

### The duplication tripwire

```sh
select count(*) from users where name like '@telegram\_%';   -- 407 before revival, 425 after
select id, receiver, mxid from portal;                        -- 38 rows / 21 with mxid at restore
```

Growth alone is **not** proof of duplication — chats started during the outage legitimately have no
portal. The real test is that every pre-existing portal still points at its *original* room (after
the revival: 21 unchanged, 0 rebound, 0 vanished).

Two query traps: `portal` is keyed on **`(bridge_id, id, receiver)`**, so grouping by `id` alone
reads one chat seen by two logins as a duplicate; and `message.room_receiver` is the *login* ID —
the per-portal column is `room_id`.

### Other

- Oversized media is silently dropped during backfill (`media too large`, 50 MB default).
- `com.beeper.room_features` cannot be disabled; Element X shows it as a stray timeline event.
- Redactions leave tombstones; hide via Element → Preferences → Timeline.
- Synapse cannot insert into history, so catch-up messages are **appended**, not slotted in.
  `backfill.queue` is Beeper-only.

## Double puppeting

Makes messages *you* send on the remote network appear as `@admin:example.com` rather than as your own
ghost. All five bridges have it; getting there cost two separate incidents, both from the same
misreading.

**`as_token:` is a prefix carrying the token, not a mode keyword.** There is no bare-keyword form
in Go. `mautrix-go/bridgev2/matrix/doublepuppet.go`:

```go
const useConfigASToken  = "appservice-config"
const asTokenModePrefix = "as_token:"

if hasSecret && strings.HasPrefix(loginSecret, asTokenModePrefix) { ... }   // as_token mode
else if savedAccessToken == "" { err = ErrNoAccessToken }                   // nothing happens
```

**`appservice` is a mautrix-*python* value and is silently ignored by Go bridgev2.** It fails the
prefix test, falls through, and yields `ErrNoAccessToken` — which
`bridgev2/matrix/connector.go` then *swallows* (`if errors.Is(err, ErrNoAccessToken) { err = nil }`),
returning a nil intent. No error, no warning, no log line at any level. Silence is the designed
behaviour for "not configured", so an empty log proves nothing here.

It got into the configs by itself: `bridgev2/bridgeconfig/legacymigrate.go` copies
`bridge.login_shared_secret_map` -> `double_puppet.secrets` **verbatim, without translating
values**. `appservice` was valid pre-rewrite and was carried mechanically into a generation that
no longer understands it. **All four bridgev2 bridges carried the dead value**; signal, telegram
and instagram were plainly broken, and whatsapp only looked healthy because it had a real token
stored from 2023. All four were moved to `as_token:` on 2026-08-24.

### The settings

| bridge | generation | key | value |
| --- | --- | --- | --- |
| whatsapp, signal, telegram, instagram | bridgev2 | `double_puppet.secrets` | `example.com: as_token:<that bridge's own as_token>` |
| discord | legacy mautrix-go | `bridge.login_shared_secret_map` | `example.com: as_token:<its own as_token>` |

Each bridge reuses **its own** `as_token` from its `registration.yaml`; no dedicated
double-puppet registration exists. This works because every registration in
`secret-registrations.yaml` carries the non-exclusive `^@.*:example\.com$` user namespace, which is
what lets it masquerade via `?user_id=`. That namespace is required — without it the bridge can
only ever speak as a ghost. It is also why a stale registration is dangerous: it grants
impersonation over every local account.

Configs live on the PVCs, so this is **not** a git change. Scope the edit to the
`double_puppet.secrets:` block — an unanchored `sed` on `appservice` will hit unrelated keys.

### Verifying — the config alone tells you nothing

Initialisation is **lazy and cached per process**: `user.DoublePuppet()` sets
`doublePuppetInitialized = true` *before* attempting, so a failure is cached until restart, and
nothing is attempted at startup at all. After a config change you must restart, then trigger it.

```
!signal ping-matrix      # -> "Confirmed valid access token for @admin:example.com (appservice double puppeting)"
```

That exact wording is the masquerade path. Then check the database:

```sql
select mxid, access_token from "user" where access_token <> '';   -- must be 'appservice-config'
select count(*) filter (where double_puppeted), count(*) from message;
```

`user.access_token` must read the literal sentinel `appservice-config` — NULL means no double
puppet, and a *real* token means someone ran `login-matrix <token>` by hand (whatsapp's 2023
state). Discord's legacy schema has no per-message flag; check `puppet.access_token` instead.

**It is not retroactive.** Existing messages keep ghost attribution permanently; only messages
sent after activation count, so `double_puppeted` stays at `0/N` until you send a new one.

`login-matrix` cannot enable this mode — it requires a token argument and errors out with
"logging in manually is not supported when automatic double puppeting is enabled".

## whatsapp-bridge

Migrated with a real `pg_dump`, so it has been on CNPG since cutover. Config is a hand-edited copy
on `whatsapp-data`.

**`personal_filtering_spaces` was `false`** until Aug 2026 purely because the Swarm config had it
off. Enabling it is **retroactive but lazy**: `bridgev2/space.go` only adds a portal when
`MarkInPortal` runs and `in_space` is unset, and a restart does not re-mark existing portals.
`!wa sync groups` pulls in groups; **there is no bulk equivalent for DMs**, which join individually
as messages arrive. It looks alarming while running — chats leave the top-level list as they enter
the space, indistinguishable from deletion. Check `select in_space, count(*) from user_portal`.

**`encryption.msc4190` was true while the registration did not declare it** — the same mismatch
that killed telegram, dormant only because the bot device already existed. It would have surfaced on
the first DB restore. Fixed by adding `io.element.msc4190: true` to the registration.

**Double puppeting worked for years for the wrong reason.** Its `double_puppet.secrets` said
`appservice`, which is inert (see [Double puppeting](#double-puppeting)) — the same dead value the
other three had. It kept working only because `user.access_token` held a real Matrix token
provisioned by hand in **January 2023** (Synapse `access_tokens` id 180, device `WhatsApp Bridge`),
carried through the `pg_dump` migration. That made it look like the healthy reference case while
it was one logout away from the same silent failure.

Corrected 2026-08-24: switched to `example.com: as_token:<own as_token>`, which replaced the stored
token with the `appservice-config` sentinel. **The 2023 device and its token still exist in Synapse
and are now unused** — along with an orphaned `Telegram Bridge` token (id 179) whose device row is
already gone. Neither is referenced any more; logging them out is safe cleanup but has not been
done.

## signal-bridge

Fresh install Aug 2026, mautrix-signal `v0.2608.0`, CNPG from the start. `signal-data` (cephfs)
holds only config and the generated registration.

### Backfill is a ONE-SHOT at pairing

Signal keeps **no server-side history**. The only importable history is what the phone transfers
during device linking, and whether that is offered is decided *before the QR is scanned* —
`login.go` passes `Config.Backfill.Enabled` as `allowBackup`, which is what advertises `backup5`
and yields an `EphemeralBackupKey`. Backfill then reads `store.BackupStore`, which only that
transfer fills. Enable it afterwards and there is no recovery but unlink and re-pair; there is no
`sync-chats` equivalent. It worked here: 9 portals, 460 messages.

The QR rotates every **45 s** (20 refreshes, then it gives up) and each refresh posts a new image —
open Signal's camera first, then scan the newest. A stale scan fails silently and reads exactly
like a broken bridge.

### Config on the PVC

| setting | value |
| --- | --- |
| `database` | postgres, CNPG `signal` |
| `homeserver.address` | `http://synapse:8008` |
| `appservice.hostname` | `0.0.0.0` — **default is `127.0.0.1`**, unreachable by Synapse |
| `permissions` | dropped `"*": relay` |
| `backfill.enabled` + both limits | `true`, `5000`/`5000` — **before pairing** |
| `encryption` allow/default/msc4190 | `true`/`false`/`true` |
| `double_puppet.secrets` | `example.com: as_token:<own as_token>` — was the inert `appservice` until 2026-08-24 |
| `provisioning.shared_secret` | `disable` (unused API) |

**Double puppeting needed a registration edit**, not just config: the generated registration grants
only `^@signalbot:…$` and `^@signal_.*:…$`, so the non-exclusive `^@.*:example\.com$` was added by hand
to match telegram. Without it your own Signal-sent messages appear as a ghost.

**Watch unanchored edits**: `sed 's/enabled: false/enabled: true/'` also flips `public_media` and
`direct_media`, which must stay off. Scope to the `backfill:` block.

Known: old attachments 404 (`attachment not found on server` — Signal's CDN expires media);
occasional `AuthorizationFailedError` on Matrix membership changes, harmless; `signal-data` is in
**no Backrest plan** (task #89) yet holds the appservice tokens.

## instagram-bridge

`mautrix-meta`, Instagram build, added Aug 2026. Fresh install: CNPG from the start, so
`instagram-data` (cephfs, 1Gi) holds only `config.yaml` and the generated registration.

**The `ig-` tag prefix is load-bearing.** `mautrix/meta` ships *two bridges from one image* —
plain tags are Facebook Messenger, `ig-` tags are Instagram (upstream README: "this repo contains
two bridges … different build script, different docker tags"). They are separate images, not
aliases: `ig-v0.2608.0` is `sha256:4b2704de…`, `v0.2608.0` is `sha256:662f3d52…`. Dropping the
prefix starts *Messenger* against an Instagram database — a silent wrong-bridge swap, not a
crash. `renovate.json` pins the image to `^ig-v` with a regex versioning so an update can never
strip it, and the image is digest-pinned here as well.

| | |
| --- | --- |
| image | `dock.mau.dev/mautrix/meta:ig-v0.2608.0@sha256:4b2704de…` |
| port | 29330 (upstream default for this build; telegram 29317, whatsapp 29318, signal 29328) |
| database | `instagram` on the shared CNPG, owner `matrix` |
| PVC | `instagram-data`, cephfs, 1Gi |

Config on the PVC, same shape as signal: `appservice.hostname: 0.0.0.0` (the default
`127.0.0.1` is unreachable by Synapse — the usual first failure), `permissions` limited to
`example.com: user` + `@admin:example.com: admin` with `"*": relay` removed, `double_puppet.secrets:
example.com: as_token:<own as_token>` (the inert `appservice` until 2026-08-24), encryption
allow/default/msc4190 `true`/`false`/`true`. The generated
registration grants only `^@instagrambot:…$` and `^@instagram_.*:…$`; the non-exclusive
`^@.*:example\.com$` namespace was added by hand for double puppeting, as on telegram and signal.

**Backfill is OFF by default** — `thread_backfill.batch_count: 0` disables it entirely; `-1` is
unlimited, which is what is set here. `batch_delay: 2s` is the only throttle. Unlike signal's
pairing-time one-shot, **Instagram backfill is re-runnable**: if Meta starts rate-limiting or
challenges the login, drop to a bounded `batch_count` and run it again. `disable_xma_backfill`
stays `true`, so reels/stories media is not fetched during the crawl.

Login is **cookie-based** (`!ig login`) — cookies from a browser session logged into Instagram.
Nothing server-side can perform this step.

**Bridging a personal account violates Meta's ToS**, and accounts do get disabled for it. This
runs on the main account by deliberate choice. There is no compliant alternative for DMs (as with
Discord, a bot account cannot see them). Unlimited backfill immediately after a fresh login is the
heaviest possible pattern, which is the main thing that would attract attention.

`instagram-data` is in **no Backrest plan** — same gap as `signal-data` (task #89) — and it holds
the appservice tokens.

## discord-bridge

Deployed 2026-08-21 on a **forked image**, `ghcr.io/lhns/mautrix-discord:v0.7.7-lhns.4` — see
[Backfill throttle + retry patch](#backfill-throttle--retry-patch-forked-image) below for what the
fork changes and why. Port **29334**, appservice id `discord`, DB `discord` on the shared CNPG.

**mautrix-discord is not bridgev2** (v0.7.7 on mautrix-go v0.16.3). Consequences that bite when
copying settings from the other four bridges: the database is configured under
**`appservice.database`**, not a top-level `database:` block, and there is no
`unknown_error_auto_reconnect` — the auto-reconnect knob that telegram/signal/whatsapp/instagram use
does not exist here.

**Bridging a personal account violates Discord's ToS.** A user token is self-botting; accounts have
been terminated for it and mautrix's own docs carry the warning. The compliant alternative, a bot
account, **cannot see DMs** and only reaches servers it is invited to — so the two are not
substitutes. Accepted deliberately, same as the Instagram bridge.

### Backfill is a ONE-SHOT per portal

`portal.go:559` fires `go portal.forwardBackfillInitial(...)` from `CreateMatrixRoom` — initial
backfill runs **once, at room creation, and never again**. Upstream #89 (tulir): *"you can only
backfill right after room creation."* A crawl that fails partway leaves that channel permanently
truncated; the only redo is deleting the portal room and letting it be recreated.

This is the single fact that shapes every value below, and it is the opposite of the Instagram
bridge, whose `thread_backfill` is re-runnable.

### Settings (on the PVC, `/data/config.yaml`)

| key | value | why |
| --- | --- | --- |
| `backfill.forward_limits.initial.dm` | `1000000000` | effectively unlimited. There is no `-1` for *initial* backfill — upstream: *"a special unlimited value is not supported, you must set a limit"* |
| `backfill.forward_limits.initial.channel` / `.thread` | `0` | guilds are opt-in anyway |
| `backfill.forward_limits.missed.dm` | `-1` | everything since the last bridged message. `-1` **is** supported here, and unlike initial it streams as it fetches instead of collecting first |
| `backfill.fetch_delay_ms` | `1000` | **patch.** Paces one request per second *per portal* |
| `backfill.retry_interval_ms` | `60000` | **patch.** |
| `backfill.max_retries` | `60` | **patch.** Up to an hour of patience per chunk. Generous because exhausting it truncates that channel forever |
| `startup_private_channel_create_limit` | `1000` | see below — the default of 5 would have bridged five DMs and silently skipped the rest |
| `permissions` | `example.com: user`, `@admin:example.com: admin` | the shipped `"*": relay` is removed |
| `encryption.allow` / `.msc4190` | `true` / `true` | as signal |
| `login_shared_secret_map` | `example.com: as_token` | double puppeting. Needs the non-exclusive `^@.*:example\.com$` namespace in the registration |
| `logging.min_level` | `info` | at `debug` it logs every one-time-key upload payload in full — tens of KB per restart |

### `startup_private_channel_create_limit` is positional, not incremental

```go
sort.Sort(ChannelSlice(r.PrivateChannels))   // newest first
for i, ch := range r.PrivateChannels {
    user.handlePrivateChannel(portal, ch, updateTS, i < ...PrivateChannelCreateLimit, ...)
}
```

The flag is `i < limit` over a list sorted newest-first. So the limit does **not** mean "create up to
N portals that don't exist yet" — restarting does not progressively work through the backlog, it
picks the same N newest DMs every time. Channels past the cutoff take the `else` branch, which calls
`ForwardBackfillMissed` on a portal with no `MXID`, i.e. nothing happens.

With the default of `5`, only the five most recently active DMs would ever be created and
backfilled. Hence `1000`.

The limit is therefore also the **backfill concurrency**, since each created portal gets its own
`go forwardBackfillInitial` goroutine. `fetch_delay_ms` paces *within* a portal, not across them, so
N portals starting together produce an N-request burst before settling to roughly
`N_still_crawling / fetch_delay`. The burst decays fast (small DMs finish in one or two requests);
the retry loop is what covers it if it does trip the edge limiter.

### Memory

`collectBackfillMessages` appends every fetched message into one slice and bridges nothing until the
whole channel is collected. With an effectively unlimited limit, a large DM is held entirely in RAM,
times however many portals are crawling at once. The Deployment gets **6Gi** rather than the 1Gi the
other bridges use.

### The registration replaced the dead mx-puppet-discord one

`mx-puppet-discord` ran 16 Jun 2022 → 6 Jun 2023. It left **73 ghosts** (`@_discordpuppet__*`), the
bot `@_discordpuppet_bot`, **51 portals** (31 empty), **46 aliases** — but only **355 messages**, 227
of them in two bot-command DMs, one human ever, nothing since Jun 2023. **The history is worth
nothing.** Old data sits read-only at `/mnt/fastappdata/docker/matrix/bridges/discord/` (76 MB,
SQLite). Its ghosts, rooms and aliases are now orphaned — that was accepted.

Synapse had accumulated ~160k undelivered transactions for it, pushed at `discord-bridge:8434` — a
Service that never existed here. It grew with local Matrix activity rather than on a timer, because
the registration granted a non-exclusive `^@.*:example\.com$`, which also meant a three-year-unused
`as_token` held impersonation rights over every local account.

The `discord.yaml` key in `secret-registrations.yaml` was **replaced in place** rather than removed
and re-added. Two reasons: `synapse-appservices.yaml` is whole-key, so touching the list risks
silently unregistering other bridges; and the old and new registrations both claim overlapping
exclusive namespaces, so Synapse would refuse to start if they ever coexisted. The stale
`discord-puppet` row in `application_services_state` is left behind and is harmless — no
registration references it.

### Backfill throttle + retry patch (forked image)

Upstream mautrix-discord fetches backfill in a tight loop with **no pacing and no retry**
(`backfill.go`, `collectBackfillMessages`): 50 messages per request, next request immediately.

That matters because of a specific bug one layer down. In `beeper/discordgo`'s
`RequestWithLockedBucket`:

```go
case http.StatusTooManyRequests:
	rl := TooManyRequests{}
	err = Unmarshal(response, &rl)
	if err != nil {
		s.log(LogError, "rate limit unmarshal error, %s", err)
		return                       // <-- early return, SKIPS the retry below
	}
	if cfg.ShouldRetryOnRateLimit {
		time.Sleep(rl.RetryAfter)    // normal 429s are handled correctly here
		... retry ...
	}
```

A normal 429 is handled — discordgo sleeps for the server's `Retry-After` and retries. But when
Discord's Cloudflare edge answers with an **HTML** block page instead of JSON, `Unmarshal` fails,
the early `return` skips the retry, and the error propagates into backfill. mautrix-discord then
aborts that portal's backfill and **reports success anyway**, so the chat silently ends up with
partial or no history (upstream mautrix/discord#157, open since 2024-09; discordgo#659).

The reporter there had `limit: 1000000000` and got "entirely missing or partially incomplete
histories". Initial backfill is a **one-shot per portal** — tulir, mautrix/discord#89: *"you can
only backfill right after room creation … and missed messages on startup"* — so a truncated
backfill cannot simply be re-run; the portal has to be deleted and recreated.

The patch adds a configurable inter-request delay and a bounded retry at the call site in
mautrix-discord, rather than forking discordgo as well.

**`config/bridge.go`** — new fields. Plain ints, not `time.Duration`: nothing in this config uses
durations, the file does not import `time`, and yaml.v3 will not parse `"500ms"` into a Duration.

```diff
 	Backfill struct {
 		Limits struct {
 			Initial BackfillLimitPart `yaml:"initial"`
 			Missed  BackfillLimitPart `yaml:"missed"`
 		} `yaml:"forward_limits"`
 		MaxGuildMembers int `yaml:"max_guild_members"`
+		FetchDelayMS    int `yaml:"fetch_delay_ms"`
+		RetryIntervalMS int `yaml:"retry_interval_ms"`
+		MaxRetries      int `yaml:"max_retries"`
 	} `yaml:"backfill"`
```

**`config/upgrade.go`** — register them, or the bridge strips unknown keys when it rewrites
config.yaml on startup. Insert after the existing `max_guild_members` line (~98):

```diff
 	helper.Copy(up.Int, "bridge", "backfill", "max_guild_members")
+	helper.Copy(up.Int, "bridge", "backfill", "fetch_delay_ms")
+	helper.Copy(up.Int, "bridge", "backfill", "retry_interval_ms")
+	helper.Copy(up.Int, "bridge", "backfill", "max_retries")
```

**`example-config.yaml`** — after `max_guild_members: -1` (~264):

```diff
         max_guild_members: -1
+        # Delay in milliseconds between message-fetch requests while backfilling.
+        # 0 = upstream behaviour (as fast as the ratelimiter allows).
+        fetch_delay_ms: 500
+        # If a fetch fails, wait this long and try again, up to max_retries times.
+        # Exists because discordgo returns early WITHOUT retrying when a 429 body is not
+        # JSON (Cloudflare HTML), which silently truncates backfill.
+        retry_interval_ms: 30000
+        max_retries: 5
```

**`backfill.go`** — the actual behaviour. Note the `time` import: the file does **not** import it
upstream, so without this hunk the build fails.

```diff
 import (
 	"context"
 	"crypto/sha256"
 	"encoding/base64"
 	"fmt"
 	"sort"
+	"time"
 
 	"github.com/bwmarrin/discordgo"
```

```diff
 	for {
 		log.Debug().Str("before_id", before).Msg("Fetching messages for backfill")
-		newMessages, err := source.Session.ChannelMessages(protoChannelID, messageFetchChunkSize, before, "", "", portal.RefererOptIfUser(source.Session, protoChannelID)...)
-		if err != nil {
-			return nil, false, err
-		}
+		bfCfg := portal.bridge.Config.Bridge.Backfill
+		var newMessages []*discordgo.Message
+		var err error
+		for attempt := 0; ; attempt++ {
+			newMessages, err = source.Session.ChannelMessages(protoChannelID, messageFetchChunkSize, before, "", "", portal.RefererOptIfUser(source.Session, protoChannelID)...)
+			if err == nil {
+				break
+			} else if attempt >= bfCfg.MaxRetries {
+				log.Err(err).Int("attempts", attempt+1).
+					Msg("Giving up fetching messages for backfill")
+				return nil, false, err
+			}
+			log.Warn().Err(err).
+				Int("attempt", attempt+1).
+				Int("retry_in_ms", bfCfg.RetryIntervalMS).
+				Msg("Failed to fetch messages for backfill, retrying")
+			time.Sleep(time.Duration(bfCfg.RetryIntervalMS) * time.Millisecond)
+		}
 		if until != "" {
```

```diff
 		before = newMessages[len(newMessages)-1].ID
+		if bfCfg.FetchDelayMS > 0 {
+			time.Sleep(time.Duration(bfCfg.FetchDelayMS) * time.Millisecond)
+		}
 	}
```

**Build.** Upstream ships `Dockerfile`, `Dockerfile.ci` and `build.sh`; it is a normal Go
multi-stage build (`go 1.25.0`). Tag the fork so Renovate cannot silently pull an unpatched
upstream tag over it, the same hazard the `ig-` prefix rule guards against for mautrix/meta.

**Cost of carrying this.** It is a fork: every upstream release needs the four hunks reapplied and
a rebuild. Worth it only if the rate-limit signature actually appears in the logs — the string to
watch for is:

```
Error collecting messages to forward backfill ... invalid character '<' looking for beginning of value
```

If that never shows up at the configured limit, upstream images are fine and this patch should
stay unbuilt.

## Databases — shared CNPG (`postgres` ns)

One `matrix` role owns **synapse**, **mas**, **whatsapp**, **telegram**, **signal**,
**instagram** and **discord** (`database.yaml`). The first three came from Swarm `pg_dump`s;
telegram was hand-ported out of SQLite; signal, instagram and discord were created empty.

The cluster was initdb'd `C`/`C`, which is Synapse's collation requirement, so a plain `Database`
inherits it. Apps connect to `postgres-rw.postgres.svc.cluster.local:5432` — **not** the pooler
(Synapse needs LISTEN/NOTIFY + advisory locks). Password in `db-role-secret.yaml` (SOPS, reflected
into `matrix`).

## Verify

- `synapse`/`mas`/`whatsapp`/`telegram`/`signal`/`instagram`/`discord` databases present;
  synapse `datcollate = C`.
- `https://matrix.example.com/_matrix/client/versions` → 200; login via MAS works.
- `https://federationtester.matrix.org/?server_name=example.com` green.
- `element.example.com` loads and sends; Element Call connects over `10.20.2.19`.
- Synapse logs **five** `Loaded application service` lines on startup.
- `application_services_state` is currently **empty** — Synapse has no state row for any of the
  five. Rows appear only once Synapse has recorded a transaction outcome, so absence is not
  itself a failure, but it means this check cannot confirm anything as written.
- Double puppeting: `access_token = 'appservice-config'` in each bridge DB (see
  [Double puppeting](#double-puppeting)).
- Bridges respond to `!tg` / `!wa` / `!signal` in their management rooms.

## Rollback

The Swarm stack and its CephFS data are untouched (media mounted in place; mas/whatsapp/baibot
copied, not moved; CNPG uses fresh databases). Restart the Swarm stack to restore; drop and re-dump
the CNPG databases to retry.

Per-bridge rollbacks are noted in their sections: telegram keeps its SQLite file, the registrations
overlay can be dropped to fall back to `homeserver.yaml`'s list, and every hand-edited config has a
`.bak` beside it on its PVC.
