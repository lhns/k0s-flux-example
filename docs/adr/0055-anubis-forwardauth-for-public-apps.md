# 0055 — Anubis as a shared forwardAuth for public apps

- Status: Accepted
- Date: 2026-10-13, amended 2026-10-16 and 2026-10-17
- Current answer: one Anubis (`apps/anubis`) behind a reflected Traefik forwardAuth `Middleware`
  challenges scrapers in front of public apps. A route opts in with one middleware line; client
  and API paths are excluded per route.

## Context

Public apps are crawled by AI scrapers. Anubis (proof-of-work challenge) stops them, and supports
a subrequest mode where Traefik asks it before forwarding. The apps differ in what must stay
reachable without a browser (git, APIs, runners), so protection cannot be all-or-nothing.

## Decision

- One shared Anubis, reached through a forwardAuth `Middleware` reflected into every namespace,
  as for Authelia ([0022](0022-sso-authelia-forwardauth-reflected.md)). No sidecar per app.
- Opt-in per route. Paths that non-browser clients use are served by a higher-priority route
  without the middleware.
- The policy denies with 403, because forwardAuth treats a 200 as a pass.
- Challenge state lives in a non-persistent Valkey; a signed cookie scoped to `example.com` carries a
  passed client across all protected hosts. The signing key is a SOPS Secret shared by all replicas.
- Amended 2026-10-16: `REDIRECT_DOMAINS` is the glob `*.example.com`, so a new host under that
  domain needs no change; any example.com subdomain is a permitted redirect target.
- Amended 2026-10-17: the cookie is not bound to the client IP (`-jwt-restriction-header` empty),
  because rotating client IPs (Private Relay, mobile) invalidated it and broke SPAs' background
  calls. Accepted risk: a stolen cookie works from any IP until it expires.

## Consequences

- A protected route depends on Anubis, so it runs two replicas with a PDB. If the middleware is
  missing, Traefik drops the route (404), so it must exist before a route references it.
- One solved challenge covers every protected host; a stolen cookie works on all of them.
- Every protected host must match Anubis's `REDIRECT_DOMAINS`.
- Rotating the key invalidates every cookie.

## References

- `23a94ede`, `d4b02469`, `d8d975cf` (PR #853, #884).
