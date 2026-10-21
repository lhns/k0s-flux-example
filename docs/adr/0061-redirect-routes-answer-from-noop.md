# 0061 — Redirect routes answer from `noop@internal` and match any Host

- Status: Accepted
- Date: 2026-10-21
- Current answer: an IngressRoute route whose middlewares are only redirects has service
  `noop@internal` (kind `TraefikService`), and every `redirectRegex` matches any host with
  `[^/]+`, never a literal one. `scripts/check-redirects.py` enforces both.

## Context

Traefik's `Host()` matcher ignores the port in the Host header; `redirectRegex` matches the raw
URL, which keeps it. A regex naming the host therefore misses `Host: alias:443` while the route
still matches, and a redirect-only route whose service is the real app forwards the request
there, past Authelia and Anubis.

## Decision

- Redirect-only routes point at `noop@internal`: a request the redirect misses gets a 404, not
  the app.
- Redirect regexes are host-agnostic (`^https?://[^/]+/(.*)`); the canonical host is only in the
  replacement.
- A conditional redirect on a real route (for example `/web` to `/web/index.html`) is not a
  redirect route and keeps its backend.

## Consequences

- A new alias host cannot open a path to a backend by a typo in its regex.
- Authentication on a real route is unaffected: its redirects run after, or alongside, the auth
  middleware.
