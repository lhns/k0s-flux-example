# 0021 — An object needed in several namespaces has one source; a controller copies it

- Status: Accepted
- Date: 2026-07-16, amended 2026-07-19 and 2026-09-16
- Current answer: a Secret or ConfigMap that more than one namespace needs is declared once and
  mirrored by Reflector (`infra/reflector`, emberstack): database role credentials, `sops-age`
  into every control namespace, pod-gateway's settings, a password two apps share. Traefik
  Middlewares are mirrored the same way by kube-traefik-reflector
  ([0022](0022-sso-authelia-forwardauth-reflected.md)). A second copy in git is the exception,
  kept only where neither a mirror nor substitution can reach the consumer, and its comment says
  it is a copy.

## Context

A credential needed in two namespaces, such as a Postgres role password, would be two SOPS
documents. Secrets should not be duplicated in git.

## Decision

- **Reflector for Secrets and ConfigMaps** (2026-07-16): purpose-built and narrow (Secrets,
  ConfigMaps, certificates). The source carries the reflection annotations; the copies are
  created and kept in step by the controller.
- **Not Kyverno**: a policy engine is too much machinery for copying an object. For Traefik's
  CRDs, which Reflector declines, there is kube-traefik-reflector (2026-07-19).
- **Copies where nothing else reaches** (2026-09-16/17): Flux derives substitution variable names
  from Secret keys, so two Secrets both keyed `password` collide, and some apps read neither env
  nor a mounted Secret. There a copy stays, marked as one.

## Consequences

- Replacing a duplicate by a mirror is two commits: add the source and confirm the copy, then
  remove the duplicate, or the old one is pruned before the copy exists.
- A namespace that appears while Reflector is down gets no copies: a routed namespace has no
  gateway settings and fails closed, a new control namespace cannot decrypt
  ([0046](0046-control-namespaces-always-on.md)). A fresh cluster needs one manual copy of
  `sops-age` (README, [Bootstrapping a cluster](../../README.md#bootstrapping-a-cluster)).
- The annotations on `sops-age` hand out the decryption key; only the generator's label selects
  where it goes.

## References

- `17de34ed` (first mirrored secret), `abe628c6`, `60ce2a83` (traefik-reflector), `9f958e88`
  (`sops-age` into control namespaces).
- Conversation 2026-07-16 (Reflector, not Kyverno), 2026-09-16 and 2026-09-17 (where copies
  stay).
- [shared/infra/reflector](../../shared/infra/reflector/README.md).
