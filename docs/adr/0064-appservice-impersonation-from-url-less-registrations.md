# 0064 — Appservice impersonation comes only from URL-less registrations

- Status: Accepted
- Date: 2026-10-24
- Current answer: a Matrix appservice registration that receives events claims only its own
  users. The right to act as any local user (double puppeting) is granted only by registrations
  with `url: null`, one per consumer. The mautrix bridges are the first example.

## Context

A registration's users namespace decides two things at once: which users its token may act as,
and which events Synapse pushes to it. A bridge that needs to act as local users was given a
non-exclusive namespace over all of them, and with it the events of every room with a local
member: rooms it has no business in, encrypted events it cannot read, and the plaintext of every
unencrypted room.

## Decision

- **Event-receiving registrations claim only their own users** (bot, ghosts, aliases).
- **Impersonation comes from a separate registration with `url: null`**, which Synapse never
  pushes to. `url` must be present and null; a missing one is a startup error.
- **One such registration per consumer**, with its own tokens, rather than one shared: a
  consumer's impersonation can be revoked or rotated alone, and Synapse attributes it.

## Consequences

- Revoking one consumer is removing its entry from `app_service_config_files`, a whole-key list,
  and restarting Synapse; its users fall back to ghost attribution.
- Each consumer adds a registration and a token pair in SOPS.
- Synapse reads registrations only at start: adding one and switching a consumer to it are two
  steps, since a consumer that tries the new token before Synapse has it can fail until restart.

## References

- PRs #1195, #1196.
- [matrix README](../../kube-cluster/apps/matrix/README.md), "Double puppeting".
- mautrix: [Double puppeting](https://docs.mau.fi/bridges/general/double-puppeting.html).
