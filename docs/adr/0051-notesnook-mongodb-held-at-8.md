# 0051 — Notesnook's MongoDB stays on 8 (an exception)

- Status: Accepted
- Date: 2026-10-05
- Current answer: notesnook's MongoDB stays on the 8.x line. Renovate's PR #516 (`mongo` 9) is
  held, not merged. Revisit once notesnook's services use a MongoDB driver that supports server
  9.0.

## Why this is an exception

Renovate keeps every version current, and a reviewed major is normally merged once checked
([0015](0015-self-hosted-renovate-automerge-policy.md)). This one is deliberately held.

## Context

Notesnook's services reach MongoDB through its C# drivers: the sync server pins `MongoDB.Driver`
2.22.0, and the identity server uses 3.2.1 through `AspNetCore.Identity.Mongo` 10.1.0. MongoDB
server 9.0 needs driver 3.10 or later (3.8 and 3.9 with limitations). Upstream still pins 2.22 in
its latest beta and on master, so a newer notesnook does not help.

It is not the CPU baseline ([0026](0026-cpu-baseline-ivybridge.md)): that only requires AVX,
which every worker has.

## Consequences

- Renovate keeps proposing 9.x until a rule caps `mongo` for notesnook below 9; a cap was
  proposed and not yet decided.
- When revisited, MongoDB 9.0 also needs `featureCompatibilityVersion` 8.3 first, with a backup
  before.

## References

- PR #516.
- Conversation 2026-10-05.
