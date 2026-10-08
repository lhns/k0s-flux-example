# jetkvm

A self-hosted JetKVM cloud, replacing `app.jetkvm.com`. Devices phone home here instead.

| | |
| --- | --- |
| `jetkvm.example.com` | the SPA (`ui`, nginx serving a Vite bundle) |
| `jetkvm-api.example.com` | the API (`cloud-api`, Node) — devices connect to `wss://jetkvm-api.example.com/` |
| database | `jetkvm` on the shared CNPG cluster |
| STUN/TURN | the shared coturn, `turn.example.com` — see [TURN](#turn-coturn-credentials) |

## Both images are built here

Upstream publishes neither. `jetkvm/cloud-api` ships a Dockerfile but its only workflow is
`pull-request.yml` and its only tag is `challenge-base`. The UI is not merely unpublished:
`ui/.env.cloud-production` hardcodes `VITE_CLOUD_API=https://api.jetkvm.com`, and Vite bakes
`VITE_*` into the bundle at build time, so **the API hostname is compiled in**. There is no
runtime setting that can point a stock bundle at us.

Both are built from our forks, pinned to a commit on their `lhns-build` branch. Each feature is its
own branch on the upstream base, and `lhns-build` merges them; it is also the forks' default
branch, so Renovate's digest PRs follow it.

| fork | base | branches |
| --- | --- | --- |
| [`lhns/jetkvm-cloud-api`](https://github.com/lhns/jetkvm-cloud-api) | upstream `6d5da4f` | `coturn-turn` ([TURN](#turn-coturn-credentials)), `device-sharing` ([sharing](#device-sharing)) |
| [`lhns/jetkvm-kvm`](https://github.com/lhns/jetkvm-kvm) | `release/0.5.8`, the firmware the devices run | `device-sharing` (the UI half), `cloud-fullscreen` (fullscreen button at every width; covers the viewport where the Fullscreen API is missing, e.g. iPhone) |

Each `lhns-build` runs CI on push: cloud-api's tests against Postgres, and the UI's lint and
`build:prod`. The Dockerfiles stay `inline` in `artifacts/images.yaml`, not in the forks.
`subpath: ui` builds the firmware monorepo's self-contained `ui/`.

`ImageBuild` build pods permit
privilege escalation and run seccomp/AppArmor unconfined, which rootless BuildKit needs.
`serviceAccountName` is deliberately left empty, so the pod gets the namespace default with **no
API token mounted** — whatever the account could do, a Dockerfile in the referenced repository
could do.

`interval` never rebuilds on a timer. Picking up an upstream fix means moving the pinned
revision, which is a visible commit; the rebuild exports its digest and `resources.yaml` rolls
onto it (see [`infra/oci-composer`](../../infra/oci-composer/README.md#how-a-workload-names-an-artifact)).

### Deliberate changes to upstream's cloud-api Dockerfile

It could not be used as-is — `FROM node:21.1.0-alpine` is not digest-pinned and the FROM guard
refuses it. Since it had to be rewritten, three other things are fixed at the same time:

1. **Node 22, not 21.** `package.json` declares `"engines": {"node": "22.x"}`, so upstream's own
   image fails upstream's own engines check and runs an EOL runtime.
2. **`npm ci`, not `npm install`** — the lockfile is committed.
3. **`COPY .env.example ./.env` is dropped.** This is the important one. The app does
   `import 'dotenv/config'`; dotenv does not override real environment variables, but it *does*
   supply every variable we do not set, and `.env.example` is not blank. It would bake in
   `CORS_ORIGINS=https://app.jetkvm.com,http://localhost:5173` — which blocks our own origin —
   and a `localhost` `DATABASE_URL`. Without the file, unset means unset, and dotenv ignores a
   missing `.env` silently. Every other key in it is empty, so nothing is lost.

The Prisma CLI is a production dependency, so the final stage's `npm ci --omit=dev` still ships
it; the migrate initContainer runs from the same image.

The UI Dockerfile deletes the `prepare` lifecycle script before `npm ci`. It is
`cd .. && husky ui/.husky`, and with `ui/` as the context root its parent is not the repository,
so `npm ci` would fail. Removing that one script is narrower than `--ignore-scripts`, which would
also skip the native postinstalls swc and friends need.

## Three source-level traps, all confirmed at the pinned commit

**`ICE_SERVERS` silently drops anything that is not `stun:`.** `src/webrtc-signaling.ts:17`:

```js
return str.split(",").filter(url => url.startsWith("stun:"));
```

It is the *device's* list, so putting a `turn:` URL there accomplishes nothing; the browser's TURN
comes from `COTURN_*` (below). (Upstream's own default is buggy for the same reason:
`"stun.cloudflare.com:3478"` has no `stun:` prefix and filters itself out.)

**`REAL_IP_HEADER` must be lowercase**, `src/webrtc-signaling.ts:147`:

```js
const ip = (process.env.REAL_IP_HEADER && req.headers[process.env.REAL_IP_HEADER]) ||
           req.socket.remoteAddress;
```

Node lower-cases every header key and there is no `.toLowerCase()` here, so `X-Real-IP` — which
upstream's `.env.example` suggests — looks up a key that cannot exist, falls through to
`req.socket.remoteAddress`, and records **Traefik's pod IP**. It is also not
`x-forwarded-for`: the value is used whole with no list parsing, and behind our two proxies that
header is `"1.2.3.4, 10.20.2.31"`, which is not an address. This value is the device's own
observed source address, echoed back to it as its srflx candidate.

**`ALLOWED_IDENTITIES` is security-critical.** Upstream's comment is *"leave empty to allow
all"* — unset, anyone with a Google account can enrol a device against this instance.

## One pod, and it cannot be otherwise

`activeConnections` and `inFlight` are in-process `Map`/`Set` with nothing shared behind them.
The device's WebSocket and the browser's `POST /webrtc/session` must be served by the same
process or the offer never meets its answer, so `replicas: 1` and `strategy: Recreate` are
correctness requirements, not capacity choices. Sticky sessions do not help — the device and the
browser are different clients that have to converge on one pod.

The API also owns the root of its hostname: the device connects to `wss://<host>/`, "for legacy
reasons" per the source. So it cannot be mounted under a path of the SPA's hostname, and it must
**not** go behind Authelia or any other ForwardAuth — the device authenticates with its own
`Authorization` header and cannot complete an interactive login.

`SameSite=Strict` ties the two hostnames together: the session cookie is set on the API host and
the SPA calls it with `credentials: "include"`. Strict is evaluated per registrable domain, so
both names must share an eTLD+1 — `jetkvm.example.com` and `jetkvm-api.example.com` do.

There is no `SIGTERM` handler, so `terminationGracePeriodSeconds` only delays the kill. Every
restart is a reconnect storm across all devices, which recover after ~5s.

## TURN: coturn credentials

Upstream's `CreateIceCredentials` only calls Cloudflare TURN; unconfigured it returns 500, and
`ui/src/routes/devices.$id.tsx` (no `.ok` check) then builds `new RTCPeerConnection({})` — host
candidates only. The fork's `coturn-turn` branch is
[jetkvm/cloud-api#53](https://github.com/jetkvm/cloud-api/pull/53) (unmerged) rebased onto the
upstream base: it mints coturn REST-API credentials. The UI needs no change: a 200 with
`iceServers` is used as-is.

- `COTURN_TURN_URLS`: `turn:` UDP+TCP on 3478 and `turns:` on 5349, as matrix uses.
- `COTURN_TURN_SECRET`: `coturn-shared-secret`, reflected from the coturn namespace.
- `TURN_TTL` unset: 3600s. `CLOUDFLARE_TURN_*` must stay unset — set, it wins over coturn.

cloud-api never connects to coturn (it only HMACs), so there is no kube-vnet edge. coturn needed
only `jetkvm` added to its secret's Reflector namespaces; its STUN is unauthenticated on 3478
(`use-auth-secret` gates allocations, not binding requests).

Once #53 (or an equivalent) is merged upstream, rebuild `lhns-build` without `coturn-turn`. The
env vars stay if upstream kept their names.

## Device sharing

An owner shares a device by Google account email (device menu → Share Device). The invitee sees
it in their list, marked with the owner, and can open a session; rename, share and deregister
stay the owner's. `DeviceShare` holds the invites; one binds to an account when that verified
address logs in.

**The device accepts only its owner.** Firmware verifies each cloud session's Google id_token and
requires the `aud:sub` it stored at adoption (`cloud.go`, `authenticateSession`). So a shared
user's session carries an id_token the API mints *for the owner* from a Google refresh token. The
owner grants it once with the "Allow sharing" button on the share page (offline access); it is
stored sealed with `COOKIE_SECRET`, so rotating that secret means granting it again. Consequences:

- **A share is full control**, the owner's included: the device cannot tell the two apart, so a
  view-only role is not enforceable in the cloud.
- **The Google OAuth app must be "In production"**, not "Testing": a Testing app's refresh tokens
  expire after 7 days and sharing then stops until the owner grants it again. Revoking the app at
  Google does the same.
- **The invitee must be in `ALLOWED_IDENTITIES`** too, or they cannot log in; the share page
  flags such an address. An owner removed from it loses every share of theirs as well.
- **Removing a share** is effective at the invitee's next API call. A running WebRTC session is
  peer-to-peer and continues until it closes; the device keeps one session at a time, so the
  next one from anyone replaces it.

## What this deployment cannot do

`R2_*` is unset, so firmware release hosting is not available. The API syncs R2 releases on
start and every 30 minutes with no flag to turn it off, so every tick logs
`[sync-releases] scheduled run failed` and stops there — `R2_BUCKET` is undefined, the first
list call is rejected before any network I/O, and `scheduleReleaseSync` catches it. Expected
noise, not a fault. `POST /releases/sync` is registered only when `RELEASE_SYNC_TOKEN` is set,
so it is not exposed here.

## Before it works

The Google OAuth client lives in `secret-google.yaml` (`GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`). Its redirect URI is **`https://jetkvm-api.example.com/oidc/callback`** (on the
API host, not the SPA's). `secret.yaml` still carries unused `REPLACE-ME` keys of the same names;
nothing reads them.

`COOKIE_SECRET` is already a real random value; it signs sessions, and rotating it logs everyone
out.

## Verifying a deployment

1. Both `ImageBuild`s reach `Ready` and `status.artifact.ref` resolves. An unpinned `FROM` being
   rejected is the guard working, not a bug.
2. The migrate initContainer completes and `\dt` in the `jetkvm` database shows Prisma's tables.
   Do not trust `/healthz` for this — it returns `{ready:true}` unconditionally and never touches
   the database, so a pod whose Prisma client cannot reach CNPG still passes its readiness probe.
3. Deep-link a sub-path of `jetkvm.example.com` to prove the SPA fallback. `/assets/*` deliberately
   does **not** fall back — a hashed bundle that 404s must stay a 404, or a broken deploy looks
   like a blank page returning 200s.
4. **The one that will actually bite**: register a device, then check what source IP the API
   recorded for it (`[Device] New connection`). `*.example.com` resolves only to the public edge —
   there is no split-horizon — so a device on the LAN hairpins out through the router and back in
   via the swarm Traefik at `10.20.2.31/.32/.33`. Whether it arrives with its LAN or WAN address
   depends on the router's hairpin NAT, and that value is what the device advertises as its srflx
   candidate. Confirm empirically before assuming remote access works.
5. Device enrolment is `setCloudUrl(https://jetkvm-api.example.com, https://jetkvm.example.com)` followed
   by a **re-register** — changing the URL alone does not rebind an already-registered device.
6. TURN: `POST /webrtc/ice_config` returns `turn:` URLs with a `<expiry>:<sub>` username, not a
   500, and during a session `chrome://webrtc-internals` lists `relay` candidates while coturn logs
   an allocation.
