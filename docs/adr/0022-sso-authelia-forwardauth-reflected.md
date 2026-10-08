# 0022 — SSO: Authelia and LLDAP in the cluster, forwardAuth reflected into each namespace

- Status: Accepted
- Date: 2026-07-19, amended 2026-07-23, 2026-07-29 and 2026-09-16
- Current answer: Authelia (with LLDAP as its directory) runs in `apps/`, and protects a route
  through a Traefik forwardAuth `Middleware`. traefik-reflector copies that Middleware into every
  namespace, so a route references a local one and Traefik's cross-namespace references stay
  off. Apps that speak OIDC use Authelia as their provider; so does the Kubernetes API server,
  with group bindings in `rbac/`.

## Context

A route can reference a Middleware in another namespace only through Traefik's global
`allowCrossNamespace`, which is one grant for every namespace.

## Decision

- **Reflect, don't cross namespaces** (2026-07-19): a small controller, kube-traefik-reflector
  (own code, [0009](0009-own-code-in-own-repos.md)), mirrors annotated Middlewares; the
  global toggles were removed. Some middlewares, Authelia's forwardAuth first, are used by many
  namespaces: see below and [0021](0021-cross-namespace-copies-are-reflected.md).
- **In-cluster auth stack** (2026-07-23): Authelia and LLDAP run in the cluster with
  their databases. Authelia also serves forwardAuth on a raw TCP entrypoint for the proxies still
  on the Swarm. Its one SMTP credential goes to a relay (`apps/msmtpd`) so that Authelia's config
  can stay plaintext ([0038](0038-plaintext-config-substituted-secrets.md) later made that
  general).
- **HA** (2026-07-29): two replicas with a PDB and topology spread, sessions in a Valkey
  Sentinel store instead of a single Redis: the exception to single replicas, as the login path
  for everything ([0029](0029-availability-single-replicas-and-ha-surge.md)). A preStop hook
  hands the primary over via Sentinel before a pod stops, because Valkey has no failover on
  shutdown outside cluster mode and Authelia cannot use cluster mode (authelia/authelia#12766).
- **API server OIDC** (2026-09-16): a structured `AuthenticationConfiguration` trusts Authelia;
  the group binding lives in `rbac/oidc` because it applies to every client, kubectl included
  ([0005](0005-groups-infra-apps-rbac.md)).

## Alternatives considered (2026-07-16 to 2026-07-19)

- Traefik's global cross-namespace toggle: one grant for every namespace, which the reflected
  copies made unnecessary.
- A copy of the Middleware in each app, by hand.
- emberstack Reflector: it copies Secrets and ConfigMaps only and declines arbitrary CRDs.
- Kyverno `generate` policies: a full policy engine for copying one object.

## Consequences

- An app that has its own login decides per route whether to add the Middleware; a
  session-only variant exists for apps with their own basic auth. Apps that are OIDC clients
  keep their own login and use Authelia as the provider instead.
- Authelia's clients share one token bucket, sized for all of them.

## References

- `8d5bfc3d`, `1e9fc2ba`, `abe628c6`, `60ce2a83`, `063114ae`, `7e93f78e`, `66404e80`,
  `ca8f458c`, `1b3af452`..`34d78b8d`, `d4452189`, `abd926e3`.
- Conversation 2026-07-16 to 2026-07-19 (reflector design), 2026-07-23 (SMTP relay),
  2026-10-05 (why reflection).
- [infra/traefik-reflector](../../kube-cluster/infra/traefik-reflector/README.md).
