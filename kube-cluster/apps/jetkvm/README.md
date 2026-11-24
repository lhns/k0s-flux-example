# jetkvm

A self-hosted JetKVM cloud, replacing `app.jetkvm.com`. Devices phone home here instead.

| | |
| --- | --- |
| `jetkvm.example.com` | the SPA (`ui`, nginx serving a Vite bundle) |
| `jetkvm-api.example.com` | the API (`cloud-api`, Node) — devices connect to `wss://jetkvm-api.example.com/` |
| database | `jetkvm` on the shared CNPG cluster |
| STUN/TURN | the shared coturn, `turn.example.com` — see [TURN](#turn-coturn-credentials) |

## Images

Upstream publishes neither. `jetkvm/cloud-api` ships a Dockerfile but its only workflow is
`pull-request.yml` and its only tag is `challenge-base`. The UI is not merely unpublished:
`ui/.env.cloud-production` hardcodes `VITE_CLOUD_API=https://api.jetkvm.com`, and Vite bakes
`VITE_*` into the bundle at build time, so **the API hostname is compiled in**. There is no
runtime setting that can point a stock bundle at us.

Both images are built by our forks' CI and published to ghcr
([ADR 0009](../../../docs/adr/0009-own-code-in-own-repos.md)):
`ghcr.io/lhns/jetkvm-cloud-api` and `ghcr.io/lhns/jetkvm-ui`. A push to `lhns-build`
publishes `:lhns-build` and `:sha-<short>`, and `resources.yaml` pins `:lhns-build@sha256:<index
digest>`. The fork's workflow summary maps that digest to its commit. Each feature is its own
branch on the upstream base and `lhns-build` merges them; it is also the forks' default branch.
`lhns-build` additionally carries lhns-only commits that never go on feature branches: the CI and
image workflow, the Dockerfile and `dependabot.yml`. They are re-applied when `lhns-build` is
rebuilt.

| fork | base | branches |
| --- | --- | --- |
| [`lhns/jetkvm-cloud-api`](https://github.com/lhns/jetkvm-cloud-api) | upstream `6d5da4f` | `coturn-turn` ([TURN](#turn-coturn-credentials)), `device-sharing` ([sharing](#device-sharing)) |
| [`lhns/jetkvm-kvm`](https://github.com/lhns/jetkvm-kvm) | `release/0.5.8`, the firmware the devices run | `device-sharing` (the UI half), `cloud-fullscreen` (fullscreen button at every width; covers the viewport where the Fullscreen API is missing, e.g. iPhone)), then a stack for tablets and touch, each branched from the previous `lhns-build` because they build on one another (the first edits the lhns-only CI workflow): `ui-unit-tests` (vitest, run in CI), `touch-absolute-drag` (finger drags in absolute mode, int8-safe relative deltas), `android-locks` (pointer/keyboard lock where `permissions.query` throws, keyboard unlock fix), `fullscreen-shortcuts` (three-finger swipes, four-finger menu, "Use a key as Win"), `touch-input` (touchpad/direct gestures and settings), `touch-input-modes` (one "Touch input" setting, Direct touch or Trackpad, with fixed Windows-like timings) |

The recipes live in the forks: the root
[`Dockerfile`](https://github.com/lhns/jetkvm-cloud-api/blob/lhns-build/Dockerfile) of cloud-api
and [`ui/Dockerfile`](https://github.com/lhns/jetkvm-kvm/blob/lhns-build/ui/Dockerfile) of the
kvm fork; their comments explain the deliberate changes to upstream's. One matters here: upstream's
`COPY .env.example ./.env` is dropped. The app loads dotenv, which fills every variable we do not
set, and `.env.example` carries `CORS_ORIGINS=https://app.jetkvm.com,...` (which blocks our own
origin) and a `localhost` `DATABASE_URL`. That is why `CORS_ORIGINS` is set explicitly in
`resources.yaml`.

CI in each fork tests before it publishes: cloud-api runs its tests and the migration against
Postgres, then the same migration from the built image (the libssl regression test); the UI runs
lint, unit tests and `build:prod`, then smoke-tests nginx's cache headers and SPA fallback.

Base images are bumped by Dependabot PRs in the forks (merged by hand), and the resulting digest
reaches this repo as a Renovate PR that is never automerged.

## Decisions

- **Images are built and published by the forks' CI**, pinned `:lhns-build@digest` and never
  automerged (a moving fork tag makes a digest update an unreviewed rebuild).
- **The API hostname `https://jetkvm-api.example.com` is compiled into the published UI image.** The UI
  has no runtime override, `lhns-build` is specific to this cluster, and the fork's workflow
  asserts the hostname is in the bundle. Pointing the image elsewhere means a rebuild. A runtime
  substitution in an nginx entrypoint would remove this, but it is a UI feature of its own.
- **Slower base-image updates.** A node or nginx patch needs a Dependabot PR in the fork, merged
  by hand, and then a reviewed digest PR here, instead of an automerged bump and in-cluster
  rebuild.
- **amd64 only**, as every node is.

The Google OAuth client lives in `secret-google.yaml` (`GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`). Its redirect URI is **`https://jetkvm-api.example.com/oidc/callback`** (on the
API host, not the SPA's). `secret.yaml` still carries unused `REPLACE-ME` keys of the same names;
nothing reads them.

`COOKIE_SECRET` is already a real random value; it signs sessions, and rotating it logs everyone
out.

## Verifying a deployment

1. The fork's image workflow is green and the pinned digest is the one in its summary.
2. The migrate initContainer completes and `\dt` in the `jetkvm` database shows Prisma's tables.
   Do not trust `/healthz` for this — it returns `{ready:true}` unconditionally and never touches
   the database, so a pod whose Prisma client cannot reach CNPG still passes its readiness probe.
3. Deep-link a sub-path of `jetkvm.example.com` to prove the SPA fallback. `/assets/*` deliberately
   does **not** fall back — a hashed bundle that 404s must stay a 404, or a broken deploy looks
   like a blank page returning 200s.
4. **The one that will actually bite**: register a device, then check what source IP the API
   recorded for it (`[Device] New connection`). `*.example.com` resolves only to the public edge —
   there is no split-horizon — so a device on the LAN hairpins out through the router and back in
   via the swarm Traefik at `10.20.176.125/.185/.190`. Whether it arrives with its LAN or WAN address
   depends on the router's hairpin NAT, and that value is what the device advertises as its srflx
   candidate. Confirm empirically before assuming remote access works.
5. Device enrolment is `setCloudUrl(https://jetkvm-api.example.com, https://jetkvm.example.com)` followed
   by a **re-register** — changing the URL alone does not rebind an already-registered device.
6. TURN: `POST /webrtc/ice_config` returns `turn:` URLs with a `<expiry>:<sub>` username, not a
   500, and during a session `chrome://webrtc-internals` lists `relay` candidates while coturn logs
   an allocation.
