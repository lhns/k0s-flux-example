# jetkvm

A self-hosted JetKVM cloud, replacing `app.jetkvm.com`. Devices phone home here instead.

| | |
| --- | --- |
| `jetkvm.example.com` | the SPA (`ui`, nginx serving a Vite bundle) |
| `jetkvm-api.example.com` | the API (`cloud-api`, Node) — devices connect to `wss://jetkvm-api.example.com/` |
| database | `jetkvm` on the shared CNPG cluster |
| STUN | the existing coturn, `stun:turn.example.com:3478` — unchanged, see below |

## Both images are built here

Upstream publishes neither. `jetkvm/cloud-api` ships a Dockerfile but its only workflow is
`pull-request.yml` and its only tag is `challenge-base`. The UI is not merely unpublished:
`ui/.env.cloud-production` hardcodes `VITE_CLOUD_API=https://api.jetkvm.com`, and Vite bakes
`VITE_*` into the bundle at build time, so **the API hostname is compiled in**. There is no
runtime setting that can point a stock bundle at us.

Neither repository is forked. `ImageBuild` takes its context from a `GitRepository` and its
Dockerfile `inline`, so the two files upstream does not have live in `images.yaml` instead of in
a fork we would have to keep rebasing. `subpath: ui` is what makes the firmware monorepo usable
without one — `ui/` is self-contained, with its own `package-lock.json`.

This is the repo's first `ImageBuild`. Worth restating what that turns on: build pods permit
privilege escalation and run seccomp/AppArmor unconfined, which rootless BuildKit needs.
`serviceAccountName` is deliberately left empty, so the pod gets the namespace default with **no
API token mounted** — whatever the account could do, a Dockerfile in the referenced repository
could do.

`interval` never rebuilds on a timer. Picking up an upstream fix means moving the pinned
revision, which is a visible commit; the root generator hashes each `ImageBuild` spec together
with its `GitRepository`'s and drives both the published tag and the `image:` reference in
`resources.yaml` from that one hash, so the artifact and its consumer can never drift.

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

The final stage still inherits `node_modules` from the `packages` stage. That is load-bearing:
it is what puts the Prisma CLI in the image, and the migrate initContainer runs from the same
image.

The UI Dockerfile deletes the `prepare` lifecycle script before `npm ci`. It is
`cd .. && husky ui/.husky`, and with `ui/` as the context root its parent is not the repository,
so `npm ci` would fail. Removing that one script is narrower than `--ignore-scripts`, which would
also skip the native postinstalls swc and friends need.

## Three source-level traps, all confirmed at the pinned commit

**`ICE_SERVERS` silently drops anything that is not `stun:`.** `src/webrtc-signaling.ts:17`:

```js
return str.split(",").filter(url => url.startsWith("stun:"));
```

Putting a `turn:` URL there accomplishes nothing. (Upstream's own default is buggy for the same
reason: `"stun.cloudflare.com:3478"` has no `stun:` prefix and filters itself out.)

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

## What this deployment cannot do

Worse than "no TURN". `ui/src/routes/devices.$id.tsx` calls `POST /webrtc/ice_config` with **no
`.ok` check**, and `CreateIceCredentials` unconditionally calls Cloudflare using
`CLOUDFLARE_TURN_ID`/`TOKEN`. With those unset it throws, returns 500, and the loader silently
builds `new RTCPeerConnection({})`.

So the **device** gets STUN plus its srflx hint, and the **browser gets no ICE servers at all** —
host candidates only. Sessions succeed only where the browser can reach one of the device's
candidates directly. Adding a `turn:` URL to `ICE_SERVERS` does not fix this (see above);
upstream PR #53 exists to add coturn support and is unmerged.

`R2_*` is also unset, so firmware release hosting is not available.

coturn itself needs no change: it already answers unauthenticated STUN on 3478 —
`use-auth-secret` gates allocations, not binding requests — and `external-ip` plus
`externalTrafficPolicy: Local` already make the reflexive address the real client IP.

## Before it works

`secret.yaml` ships `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` as `REPLACE-ME` placeholders,
because they come from a Google OAuth client that has to be created by hand. Until they are real,
the app runs and serves but nobody can sign in. The redirect URI must be registered against
**`jetkvm-api.example.com`**, not the SPA hostname.

To set them (from the repo root, sops picks up `.sops.yaml`):

```sh
sops kube-cluster/apps/jetkvm/secret.yaml
```

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
