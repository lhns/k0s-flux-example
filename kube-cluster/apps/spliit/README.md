# spliit

Shared-expense app at **`spliit.example.com`**, running the
[`antonio-ivanovski/spliit-cloud`](https://github.com/antonio-ivanovski/spliit-cloud) fork of
[Spliit](https://github.com/spliit-app/spliit), with sign-in through Authelia over OIDC.

## Why the fork

Upstream Spliit has **no accounts at all** — its Prisma schema has `Group`, `Participant`,
`Expense` and no `User`/`Account`/`Session`. Groups are addressed by URL, so anyone holding a link
can edit, and the only possible gate is a reverse proxy. SSO there is not unconfigured, it is
impossible. The fork adds real accounts on better-auth, including a **generic OIDC provider** that
Authelia drives directly.

What that costs: five images instead of one, mandatory SMTP, a single-maintainer project, and no
semver. Upstream, for its part, came back to life in August 2026 after a quiet winter and now cuts
proper releases — so this is a deliberate choice for per-user identity, not a rescue from an
abandoned project.

## Shape

- **`web`** — nginx serving the SPA and reverse proxying the API. The only thing traefik talks to.
- **`api`** — better-auth, tRPC, everything touching the database.
- **`worker`** — recurrence series, notifications, reconciliation. Recurring expenses are real
  scheduled jobs here, not read-time side effects, so without it they never fire.
- **`migrate`** — an initContainer on both api and worker, not a Job. It runs
  `prisma migrate deploy`, which is idempotent, so whichever pod starts first applies and the
  other no-ops. A Job would need its name to change on every image bump, since Jobs are immutable.
- **DB:** the shared CNPG cluster (`database.yaml`), not the bundled Postgres.
- **MCP:** not deployed.

**The Service named `api` cannot be renamed.** `nginx.web.conf` in the web image hard-codes
`proxy_pass http://api:3001` in seven places, resolved by the same-namespace DNS search path.
Rename it and the SPA loads while every backend call 502s.

All four images are pinned to **one commit sha**. The `web`↔`api` contract (tRPC, the proxied
paths) is unversioned, so mixing commits breaks things in ways nothing checks — `renovate.json`
groups them into a single PR for exactly that reason.

## Getting in

There is no forward-auth on this host. The app authenticates natively, and putting Authelia in
front would lock out the people it is for: friends with no lldap account, and anyone opening a
shared link.

| who | how | can write |
| --- | --- | --- |
| someone in `admin` or `spliit` | the **Authelia** button → account created on first sign-in | yes |
| a friend with no lldap account | a **magic link** to their email, or an **anonymous account** | yes |
| a reader | a **public view-only group link** — no account at all | **no** |

**There are no password accounts.** `/auth/sign-up/email` and `/auth/sign-in/email` are refused at
traefik (`spliit-deny-local-signup` in `routing.yaml`), because the fork has no setting to turn
them off — `emailAndPassword.enabled` is hard-coded `true`.

**Registration is not restricted to OIDC, and cannot be.** Magic link signs in *and* registers in
one request, and the plugin is constructed with `disableSignUp: false` hard-coded, so anyone able
to receive mail at any address can obtain a writable account. The only lever that would prevent
that is `SIGNUP_MODE=invite_only` — and its gate covers `/sign-in/oauth2` as well, so it would
also stop lldap users being registered on first SSO, which is the thing this deployment exists to
do. Closing that gap needs an upstream flag, not configuration.

A view-only link genuinely cannot write: the `viewKey` is accepted only by `groupReadProcedure`,
while every mutation runs through `protectedProcedure`, which throws `UNAUTHORIZED` without a real
account. That is enforced in the API, not hidden in the UI — unlike upstream, where the group URL
*was* the credential.

`SIGNUP_MODE=open` is what lets SSO, magic link and anonymous sign-ins create accounts by
themselves. `invite_only` cannot express what is wanted here: its gate covers `/sign-in/oauth2`
exactly as it covers `/sign-up/email`, so it turned away people Authelia had already vetted —
which is precisely how it failed the first time.

### Invitations

Still useful, but no longer how anyone gets an account — they are how someone joins a *group*.

- **An invitation does not create an account.** It writes a `GroupInvitation` bound to a group;
  there is no instance-level "invite a user" at all. The invitee appears in the participant picker
  under a `temporaryName` and can be assigned expenses *before* they have an account, with their
  profile name taking over once they accept.
- **An email invitation must match the address lldap holds**, since acceptance requires
  `invitation.email == accountEmail`. Invite the wrong alias and sign-in still succeeds while the
  invitation silently fails to attach — it reads as "the invite did nothing". `maximilian` is
  `admin@example.org`, for instance, which is not the address the name suggests.
- **A link invitation carries a token**, not an address, so it only counts if the person arrives
  *through the link* — the token rides in a 15-minute cookie so it survives the OAuth redirect.
  Someone who opens the bare site and signs in instead is just an ordinary new account.

## OIDC

Authelia client `spliit`, secret held here in `secret.yaml` and as a pbkdf2 digest in
`apps/authelia/configuration.yml`.

```
redirect_uri  https://spliit.example.com/auth/oauth2/callback/oidc
discovery     https://auth.example.com/.well-known/openid-configuration
scopes        openid profile email
```

That redirect URI is derived, not documented, and is the single most likely thing to get wrong —
it fails only at the last hop, after the login otherwise looks healthy. The fork overrides
better-auth's `basePath` to `/auth` (its default `/api/auth` would 404, because nginx only proxies
`^/(trpc|auth)` to the API), and `genericOAuth` appends `/oauth2/callback/<providerId>`.

PKCE is required — the fork registers the provider with `pkce: true` and `requireIssuerValidation`.
The client is registered `token_endpoint_auth_method: client_secret_post`, **not** the
`client_secret_basic` every other client here uses: better-auth sends the credentials in the
request body, and Authelia rejects the exchange outright if the registration disagrees. That one
fails at the very last hop — authorization, login and consent all succeed first — so the symptom
is an OAuth error at the end of an apparently complete sign-in.

Who may use the button is the client's `authorization_policy` in the Authelia config
(`group:admin` or `group:spliit`). That is the *only* thing it governs: with no forward-auth on
the route, `access_control` never sees `spliit.example.com` — which is why there is deliberately no
rule for that host there. One would read as gating while doing nothing.

Generic OIDC is deliberately **not trusted for account linking** in this fork (`google`, `github`
and `x` are; OIDC is not, because an operator-configured provider may report unverified emails).
First sign-in therefore creates a fresh account rather than linking onto a matching email.

## Notes

- **Mail is required.** `msmtpd.msmtpd.svc.cluster.local:2500`, a trusted anonymous in-cluster
  relay, so no SMTP credentials. Sender `admin+spliit@example.com`, per the relay's rule.
  The `spliit` namespace is listed in `apps/msmtpd/vnet.yaml`; without that entry, invitations
  silently never send.
- **The rate limit is the brute-force protection.** With the forward-auth challenge gone, the
  `spliit-ratelimit` middleware is what stands in front of the auth endpoints. It is per source
  IP, which only works because the api runs `TRUST_PROXY=true` — which
  `ENABLE_ANONYMOUS_AUTH` independently requires, and the env validator enforces.
- **The email form is still rendered.** The deny is at traefik, not in the app, so the auth panel
  still shows an email field and submitting it returns 403 rather than saying "not available
  here". Fixing that needs an upstream flag: `emailAndPassword.enabled` is hard-coded `true` and
  `magicLink` is an unconditional plugin. better-auth has a `disabledPaths` mechanism — the fork
  uses it for `/token` — it is just not exposed as configuration.
- **`net.api` is a same-namespace vnet.** Pod-level isolation denies intra-namespace ingress too,
  so the web→api hop needs it. Missing it looks like a working SPA with every `/trpc` and `/auth`
  call returning 502.
- **Documents and AI carry over unchanged.** The fork kept upstream's `S3_UPLOAD_*` names, so the
  existing bucket and keys still fit; `OPENAI_API_KEY` feeds the fork's `AI_API_KEY`.
- **The database was wiped** on migration. The fork's schema shares nothing with upstream's, and
  there is no DB migration between them — only a per-group import wizard for JSON/CSV/URL exports
  (`docs/migration.md` upstream in the fork). There was no real data at the time.
